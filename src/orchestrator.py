"""
BetterWiser Legal-Tech AI Briefing Agent — Main Orchestrator

Five-phase pipeline controller with CLI interface.

Usage:
    python -m src.orchestrator --month 2026-03
    python -m src.orchestrator --month 2026-03 --track C --dry-run
    python -m src.orchestrator --month 2026-03 --send
    python -m src.orchestrator --resume runs/2026-03_run_20260324T150000/

Phases:
    1  Trigger    — Build RunContext, load config, set up logging
    2  Gather     — 5 parallel sub-pipelines (inbox, web, discovery, TL, history)
    3  Synthesise — 6-pass pipeline per track (cluster→triage→draft→factcheck→ground→format)
    4  Validate   — Link check + grounding threshold check
    5  Deliver    — Archive + optional email send via MS Graph
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
import yaml
from dotenv import load_dotenv

# Load .env before any other imports that may need env vars
load_dotenv()

import anthropic

from src.delivery.archiver import archive_gathered_data, archive_synthesis
from src.delivery.email_sender import send_briefing
from src.gatherers import discovery, history_loader, inbox_reader, thought_leadership
from src.gatherers.profile_updater import update_context_if_needed
from src.gatherers.thought_leadership import _wave1_newsletter_extraction
from src.gatherers.web_scraper import scrape_urls
from src.schemas import (
    BriefingTrack,
    DeliveryReceipt,
    GatheredData,
    GatheringStats,
    RunContext,
    ValidatedBriefing,
)
from src.synthesis import (
    pass0_cluster,
    pass1_triage,
    pass2_draft,
    pass3_factcheck,
    pass35_grounding,
    pass4_format,
)
from src.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--month",
    default=None,
    help="Target month in YYYY-MM format. Defaults to current month.",
    metavar="YYYY-MM",
)
@click.option(
    "--track",
    multiple=True,
    type=click.Choice(["A", "B", "C"], case_sensitive=True),
    default=["A", "B", "C"],
    help="Which tracks to run. Can be specified multiple times. Default: all three.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Dry run: generate briefings and save to disk but don't send emails. DEFAULT.",
)
@click.option(
    "--send",
    is_flag=True,
    default=False,
    help="Actually send emails via MS Graph. Requires Azure AD credentials in .env.",
)
@click.option(
    "--resume",
    "resume_path",
    default=None,
    help="Resume from a previous failed run. Provide the run directory path.",
    metavar="PATH",
)
@click.option(
    "--skip-context-update",
    is_flag=True,
    default=False,
    help="Skip Phase 0 LinkedIn profile check and betterwiser_context.txt update.",
)
def main(
    month: Optional[str],
    track: tuple[str, ...],
    dry_run: bool,
    send: bool,
    resume_path: Optional[str],
    skip_context_update: bool,
) -> None:
    """
    BetterWiser Legal-Tech AI Briefing Agent.

    Generates three monthly intelligence briefings (Vendor & Customer,
    Global AI Policy, Thought Leadership) and delivers them via email
    or saves them to disk.

    Minimum requirement: ANTHROPIC_API_KEY in .env

    \b
    Examples:
        # Generate all 3 tracks for current month (dry-run)
        python -m src.orchestrator

        # Generate Track C only for March 2026
        python -m src.orchestrator --month 2026-03 --track C

        # Generate and send all tracks (--send disables dry-run automatically)
        python -m src.orchestrator --month 2026-03 --send

        # Resume a failed run
        python -m src.orchestrator --resume runs/2026-03_run_20260324T150000/
    """
    # Determine target month
    if month is None:
        month = datetime.now().strftime("%Y-%m")

    # Determine tracks
    tracks = [BriefingTrack(t) for t in track] if track else list(BriefingTrack)

    # --send implicitly disables dry-run: there is no reason to request a send
    # and keep dry_run=True, and the flag combination is a common user mistake.
    if send:
        dry_run = False

    # Exit code: 0 on success, 1 on failure
    exit_code = asyncio.run(_run_pipeline(
        month=month,
        tracks=tracks,
        dry_run=dry_run,
        send=send,
        resume_path=resume_path,
        skip_context_update=skip_context_update,
    ))
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

async def _run_pipeline(
    month: str,
    tracks: list[BriefingTrack],
    dry_run: bool,
    send: bool,
    resume_path: Optional[str],
    skip_context_update: bool = False,
) -> int:
    """
    Execute the full 5-phase briefing pipeline (plus Phase 0 context update).
    Returns 0 on success, 1 on fatal error.
    """
    # ---------------------------------------------------------------------------
    # Phase 1: Trigger — build RunContext, load config, set up logging
    # ---------------------------------------------------------------------------
    config = _load_config()
    runs_dir = config.get("run", {}).get("runs_dir", "runs")
    log_level = config.get("run", {}).get("log_level", "INFO")

    if resume_path:
        run_id = Path(resume_path).name
    else:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        run_id = f"{month}_run_{ts}"

    run_context = RunContext(
        run_id=run_id,
        month=month,
        tracks=tracks,
        dry_run=dry_run,
        send=send,
        resume=bool(resume_path),
        runs_dir=runs_dir,
    )

    # Set up logging FIRST so all subsequent messages are captured
    setup_logging(run_id=run_id, runs_dir=runs_dir, log_level=log_level)
    logger.info(
        f"Pipeline starting",
        extra={
            "month": month,
            "tracks": [t.value for t in tracks],
            "dry_run": dry_run,
            "send": send,
        }
    )

    # Check ANTHROPIC_API_KEY
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error(
            "ANTHROPIC_API_KEY not set. This is required. "
            "Add it to your .env file and retry."
        )
        return 1

    # Initialise Anthropic client (shared across all phases)
    model_config = config.get("model", {})
    model_id = model_config.get("id", "claude-opus-4-6")
    claude = anthropic.AsyncAnthropic(api_key=api_key)

    # Save RunContext for resume capability
    _save_checkpoint(run_context.model_dump_json(indent=2), run_id, runs_dir, "run_context.json")

    # ---------------------------------------------------------------------------
    # Phase 0: Context Update — refresh betterwiser_context.txt from LinkedIn
    # ---------------------------------------------------------------------------
    if not skip_context_update and not resume_path:
        try:
            context_updated = await update_context_if_needed(
                client=claude,
                model_id=model_id,
                month=month,
            )
            if context_updated:
                logger.info(
                    "Phase 0: betterwiser_context.txt refreshed — "
                    "Track C commentary will use the updated profile"
                )
        except Exception as e:
            # Phase 0 is non-fatal: if it fails the pipeline continues with
            # the existing context file.
            logger.warning(f"Phase 0: Context update failed (non-fatal): {e}")
    elif skip_context_update:
        logger.info("Phase 0: Skipped (--skip-context-update flag set)")
    else:
        logger.info("Phase 0: Skipped (resume mode — context already up to date)")

    # ---------------------------------------------------------------------------
    # Phase 2: Gather — 5 parallel sub-pipelines
    # ---------------------------------------------------------------------------
    logger.info("Phase 2: Gathering intelligence")

    gathered = await _gather_phase(run_context, config, claude, model_id, resume_path)
    archive_gathered_data(gathered, run_id, runs_dir)

    logger.info(
        f"Phase 2 complete",
        extra={
            "emails": gathered.stats.emails_read,
            "scraped": gathered.stats.urls_scraped,
            "discovered": gathered.stats.articles_discovered,
        }
    )

    # ---------------------------------------------------------------------------
    # Phase 3: Synthesise — 6-pass pipeline per track (run tracks in parallel)
    # ---------------------------------------------------------------------------
    logger.info(f"Phase 3: Synthesising {len(tracks)} tracks")

    synthesis_tasks = [
        _synthesise_track(track, gathered, config, claude, model_id, resume_path)
        for track in tracks
    ]
    synthesis_results = await asyncio.gather(*synthesis_tasks, return_exceptions=True)

    # ---------------------------------------------------------------------------
    # Phase 4: Validate + Phase 5: Deliver (per track)
    # ---------------------------------------------------------------------------
    receipts: list[DeliveryReceipt] = []

    for track, synthesis_result in zip(tracks, synthesis_results):
        if isinstance(synthesis_result, Exception):
            logger.error(
                f"Track {track.value}: synthesis failed: {synthesis_result}",
                exc_info=synthesis_result,
            )
            receipts.append(DeliveryReceipt(
                run_id=run_id,
                track=track,
                delivered=False,
                dry_run=dry_run,
                error=str(synthesis_result),
            ))
            continue

        # Phase 4: Validate
        validated = synthesis_result  # already a ValidatedBriefing from synthesis
        logger.info(
            f"Track {track.value}: grounding={validated.grounding_report.pass_rate:.1%}, "
            f"held={'YES' if validated.held_for_review else 'NO'}"
        )

        # Phase 5: Deliver
        recipients = config.get("recipients", {}).get(track.value, [])
        subject_template = config.get("email_subjects", {}).get(track.value, "")

        receipt = await send_briefing(
            validated=validated,
            recipients=recipients,
            run_context=run_context,
            subject_template=subject_template,
        )
        receipts.append(receipt)

    # ---------------------------------------------------------------------------
    # Final summary
    # ---------------------------------------------------------------------------
    _log_summary(run_id, receipts, dry_run)
    _save_receipts(receipts, run_id, runs_dir)

    failed = sum(1 for r in receipts if r.error and not r.held_for_review)
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# Phase 2: Gathering
# ---------------------------------------------------------------------------

async def _gather_phase(
    run_context: RunContext,
    config: dict,
    claude: anthropic.AsyncAnthropic,
    model_id: str,
    resume_path: Optional[str],
) -> GatheredData:
    """Run all 5 gathering sub-pipelines in parallel with graceful degradation."""

    # Check for resume checkpoint
    if resume_path:
        checkpoint = _load_checkpoint(run_context.run_id, run_context.runs_dir, "raw_data/gathered_data.json")
        if checkpoint:
            logger.info("Resuming: loaded gathered_data from checkpoint")
            try:
                return GatheredData.model_validate_json(checkpoint)
            except Exception as e:
                logger.warning(f"Could not deserialise gathered_data checkpoint: {e}. Re-gathering.")

    month = run_context.month
    queries_by_track = config.get("discovery_queries", {})
    newsletter_config = _load_newsletter_subscriptions()
    watchlist_config = _load_vendor_watchlist()
    curated_urls = _get_curated_urls(config, run_context.tracks)

    # Load thought_leadership watchlist for Track C
    tl_in_tracks = BriefingTrack.C in run_context.tracks

    start_time = datetime.now(tz=timezone.utc)

    # Run all 5 sub-pipelines concurrently
    # return_exceptions=True ensures one failure doesn't kill everything
    (
        email_result,
        scrape_result,
        discover_result,
        tl_result,
        history_result,
    ) = await asyncio.gather(
        inbox_reader.read_inbox(month),                                      # A
        scrape_urls(curated_urls),                                           # B
        discovery.discover_articles_all_tracks(                              # C
            tracks=run_context.tracks,
            month=month,
            queries_by_track=queries_by_track,
            client=claude,
            model_id=model_id,
        ),
        (
            thought_leadership.run_waves(                                    # D (Track C only)
                month=month,
                email_sources=[],  # will be populated after email_result
                watchlist_config=watchlist_config,
                client=claude,
                model_id=model_id,
            ) if tl_in_tracks else asyncio.sleep(0, result=[])
        ),
        asyncio.to_thread(history_loader.load_previous_month, run_context.runs_dir, month),  # E
        return_exceptions=True,
    )

    duration = (datetime.now(tz=timezone.utc) - start_time).total_seconds()

    # Unpack results, replacing exceptions with safe defaults
    emails = _safe_list(email_result, "inbox reading")
    scraped = _safe_list(scrape_result, "web scraping")
    discovered = _safe_list(discover_result, "Claude discovery")
    tl_articles = _safe_list(tl_result, "thought leadership research")
    history = history_result if isinstance(history_result, str) else None

    # If inbox was available, supplement Wave 1 with actual email content.
    # Only re-run Wave 1 (newsletter extraction) — NOT all 6 waves — to avoid
    # doubling the entire TL research cost.
    if emails and tl_in_tracks and not isinstance(tl_result, Exception):
        logger.info("Supplementing thought leadership Wave 1 with actual email content")
        try:
            email_articles, _ = await _wave1_newsletter_extraction(
                email_sources=emails,
                month=month,
                client=claude,
                model_id=model_id,
            )
            existing_urls = {a.url for a in tl_articles}
            new_articles = [a for a in email_articles if a.url not in existing_urls]
            tl_articles.extend(new_articles)
            logger.info(f"Wave 1 email supplement: {len(new_articles)} new articles added")
        except Exception as e:
            logger.warning(f"Email-augmented TL Wave 1 supplement failed: {e}")
    else:
        if not emails:
            logger.info(
                "No emails available — Track C thought leadership will rely on "
                "web search waves (2–6) and Tavily only"
            )
        elif not tl_in_tracks:
            logger.debug("Track C not selected — skipping email Wave 1 supplement")
        else:
            logger.debug("Track C thought leadership result unavailable — skipping email supplement")

    # Merge discovered + TL articles, deduplicating by URL
    _seen_urls: set[str] = set()
    all_discovered = []
    for _article in discovered + tl_articles:
        if _article.url not in _seen_urls:
            _seen_urls.add(_article.url)
            all_discovered.append(_article)

    stats = GatheringStats(
        emails_read=len(emails),
        attachments_parsed=sum(len(e.attachments) for e in emails),
        urls_scraped=len([s for s in scraped if not s.error]),
        scrape_failures=len([s for s in scraped if s.error]),
        articles_discovered=len(all_discovered),
        duration_seconds=duration,
    )

    logger.info(
        f"Gathering stats: {stats.emails_read} emails, "
        f"{stats.urls_scraped} pages scraped, "
        f"{stats.articles_discovered} articles discovered in {duration:.1f}s"
    )

    return GatheredData(
        run_context=run_context,
        scraped_sources=scraped,
        email_sources=emails,
        discovered_articles=all_discovered,
        historical_context=history,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Phase 3: Synthesis (6-pass, per track)
# ---------------------------------------------------------------------------

async def _synthesise_track(
    track: BriefingTrack,
    gathered: GatheredData,
    config: dict,
    claude: anthropic.AsyncAnthropic,
    model_id: str,
    resume_path: Optional[str],
) -> ValidatedBriefing:
    """Run all 6 synthesis passes for one track."""
    logger.info(f"Track {track.value}: starting 6-pass synthesis")
    model_config = config.get("model", {})
    grounding_config = config.get("grounding", {})

    # Check for synthesis resume checkpoint
    if resume_path:
        checkpoint = _load_checkpoint(
            gathered.run_context.run_id,
            gathered.run_context.runs_dir,
            f"synthesis/synthesis_track_{track.value}.json"
        )
        if checkpoint:
            logger.info(
                f"Track {track.value}: synthesis checkpoint found but not loaded "
                f"— re-synthesising (resume of synthesis not yet implemented)"
            )

    track_config = next(
        (t for t in config.get("tracks", []) if t.get("id") == track.value),
        {}
    )

    # Pass 0: Cluster + dedup
    clusters = pass0_cluster.cluster_and_dedup(gathered, track)

    # Pass 1: Triage + sort by authority
    sorted_clusters = pass1_triage.triage_clusters(
        clusters=clusters,
        track=track,
        item_count_min=track_config.get("item_count_min"),
        item_count_max=track_config.get("item_count_max"),
    )

    # Pass 2: Draft with extended thinking + citations
    synthesis = await pass2_draft.draft_briefing(
        track=track,
        gathered=gathered,
        clusters=sorted_clusters,
        client=claude,
        model_config=model_config,
    )

    # Archive synthesis after pass 2 (for resume)
    archive_synthesis(synthesis, gathered.run_context.run_id, gathered.run_context.runs_dir)

    # Pass 3: Fact-check with Citations API
    synthesis = await pass3_factcheck.fact_check(
        synthesis=synthesis,
        gathered=gathered,
        client=claude,
        model_config=model_config,
    )

    # Pass 3.5: Programmatic grounding verification
    synthesis, grounding_report = pass35_grounding.run_grounding_verification(
        synthesis=synthesis,
        gathered=gathered,
        grounding_threshold=grounding_config.get("pass_rate_threshold", 0.95),
        fuzzy_threshold=grounding_config.get("fuzzy_match_threshold", 80),
    )

    # Pass 4: Format HTML + validate links
    subject_template = config.get("email_subjects", {}).get(track.value, "")
    validated = await pass4_format.format_and_validate(
        synthesis=synthesis,
        grounding_report=grounding_report,
        month=gathered.run_context.month,
        subject_template=subject_template,
    )

    logger.info(
        f"Track {track.value}: synthesis complete — "
        f"passes={synthesis.pass_completed}, "
        f"held={'YES' if validated.held_for_review else 'NO'}"
    )

    return validated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(config_path: str = "config/briefing_config.yaml") -> dict:
    """Load master configuration YAML."""
    path = Path(config_path)
    if not path.exists():
        logger.warning(f"Config not found at {config_path}. Using defaults.")
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_newsletter_subscriptions() -> list[dict]:
    """Load newsletter subscription rules."""
    path = Path("config/newsletter_subscriptions.yaml")
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("subscriptions", [])


def _load_vendor_watchlist() -> dict:
    """Load vendor and thought leader watchlist."""
    path = Path("config/vendor_watchlist.yaml")
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get_curated_urls(config: dict, tracks: list[BriefingTrack]) -> list[str]:
    """Extract curated URLs for the specified tracks from config."""
    urls: list[str] = []
    curated = config.get("curated_sources", {})
    for track in tracks:
        key = f"track_{track.value}"
        for source in curated.get(key, []):
            url = source.get("url")
            if url:
                urls.append(url)
    # Deduplicate
    return list(dict.fromkeys(urls))


def _safe_list(result, context: str) -> list:
    """Return result if it's a list, else [] with a warning."""
    if isinstance(result, Exception):
        logger.warning(f"Gathering sub-pipeline failed ({context}): {result}. Continuing.")
        return []
    if result is None:
        return []
    return result if isinstance(result, list) else []


