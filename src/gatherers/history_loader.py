"""
Historical data loader — Phase 2, Sub-pipeline E.

Loads structured outputs from previous months' runs to provide
cross-month trend context to the synthesis pipeline.

No external API calls — pure filesystem operations.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def load_previous_month(runs_dir: str, current_month: str) -> Optional[str]:
    """
    Load synthesised content from the previous 1-3 months' runs.

    Searches the runs_dir for completed runs from prior months and
    returns a condensed text summary for injection into synthesis prompts
    as historical context.

    Args:
        runs_dir: Path to the runs/ directory.
        current_month: Current run month in "YYYY-MM" format.

    Returns:
        Multi-line summary string, or None if no prior runs exist.
    """
    runs_path = Path(runs_dir)
    if not runs_path.exists():
        logger.info(f"Runs directory not found: {runs_dir}. No historical data.")
        return None

    prior_months = _get_prior_months(current_month, count=3)
    summaries: list[str] = []

    for month in prior_months:
        month_summary = _load_month_summary(runs_path, month)
        if month_summary:
            summaries.append(f"=== {month} ===\n{month_summary}")

    if not summaries:
        logger.info("No historical run data found. This may be the first run.")
        return None

    combined = "\n\n".join(summaries)
    logger.info(f"Loaded historical context from {len(summaries)} prior month(s)")
    return combined


def _load_month_summary(runs_path: Path, month: str) -> Optional[str]:
    """
    Load summary data for a specific month from any matching run directory.
    Prefers the most recent run for that month.
    """
    # Run dirs are named like "2026-02_run_20260301T080000"
    matching_dirs = sorted(
        [d for d in runs_path.iterdir() if d.is_dir() and d.name.startswith(month)],
        reverse=True,  # most recent first
    )

    if not matching_dirs:
        return None

    run_dir = matching_dirs[0]  # use most recent run for that month
    summary_parts: list[str] = []

    # Try to load synthesis HTML files for quick context
    delivery_dir = run_dir / "delivery"
    if delivery_dir.exists():
        for html_file in sorted(delivery_dir.glob("track_*.html")):
            track_id = html_file.stem.replace("track_", "")
            text = _extract_text_from_html_file(html_file)
            if text:
                # Truncate to avoid context bloat — first 500 chars per track
                summary_parts.append(f"Track {track_id}: {text[:500]}...")

    # Try to load structured synthesis JSON if available
    synthesis_dir = run_dir / "synthesis"
    if synthesis_dir.exists() and not summary_parts:
        for json_file in sorted(synthesis_dir.glob("synthesis_track_*.json")):
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)
                track = json_file.stem.replace("synthesis_track_", "")
                items = data.get("items", [])
                if items:
                    headings = [item.get("heading", "") for item in items[:5]]
                    summary_parts.append(
                        f"Track {track} top items: " + "; ".join(h for h in headings if h)
                    )
            except (json.JSONDecodeError, KeyError) as e:
                logger.debug(f"Could not parse {json_file}: {e}")

    if summary_parts:
        return "\n".join(summary_parts)
    return None


def _get_prior_months(current_month: str, count: int = 3) -> list[str]:
    """
    Return list of YYYY-MM strings for the `count` months preceding current_month.

    Examples:
        _get_prior_months("2026-03", 3) → ["2026-02", "2026-01", "2025-12"]
    """
    year, month = int(current_month[:4]), int(current_month[5:7])
    prior: list[str] = []
    for _ in range(count):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        prior.append(f"{year:04d}-{month:02d}")
    return prior


def _extract_text_from_html_file(path: Path) -> str:
    """Quick text extraction from a saved HTML briefing file."""
    try:
        import re
        content = path.read_text(encoding="utf-8")
        # Strip HTML tags
        text = re.sub(r"<[^>]+>", " ", content)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
    except Exception as e:
        logger.debug(f"Could not read {path}: {e}")
        return ""
