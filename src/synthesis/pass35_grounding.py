"""
Pass 3.5: Programmatic grounding verification.

Uses regex pattern matching + thefuzz fuzzy matching to verify that
every number, date, and named entity in the briefing appears in the
source corpus. Complements the Citation API check in Pass 3.

Target: >95% pass rate. If below threshold, sets held_for_review=True
but does NOT stop the pipeline.

Input:  SynthesisResult + GatheredData
Output: (SynthesisResult, GroundingReport)
"""

from __future__ import annotations

import logging

from src.schemas import (
    BriefingItem,
    GatheredData,
    GroundingReport,
    SynthesisResult,
)
from src.utils.grounding import run_grounding_pass

logger = logging.getLogger(__name__)


def run_grounding_verification(
    synthesis: SynthesisResult,
    gathered: GatheredData,
    grounding_threshold: float = 0.95,
    fuzzy_threshold: int = 80,
) -> tuple[SynthesisResult, GroundingReport]:
    """
    Run the programmatic grounding verification pass.

    Args:
        synthesis: Output from Pass 3 (or Pass 2 if Pass 3 was skipped).
        gathered: All gathered data containing raw source texts.
        grounding_threshold: Minimum acceptable pass rate (0.0-1.0).
        fuzzy_threshold: Minimum fuzz score for a claim to be considered grounded.

    Returns:
        (updated_synthesis, grounding_report) tuple.
    """
    track = synthesis.track

    # Build source corpus: all scraped content + article snippets
    source_texts = _build_source_corpus(gathered)

    if not source_texts:
        logger.warning(
            f"Track {track.value}: Pass 3.5 — no source texts for grounding. "
            f"Skipping grounding verification."
        )
        report = GroundingReport(
            total_claims=0,
            grounded_claims=0,
            pass_rate=1.0,
            below_threshold=False,
        )
        if 35 not in synthesis.pass_completed:
            synthesis.pass_completed.append(35)
        return synthesis, report

    # If items are populated from a previous pass, use them
    # Otherwise, create minimal items from the raw HTML for grounding
    items = synthesis.items
    if not items:
        items = _extract_items_from_html(synthesis)

    if not items:
        logger.info(f"Track {track.value}: Pass 3.5 — no items to ground-check")
        report = GroundingReport(
            total_claims=0,
            grounded_claims=0,
            pass_rate=1.0,
            below_threshold=False,
        )
        if 35 not in synthesis.pass_completed:
            synthesis.pass_completed.append(35)
        return synthesis, report

    logger.info(
        f"Track {track.value}: Pass 3.5 — grounding {len(items)} items "
        f"against {len(source_texts)} source texts"
    )

    report = run_grounding_pass(
        items=items,
        source_texts=source_texts,
        threshold=fuzzy_threshold,
        grounding_threshold=grounding_threshold,
    )

    if report.below_threshold:
        logger.warning(
            f"Track {track.value}: GROUNDING BELOW THRESHOLD "
            f"({report.pass_rate:.1%} < {grounding_threshold:.1%}). "
            f"Briefing will be held for human review."
        )

    if 35 not in synthesis.pass_completed:
        synthesis.pass_completed.append(35)
    return synthesis, report


def _build_source_corpus(gathered: GatheredData) -> list[str]:
    """Build a flat list of source text strings for grounding checks."""
    texts: list[str] = []

    # Scraped page content
    for source in gathered.scraped_sources:
        if source.content and not source.error:
            texts.append(source.content)

    # Email body text
    for email in gathered.email_sources:
        if email.body_text:
            texts.append(email.body_text)
        for att in email.attachments:
            if att.extracted_text and not att.error:
                texts.append(att.extracted_text)

    # Article snippets
    for article in gathered.discovered_articles:
        if article.snippet:
            texts.append(article.snippet)

    return texts


def _extract_items_from_html(synthesis: SynthesisResult) -> list[BriefingItem]:
    """
    Minimal item extraction from raw HTML for grounding when structured
    items haven't been populated yet.
    """
    import re
    from src.schemas import SourceTier

    items: list[BriefingItem] = []
    html = synthesis.raw_html

    # Extract bullet-point entries or paragraph summaries
    # Match patterns like "• On [date], [entity] [did something]..."
    patterns = [
        re.compile(r"[•\-]\s*(.{30,300})", re.DOTALL),
        re.compile(r"<li[^>]*>(.{30,300})</li>", re.DOTALL | re.IGNORECASE),
        re.compile(r"<p[^>]*>(.{30,300})</p>", re.DOTALL | re.IGNORECASE),
    ]

    seen: set[str] = set()
    for pattern in patterns:
        for match in pattern.finditer(html):
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if text and text not in seen and len(text) > 30:
                seen.add(text)
                items.append(BriefingItem(
                    item_id=f"grounding_{len(items)}",
                    track=synthesis.track,
                    heading=text[:80],
                    summary=text[:300],
                    url="",
                    tier=SourceTier.TIER_3,
                ))

    return items[:30]  # cap for performance