def _save_checkpoint(data: str, run_id: str, runs_dir: str, filename: str) -> None:
    """Save a checkpoint file for resume capability."""
    try:
        path = Path(runs_dir) / run_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    except Exception as e:
        logger.debug(f"Could not save checkpoint {filename}: {e}")


def _load_checkpoint(run_id: str, runs_dir: str, filename: str) -> Optional[str]:
    """Load a checkpoint file, returning None if not found."""
    try:
        path = Path(runs_dir) / run_id / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return None


def _save_receipts(receipts: list[DeliveryReceipt], run_id: str, runs_dir: str) -> None:
    """Save delivery receipts to JSON for audit logging."""
    try:
        path = Path(runs_dir) / run_id / "delivery_receipts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [r.model_dump() for r in receipts]
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not save delivery receipts: {e}")


def _log_summary(run_id: str, receipts: list[DeliveryReceipt], dry_run: bool) -> None:
    """Log a human-readable run summary."""
    logger.info("=" * 60)
    logger.info(f"RUN SUMMARY — {run_id}")
    for receipt in receipts:
        status = (
            "HELD FOR REVIEW" if receipt.held_for_review
            else "SAVED (dry-run)" if receipt.dry_run and not receipt.error
            else "SENT" if receipt.delivered
            else f"FAILED: {receipt.error}"
        )
        output = f" → {receipt.output_path}" if receipt.output_path else ""
        logger.info(f"  Track {receipt.track.value}: {status}{output}")

    if dry_run:
        logger.info(
            "DRY-RUN MODE: Briefings saved to disk. "
            "Add --send flag with Azure AD credentials to email them."
        )
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
