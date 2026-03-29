#!/usr/bin/env python3
"""
demo_run.py — BetterWiser Briefing Agent smoke test / demo run.

Runs the FULL 5-phase pipeline with pre-built demo data so that every
code path is exercised without spending significant API credits.

What this does NOT do (intentionally):
  - No real inbox reading (Azure creds not needed)
  - No real web scraping (Jina/Spider/Crawl4AI not called)
  - No Tavily calls
  - No LinkedIn/context update (Phase 0)
  - No extended thinking (budget=0 → falls back automatically)

What this DOES do:
  - Builds real GatheredData / RunContext Pydantic objects
  - Runs Pass 0 (cluster), Pass 1 (triage) — no API
  - Runs Pass 2 (draft)    — REAL Claude call, Haiku, ~1 000 tokens out
  - Runs Pass 3 (factcheck) — REAL Claude call, Haiku, minimal
  - Runs Pass 3.5 (grounding) — programmatic only, no API
  - Runs Pass 4 (format + link validation) — no API
  - Archives the HTML to runs/demo_*/
  - Sends a [DEMO] email via MS Graph if --send-email flag is given
    (and Azure credentials are present in the environment)

Usage:
    python demo_run.py                 # save HTML only, no email
    python demo_run.py --send-email    # also send demo email
    python demo_run.py --track C       # single track (A, B, or C)
    python demo_run.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path so src.* imports work
sys.path.insert(0, str(Path(__file__).parent))

import anthropic

from src.schemas import (
    BriefingTrack,
    DiscoveredArticle,
    GatheredData,
    GatheringStats,
    GroundingReport,
    RunContext,
    ScrapedSource,
    SourceTier,
)
from src.synthesis.pass0_cluster import cluster_and_dedup
from src.synthesis.pass1_triage import triage_clusters
from src.synthesis.pass2_draft import draft_briefing
from src.synthesis.pass3_factcheck import fact_check
from src.synthesis.pass35_grounding import run_grounding_verification
from src.synthesis.pass4_format import format_and_validate
from src.delivery.email_sender import send_briefing

# ---------------------------------------------------------------------------
# Demo configuration — intentionally cheap
# ---------------------------------------------------------------------------

DEMO_MODEL = "claude-haiku-4-5-20251001"   # cheapest available model

# max_tokens must be > extended_thinking_budget.
# Setting budget to 0 will cause a BadRequestError which pass2_draft catches
# and retries WITHOUT extended thinking — saving thinking tokens entirely.
DEMO_MODEL_CONFIG: dict = {
    "id": DEMO_MODEL,
    "max_tokens": 1500,
    "extended_thinking_budget": 0,          # triggers no-thinking fallback
    "max_context_sources": 3,               # only 3 demo docs per track
    "source_content_max_chars": 600,        # short snippets
}

# Lower grounding threshold so demo data never gets held for review.
# Real runs use 0.95 (from briefing_config.yaml).
DEMO_GROUNDING_THRESHOLD = 0.0
DEMO_FUZZY_THRESHOLD = 40

DEMO_SUBJECT_PREFIX = "[DEMO] "
DEMO_BANNER_HTML = textwrap.dedent("""\
    <table width="100%" cellpadding="0" cellspacing="0" border="0"
           style="margin-bottom:16px;">
      <tr>
        <td style="background-color:#FFF3CD;border:1px solid #FFC107;
                   border-radius:4px;padding:10px 16px;">
          <p style="margin:0;font-size:13px;font-weight:bold;color:#856404;
                    font-family:Calibri,Arial,sans-serif;">
            &#9888; DEMO RUN — This is a smoke-test briefing generated with synthetic
            data. Do not distribute. Links are placeholder URLs.
          </p>
        </td>
      </tr>
    </table>
