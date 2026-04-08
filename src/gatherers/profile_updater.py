"""
LinkedIn Profile Context Updater — Phase 0.

Automatically refreshes config/betterwiser_context.txt once per month by:
  1. Scraping Lynette Ooi's LinkedIn public profile (https://www.linkedin.com/in/lynetteooi/)
       via the existing tiered scraper (Jina → Spider → Crawl4AI).
  2. Running targeted web searches via Claude's built-in web_search tool to surface
       recent news, publications, speaking engagements, and announcements.
  3. Asking Claude to compare the gathered intelligence against the current context
       file and produce an updated version reflecting any new or changed facts.

A dated backup of the previous context is written to config/context_backups/ before
any change is made, giving a full audit trail of what changed and when.

No update is written if Claude determines nothing material has changed — the function
logs a "no changes detected" message and returns False in that case.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

# Paths (relative to project root, where the orchestrator is run from)
CONTEXT_FILE = Path("config/betterwiser_context.txt")
CONTEXT_BACKUP_DIR = Path("config/context_backups")

# LinkedIn profile to monitor
LINKEDIN_URL = "https://www.linkedin.com/in/lynetteooi/"

# Web search tool definition (matches discovery.py)
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
}

# Additional search queries to run alongside the LinkedIn scrape
_SEARCH_QUERIES = [
    'Lynette Ooi BetterWiser 2026 news announcement',
    'Lynette Ooi "legal innovation" Singapore speaking event 2026',
    '"BetterWiser" Singapore consulting update 2026',
    'Lynette Ooi Substack "Innovate Legal" 2026',
    'Lynette Ooi LinkedIn update advisory board publication',
]


async def update_context_if_needed(
    client: anthropic.AsyncAnthropic,
    model_id: str = "claude-opus-4-6",
    month: Optional[str] = None,
) -> bool:
    """
    Entry point for Phase 0.  Checks whether a context update has already been
    performed this month (to avoid redundant API calls on re-runs), then gathers
    fresh profile data and asks Claude to revise the context file if warranted.

    Args:
        client:   Async Anthropic client, already initialised.
        model_id: Claude model to use for synthesis.
        month:    "YYYY-MM" string for the current run.  Defaults to today's month.

    Returns:
        True  — context file was updated.
        False — no update required (either already run this month or no new info).
    """
    if month is None:
        month = datetime.now(tz=timezone.utc).strftime("%Y-%m")

    logger.info("Phase 0: Checking LinkedIn profile for context updates")

    # Skip if we already ran an update this month (idempotency for re-runs)
    if _already_updated_this_month(month):
        logger.info(f"Phase 0: Context already updated for {month} — skipping")
        return False

    current_context = _read_current_context()
    if not current_context:
        logger.warning("Phase 0: betterwiser_context.txt not found — skipping update")
        return False

    # Step 1: Gather raw intelligence (LinkedIn scrape + web searches)
    scraped_profile = await _scrape_linkedin_profile()
    search_snippets = await _run_web_searches(client, model_id)

    # Step 2: Ask Claude to produce an updated context (or confirm no change needed)
    updated_context, changed = await _synthesise_update(
        current_context=current_context,
        scraped_profile=scraped_profile,
        search_snippets=search_snippets,
        client=client,
        model_id=model_id,
        month=month,
    )

    if not changed:
        logger.info("Phase 0: No material changes detected — context file unchanged")
        # Still write a stamp so we don't re-check until next month
        _write_update_stamp(month)
        return False

    # Step 3: Back up old context and write updated version
    _backup_context(current_context, month)
    CONTEXT_FILE.write_text(updated_context, encoding="utf-8")
    _write_update_stamp(month)
    logger.info(f"Phase 0: betterwiser_context.txt updated for {month}")
    return True


# ---------------------------------------------------------------------------
# Step 1 — Gather intelligence
# ---------------------------------------------------------------------------

async def _scrape_linkedin_profile() -> str:
    """
    Attempt to fetch the LinkedIn public profile using the project's existing
    tiered scraper.  LinkedIn heavily restricts scraping, so this is best-effort;
    even a partial response (intro text, headline, recent posts visible without
    login) is useful for detecting changes.

    Returns raw text content or an empty string on total failure.
    """
    logger.info(f"Phase 0: Scraping {LINKEDIN_URL}")
    try:
        # Import here to avoid circular imports at module load time
        from src.gatherers.web_scraper import scrape_url

        scraped = await scrape_url(LINKEDIN_URL)
        if scraped.error:
            logger.info(
                f"Phase 0: LinkedIn scrape returned partial/no content "
                f"(scraper: {scraped.scraper_used}, error: {scraped.error}) — "
                "will rely on web search results"
            )
            return scraped.content or ""
        logger.info(
            f"Phase 0: LinkedIn scraped via {scraped.scraper_used} "
            f"({scraped.word_count} words)"
        )
        return scraped.content
    except Exception as e:
        logger.warning(f"Phase 0: LinkedIn scrape raised exception: {e}")
        return ""


async def _run_web_searches(
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> str:
    """
    Run a small set of targeted web searches using Claude's built-in web_search
    tool to surface recent news, publications, and announcements about Lynette
    Ooi and BetterWiser.

    Returns a single concatenated string of all discovered snippets.
    """
    logger.info(f"Phase 0: Running {len(_SEARCH_QUERIES)} web searches for profile update")
    snippets: list[str] = []

    prompt = (
        "You are a research assistant helping to update a company context document.\n\n"
        "Use the web_search tool to search for the following queries one by one, "
        "then return ALL search results you find — do not filter or summarise yet. "
        "Include job title changes, new publications, speaking engagements, advisory "
        "board appointments, awards, new services, and any other factual updates.\n\n"
        "Queries to run:\n"
        + "\n".join(f"  {i+1}. {q}" for i, q in enumerate(_SEARCH_QUERIES))
        + "\n\nAfter running all searches, output the raw collected facts as a "
        "bullet-point list.  Do not omit any information — completeness matters here."
    )

    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=4096,
            temperature=0.3,
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )

        # Collect all text blocks from the response
        for block in response.content:
            if hasattr(block, "text") and block.text:
                snippets.append(block.text)

        combined = "\n\n".join(snippets)
        logger.info(
            f"Phase 0: Web searches returned {len(combined.split())} words of raw intelligence"
        )
        return combined

    except Exception as e:
        logger.warning(f"Phase 0: Web search phase failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Step 2 — Synthesise update via Claude
# ---------------------------------------------------------------------------

async def _synthesise_update(
    current_context: str,
    scraped_profile: str,
    search_snippets: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
    month: str,
) -> tuple[str, bool]:
    """
    Ask Claude to compare the gathered intelligence against the existing context
    and produce an updated context file if needed.

    Returns:
        (updated_context_text, was_changed)
        If no change is needed, returns (current_context, False).
    """
    gathered_intel = _build_intel_block(scraped_profile, search_snippets)

    if not gathered_intel.strip():
        logger.warning("Phase 0: No intelligence gathered — skipping synthesis")
        return current_context, False

    system_prompt = (
        "You are a precise editor responsible for keeping a company context document "
        "accurate and up to date.  Your only job is to incorporate verified new facts "
        "into the document — you must NOT change the tone, structure, or style, and "
        "you must NOT remove existing information unless it is explicitly contradicted "
        "by newer evidence.\n\n"
        "Rules:\n"
        "1. Only add or update information that is supported by the gathered intelligence.\n"
        "2. Preserve all existing sections and their headings exactly.\n"
        "3. Do not invent, speculate, or hallucinate facts.\n"
        "4. If you are unsure whether something counts as a material change, leave it unchanged.\n"
        "5. Dates in the document should reflect the real-world date of the event, "
        "not today's date.\n"
        "6. Output ONLY the complete updated context document — no preamble, "
        "no explanation, no markdown fences."
    )

    user_prompt = (
        f"## Current context document (config/betterwiser_context.txt)\n\n"
        f"{current_context}\n\n"
        f"---\n\n"
        f"## Gathered intelligence ({month})\n\n"
        f"{gathered_intel}\n\n"
        f"---\n\n"
        "Task:\n"
        "Review the gathered intelligence carefully.  Identify any facts that are "
        "NEW or UPDATED compared to the current context document (e.g. new job "
        "titles, new publications, new speaking engagements, new services, new "
        "partnerships, new client segments, changed strategic priorities).\n\n"
        "If you find material changes:\n"
        "  • Incorporate them into the appropriate sections of the document.\n"
        "  • Begin your response with the exact line: CHANGES_MADE: YES\n"
        "  • Then output the full updated document (nothing else).\n\n"
        "If there are NO material changes:\n"
        "  • Begin your response with the exact line: CHANGES_MADE: NO\n"
        "  • Then output the original document unchanged."
    )

    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=8192,
            temperature=0.3,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        full_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()

        if full_text.startswith("CHANGES_MADE: NO"):
            return current_context, False

        if full_text.startswith("CHANGES_MADE: YES"):
            # Strip the sentinel line to get the clean document
            updated = full_text[len("CHANGES_MADE: YES"):].lstrip("\n")
            return updated, True

        # Unexpected response format — treat as no change to be safe
        logger.warning(
            "Phase 0: Synthesis response did not include expected CHANGES_MADE sentinel. "
            "Treating as no change. Response preview: "
            + full_text[:200]
        )
        return current_context, False

    except Exception as e:
        logger.warning(f"Phase 0: Synthesis call failed: {e} — context unchanged")
        return current_context, False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_intel_block(scraped_profile: str, search_snippets: str) -> str:
    """Combine raw scraped content and search snippets into one block."""
    parts: list[str] = []
    if scraped_profile.strip():
        parts.append(f"### LinkedIn profile (scraped)\n\n{scraped_profile.strip()}")
    if search_snippets.strip():
        parts.append(f"### Web search findings\n\n{search_snippets.strip()}")
    return "\n\n".join(parts)


def _read_current_context() -> str:
    """Read the current context file, returning empty string if missing."""
    if not CONTEXT_FILE.exists():
        return ""
    return CONTEXT_FILE.read_text(encoding="utf-8")


def _backup_context(content: str, month: str) -> None:
    """Write a dated backup of the context file before overwriting."""
    CONTEXT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = CONTEXT_BACKUP_DIR / f"betterwiser_context_{month}_{timestamp}.txt"
    backup_path.write_text(content, encoding="utf-8")
    logger.info(f"Phase 0: Context backup written to {backup_path}")


def _stamp_path(month: str) -> Path:
    """Return the path of the monthly update stamp file."""
    return CONTEXT_BACKUP_DIR / f".updated_{month}"


def _already_updated_this_month(month: str) -> bool:
    """Return True if a stamp file for this month already exists."""
    return _stamp_path(month).exists()


def _write_update_stamp(month: str) -> None:
    """Write a stamp file so re-runs skip the update phase."""
    CONTEXT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp_path(month)
    stamp.write_text(
        datetime.now(tz=timezone.utc).isoformat(),
        encoding="utf-8",
    )