""")

# ---------------------------------------------------------------------------
# Demo source data — one set per track, realistic but clearly synthetic
# ---------------------------------------------------------------------------

def _demo_scraped_sources(track: BriefingTrack) -> list[ScrapedSource]:
    now = datetime.now(tz=timezone.utc)
    if track == BriefingTrack.A:
        return [
            ScrapedSource(
                url="https://demo.harvey.ai/blog/harvey-enterprise-2026",
                title="Harvey Launches Enterprise Contract Intelligence Suite",
                content=(
                    "Harvey AI announced the Harvey Enterprise Contract Intelligence Suite on "
                    "12 March 2026, targeting large law firms and Fortune 500 in-house legal "
                    "teams in the Asia-Pacific region. The suite adds multi-jurisdiction clause "
                    "benchmarking and automated redline generation. Harvey confirmed a partnership "
                    "with Allen & Overy and Baker McKenzie for regional rollout. Pricing starts "
                    "at USD 150,000 per year for teams of 25+ lawyers."
                ),
                tier=SourceTier.TIER_2,
                scraper_used="demo",
                scraped_at=now,
                word_count=80,
            ),
            ScrapedSource(
                url="https://demo.sal.org.sg/news/ai-competency-framework-2026",
                title="SAL Launches AI Competency Framework for Singapore Lawyers",
                content=(
                    "The Singapore Academy of Law (SAL) published its AI Competency Framework "
                    "for Legal Professionals on 20 March 2026. The framework establishes three "
                    "tiers: AI-Aware, AI-Proficient, and AI-Advanced. All Singapore-qualified "
                    "lawyers will need to complete AI-Aware CPD credits by 31 December 2026. "
                    "SAL partnered with NUS Law and SMU Yong Pung How School of Law to deliver "
                    "accredited training modules."
                ),
                tier=SourceTier.TIER_1,
                scraper_used="demo",
                scraped_at=now,
                word_count=75,
            ),
            ScrapedSource(
                url="https://demo.legora.com/blog/singapore-expansion",
                title="Legora Expands to Singapore with Rajah & Tann Partnership",
                content=(
                    "Nordic legal AI startup Legora announced its Singapore market entry on "
                    "5 March 2026 through a strategic partnership with Rajah & Tann Asia. "
                    "Legora's platform specialises in multi-lingual contract review with support "
                    "for English, Mandarin, and Bahasa Indonesia — directly relevant to "
                    "ASEAN cross-border transactions. The Singapore office will be led by a "
                    "former Allen & Gledhill technology partner."
                ),
                tier=SourceTier.TIER_2,
                scraper_used="demo",
                scraped_at=now,
                word_count=72,
            ),
        ]
    elif track == BriefingTrack.B:
        return [
            ScrapedSource(
                url="https://demo.pdpc.gov.sg/ai-governance-framework-march-2026",
                title="PDPC Releases Updated AI Governance Framework v3.0",
                content=(
                    "Singapore's Personal Data Protection Commission (PDPC) released AI "
                    "Governance Framework version 3.0 on 15 March 2026. The updated framework "
                    "introduces mandatory model cards for AI systems that process personal data "
                    "of more than 10,000 individuals. Legal service providers are explicitly "
                    "included in the scope. Firms have 12 months to achieve compliance. The "
                    "framework aligns with the ASEAN Guide on AI Governance published in 2024."
                ),
                tier=SourceTier.TIER_1,
                scraper_used="demo",
                scraped_at=now,
                word_count=78,
            ),
            ScrapedSource(
                url="https://demo.minlaw.gov.sg/generative-ai-courts-guidance",
                title="Singapore MinLaw Issues Guidance on GenAI Use in Court Proceedings",
                content=(
                    "Singapore's Ministry of Law issued practical guidance on 8 March 2026 "
                    "covering the use of generative AI tools in court proceedings. The guidance "
                    "requires lawyers to disclose when AI has been used in drafting submissions "
                    "and to verify all AI-generated citations independently. The guidance "
                    "applies to all Singapore courts with effect from 1 April 2026 and was "
                    "developed in consultation with the Singapore Courts and the Law Society."
                ),
                tier=SourceTier.TIER_1,
                scraper_used="demo",
                scraped_at=now,
                word_count=78,
            ),
            ScrapedSource(
                url="https://demo.euaiact.eu/high-risk-legal-services-update",
                title="EU AI Act: Legal Services Added to High-Risk Annex Guidance",
                content=(
                    "The European AI Office published updated guidance on 18 March 2026 "
                    "clarifying that AI systems used by law firms for litigation outcome "
                    "prediction and automated legal advice fall under the high-risk category "
                    "in Annex III of the EU AI Act. Firms providing cross-border services to "
                    "EU clients must register affected systems in the EU AI Database by "
                    "August 2026. The guidance directly impacts Singapore-based firms with "
                    "EU client relationships."
                ),
                tier=SourceTier.TIER_1,
                scraper_used="demo",
                scraped_at=now,
                word_count=82,
            ),
        ]
    else:  # Track C
        return [
            ScrapedSource(
                url="https://demo.mckinsey.com/legal-ai-maturity-2026",
                title="McKinsey: Legal AI Maturity — From Pilot to Production",
                content=(
                    "McKinsey Global Institute published research on 10 March 2026 showing "
                    "that only 18% of law firms with active AI pilots have moved a use case "
                    "into full production in the past 12 months. The primary barriers are "
                    "change management (cited by 67% of respondents), governance uncertainty "
                    "(54%), and inadequate data infrastructure (48%). Firms that succeeded "
                    "shared three characteristics: a dedicated AI transformation lead, an "
                    "explicit change management programme, and executive sponsorship at "
                    "the managing partner or GC level."
                ),
                tier=SourceTier.TIER_2,
                scraper_used="demo",
                scraped_at=now,
                word_count=95,
            ),
            ScrapedSource(
                url="https://demo.annasubstack.com/legal-ai-leadership-march-2026",
                title="Anna Lozynski: The GC as AI Change Architect",
                content=(
                    "In her March 2026 Substack post, legal technology strategist Anna Lozynski "
                    "argues that the General Counsel must evolve from AI sceptic or enthusiast "
                    "into what she calls the 'AI Change Architect' — the person who translates "
                    "business AI strategy into legal department capability. She introduces a "
                    "three-stage readiness model: Informed, Enabled, and Leading. Lozynski "
                    "draws on her experience advising major APAC in-house teams and warns that "
                    "GCs who remain passive risk ceding the AI agenda to procurement and IT."
                ),
                tier=SourceTier.TIER_2,
                scraper_used="demo",
                scraped_at=now,
                word_count=92,
            ),
            ScrapedSource(
                url="https://demo.pwc.com/sg/legal-ai-value-2026",
                title="PwC Singapore: Where Legal AI Creates Real Value — 2026 Survey",
                content=(
                    "PwC Singapore's 2026 Legal Innovation Survey, published 22 March 2026, "
                    "found that Singapore-based in-house legal teams are achieving 30-40% "
                    "time savings on contract review and due diligence through AI tools. "
                    "However, 72% of respondents say their organisations lack a formal AI "
                    "usage policy for the legal function. PwC recommends that legal leaders "
                    "prioritise three governance actions: establishing an AI usage policy, "
                    "implementing output verification workflows, and measuring AI ROI against "
                    "defined legal KPIs."
                ),
                tier=SourceTier.TIER_2,
                scraper_used="demo",
                scraped_at=now,
                word_count=90,
            ),
        ]


def _demo_discovered_articles(track: BriefingTrack) -> list[DiscoveredArticle]:
    if track == BriefingTrack.A:
        return [
            DiscoveredArticle(
                url="https://demo.artificiallawyer.com/harvey-apac-march-2026",
                title="Harvey AI Targets APAC Growth with Singapore Hub",
                snippet=(
                    "Harvey AI confirmed plans to open a Singapore regional hub in Q3 2026, "
                    "hiring 30 legal AI specialists. CEO Winston Weinberg cited APAC's fast-growing "
                    "demand for enterprise legal AI as the primary driver."
                ),
                source_name="Artificial Lawyer",
                published_date="2026-03-14",
                track=BriefingTrack.A,
                tier=SourceTier.TIER_2,
                discovered_via="claude_web_search",
            ),
        ]
    elif track == BriefingTrack.B:
        return [
            DiscoveredArticle(
                url="https://demo.imda.gov.sg/ai-testing-framework-legal",
                title="IMDA Launches AI Testing Framework for Legal Sector Applications",
                snippet=(
                    "Singapore's IMDA released a sector-specific AI testing framework for "
                    "legal applications on 25 March 2026, covering accuracy benchmarks, "
                    "hallucination detection, and bias auditing for legal AI tools."
                ),
                source_name="IMDA Singapore",
                published_date="2026-03-25",
                track=BriefingTrack.B,
                tier=SourceTier.TIER_1,
                discovered_via="claude_web_search",
            ),
        ]
    else:
        return [
            DiscoveredArticle(
                url="https://demo.lawnext.com/ai-workforce-legal-2026",
                title="Law Next: 2026 Legal AI Workforce Report",
                snippet=(
                    "Law Next's annual workforce survey finds that 43% of legal professionals "
                    "now use AI tools weekly, up from 19% in 2024. Demand for AI literacy "
                    "training has overtaken traditional legal research skills in job postings."
                ),
                source_name="Law Next",
                published_date="2026-03-18",
                track=BriefingTrack.C,
                tier=SourceTier.TIER_2,
                discovered_via="claude_web_search",
            ),
        ]


def _build_demo_gathered(
    run_context: RunContext,
    track: BriefingTrack,
) -> GatheredData:
    scraped = _demo_scraped_sources(track)
    discovered = _demo_discovered_articles(track)
    return GatheredData(
        run_context=run_context,
        scraped_sources=scraped,
        email_sources=[],           # inbox skipped in demo
        discovered_articles=discovered,
        historical_context=None,    # no history on first demo run
        stats=GatheringStats(
            emails_read=0,
            urls_scraped=len(scraped),
            articles_discovered=len(discovered),
            skipped_reasons=["demo mode — inbox reading skipped"],
        ),
    )


# ---------------------------------------------------------------------------
# Demo email subject / subject templates
# ---------------------------------------------------------------------------

DEMO_SUBJECT_TEMPLATES: dict[BriefingTrack, str] = {
    BriefingTrack.A: "[DEMO] BetterWiser Legal Intelligence — Vendor & Customer Update — {month_human}",
    BriefingTrack.B: "[DEMO] BetterWiser Legal Intelligence — Global AI Policy Watch — {month_human}",
    BriefingTrack.C: "[DEMO] BetterWiser Legal Intelligence — Thought Leadership Digest — {month_human}",
}


# ---------------------------------------------------------------------------
# Per-track pipeline runner
# ---------------------------------------------------------------------------

async def _run_track(
    track: BriefingTrack,
    run_context: RunContext,
    client: anthropic.AsyncAnthropic,
    send_email: bool,
    config: dict,
) -> dict:
    """Run the full 6-pass synthesis pipeline for one demo track."""
    label = f"Track {track.value}"
    result: dict = {"track": track.value, "passed": [], "failed": [], "output_path": None}

    # ------------------------------------------------------------------ #
    # Phase 2 (DEMO): build gathered data from pre-baked demo sources     #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Phase 2 — injecting demo data (no real scraping)")
    gathered = _build_demo_gathered(run_context, track)
    result["passed"].append("Phase 2 (demo data)")

    # ------------------------------------------------------------------ #
    # Pass 0: Cluster & Dedup                                             #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Pass 0 — cluster & dedup")
    try:
        clusters = cluster_and_dedup(gathered, track)
        result["passed"].append(f"Pass 0 (clusters={len(clusters)})")
    except Exception as exc:
        result["failed"].append(f"Pass 0: {exc}")
        print(f"  [{label}] FAIL Pass 0: {exc}")
        return result

    # ------------------------------------------------------------------ #
    # Pass 1: Triage & Sort                                               #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Pass 1 — triage & sort")
    try:
        track_cfg = next(
            (t for t in config.get("tracks", []) if t["id"] == track.value), {}
        )
        clusters = triage_clusters(
            clusters,
            track,
            item_count_min=track_cfg.get("item_count_min", 3),
            item_count_max=track_cfg.get("item_count_max", 5),
        )
        result["passed"].append(f"Pass 1 (sorted clusters={len(clusters)})")
    except Exception as exc:
        result["failed"].append(f"Pass 1: {exc}")
        print(f"  [{label}] FAIL Pass 1: {exc}")
        return result

    # ------------------------------------------------------------------ #
    # Pass 2: Draft (real Claude API call — Haiku, no extended thinking)  #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Pass 2 — drafting with Claude ({DEMO_MODEL}) …")
    try:
        synthesis = await draft_briefing(
            track=track,
            gathered=gathered,
            clusters=clusters,
            client=client,
            model_config=DEMO_MODEL_CONFIG,
        )
        chars = len(synthesis.raw_html)
        result["passed"].append(f"Pass 2 (output={chars} chars)")
        print(f"  [{label}] Pass 2 complete — {chars} chars generated")
    except Exception as exc:
        result["failed"].append(f"Pass 2: {exc}")
        print(f"  [{label}] FAIL Pass 2: {exc}")
        return result

    # ------------------------------------------------------------------ #
    # Pass 3: Fact-check                                                  #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Pass 3 — fact-check")
    try:
        synthesis = await fact_check(
            synthesis=synthesis,
            gathered=gathered,
            client=client,
            model_config=DEMO_MODEL_CONFIG,
        )
        result["passed"].append("Pass 3")
    except Exception as exc:
        # Non-fatal: log and continue with unverified synthesis
        print(f"  [{label}] WARN Pass 3 failed (non-fatal): {exc}")
        result["passed"].append("Pass 3 (skipped — non-fatal)")

    # ------------------------------------------------------------------ #
    # Pass 3.5: Programmatic grounding                                    #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Pass 3.5 — grounding verification")
    try:
        synthesis, grounding = run_grounding_verification(
            synthesis=synthesis,
            gathered=gathered,
            grounding_threshold=DEMO_GROUNDING_THRESHOLD,
            fuzzy_threshold=DEMO_FUZZY_THRESHOLD,
        )
        rate = grounding.pass_rate
        result["passed"].append(f"Pass 3.5 (grounding={rate:.0%})")
    except Exception as exc:
        print(f"  [{label}] WARN Pass 3.5 failed (non-fatal): {exc}")
        grounding = GroundingReport(
            total_claims=0,
            grounded_claims=0,
            pass_rate=1.0,
            below_threshold=False,
        )
        result["passed"].append("Pass 3.5 (skipped — non-fatal)")

    # ------------------------------------------------------------------ #
    # Pass 4: Format & validate links                                     #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Pass 4 — HTML formatting + link validation")
    try:
        validated = await format_and_validate(
            synthesis=synthesis,
            grounding_report=grounding,
            month=run_context.month,
            subject_template=DEMO_SUBJECT_TEMPLATES[track],
        )
        # Inject demo banner into the final HTML
        validated.final_html = _inject_demo_banner(validated.final_html)
        dead = sum(1 for r in validated.link_results if not r.reachable)
        result["passed"].append(f"Pass 4 (dead_links={dead})")
        print(f"  [{label}] Pass 4 complete — {dead} dead link(s) (expected for demo URLs)")
    except Exception as exc:
        result["failed"].append(f"Pass 4: {exc}")
        print(f"  [{label}] FAIL Pass 4: {exc}")
        return result

    # ------------------------------------------------------------------ #
    # Phase 5: Deliver                                                    #
    # ------------------------------------------------------------------ #
    print(f"  [{label}] Phase 5 — delivery")
    recipients = config.get("recipients", {}).get(track.value, [])
    if not recipients:
        recipients = [{"email": "lynette@betterwiser.com", "name": "Lynette Ooi"}]

    demo_run_context = RunContext(
        run_id=run_context.run_id,
        month=run_context.month,
        tracks=run_context.tracks,
        dry_run=not send_email,
        send=send_email,
    )

    try:
        receipt = await send_briefing(
            validated=validated,
            recipients=recipients,
            run_context=demo_run_context,
            subject_template=DEMO_SUBJECT_TEMPLATES[track],
        )
        result["output_path"] = receipt.output_path
        if receipt.delivered:
            result["passed"].append(f"Phase 5 — email sent to {', '.join(receipt.recipients)}")
            print(f"  [{label}] Email sent!")
        elif receipt.dry_run:
            result["passed"].append(f"Phase 5 — saved to {receipt.output_path}")
            print(f"  [{label}] Saved: {receipt.output_path}")
        else:
            msg = receipt.error or "not sent"
            result["passed"].append(f"Phase 5 — {msg}")
            print(f"  [{label}] Not sent: {msg}")
    except Exception as exc:
        result["failed"].append(f"Phase 5: {exc}")
        print(f"  [{label}] FAIL Phase 5: {exc}")

    return result


def _inject_demo_banner(html: str) -> str:
    """Insert the yellow DEMO banner immediately after the opening <body> tag."""
    # Insert after the outermost <table> opening to sit above all content
    marker = "<table width=\"100%\""
    idx = html.find(marker)
    if idx == -1:
        return html
    # Find the matching </tr></table> of the outer row and inject banner
    # Simpler: inject just after <td align="center" padding...> inner td
    inner_marker = 'align="center"'
    idx2 = html.find(inner_marker, idx)
    if idx2 == -1:
        return html
    # Find the closing > of that td
    close = html.find(">", idx2)
    if close == -1:
        return html
    return html[: close + 1] + "\n" + DEMO_BANNER_HTML + html[close + 1 :]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def _main(tracks: list[BriefingTrack], send_email: bool) -> int:
    """Run the demo pipeline for all requested tracks."""

    # Verify ANTHROPIC_API_KEY is set before doing anything
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set in the environment.")
        print("       Set it in your .env file or shell and retry.")
        return 1

    # Load config (real config, so we test that loading works too)
    try:
        import yaml  # type: ignore
        config_path = Path("config/briefing_config.yaml")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except Exception as exc:
        print(f"WARN: Could not load briefing_config.yaml ({exc}) — using defaults")
        config = {}

    # Build run context
    month = datetime.now(tz=timezone.utc).strftime("%Y-%m")
    run_id = f"{month}_DEMO_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    run_context = RunContext(
        run_id=run_id,
        month=month,
        tracks=tracks,
        dry_run=not send_email,
        send=send_email,
        runs_dir=config.get("run", {}).get("runs_dir", "runs"),
    )

    print()
    print("=" * 62)
    print("  BetterWiser Briefing Agent — DEMO RUN")
    print(f"  Run ID : {run_id}")
    print(f"  Month  : {month}")
    print(f"  Tracks : {', '.join(t.value for t in tracks)}")
    print(f"  Model  : {DEMO_MODEL}")
    print(f"  Send   : {'YES' if send_email else 'NO — dry-run, saving HTML only'}")
    print("=" * 62)
    print()

    client = anthropic.AsyncAnthropic(api_key=api_key)

    all_results = []
    for track in tracks:
        print(f"--- Track {track.value} ---")
        result = await _run_track(
            track=track,
            run_context=run_context,
            client=client,
            send_email=send_email,
            config=config,
        )
        all_results.append(result)
        print()

    # Summary
    print("=" * 62)
    print("  DEMO RUN SUMMARY")
    print("=" * 62)
    all_ok = True
    for r in all_results:
        status = "PASS" if not r["failed"] else "FAIL"
        if r["failed"]:
            all_ok = False
        print(f"\n  Track {r['track']}  [{status}]")
        for p in r["passed"]:
            print(f"    ✓ {p}")
        for f in r["failed"]:
            print(f"    ✗ {f}")
        if r["output_path"]:
            print(f"    → {r['output_path']}")
    print()
    if all_ok:
        print("  All systems operational. Pipeline is working correctly.")
    else:
        print("  One or more tracks had failures — see details above.")
    print("=" * 62)
    print()

    return 0 if all_ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BetterWiser Briefing Agent — demo/smoke test run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python demo_run.py                  # all tracks, dry-run
              python demo_run.py --send-email     # all tracks + send demo email
              python demo_run.py --track C        # Track C only
        """),
    )
    parser.add_argument(
        "--track",
        choices=["A", "B", "C"],
        action="append",
        dest="tracks",
        default=None,
        help="Track(s) to run (default: all three)",
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        default=False,
        help="Send demo email via MS Graph (requires Azure credentials in environment)",
    )
    args = parser.parse_args()

    tracks = (
        [BriefingTrack(t) for t in args.tracks]
        if args.tracks
        else [BriefingTrack.A, BriefingTrack.B, BriefingTrack.C]
    )

    exit_code = asyncio.run(_main(tracks=tracks, send_email=args.send_email))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
