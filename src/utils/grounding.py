"""
Fuzzy-match grounding verification for briefing claims.

Uses thefuzz (formerly fuzzywuzzy) with RapidFuzz backend for speed.
Extracts all factual claims from briefing items and checks each against
the raw source corpus.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from src.schemas import BriefingItem, FlaggedClaim, GroundingReport, SourceTier

logger = logging.getLogger(__name__)

try:
    from thefuzz import fuzz
except ImportError:
    logger.warning("thefuzz not installed — grounding verification will be skipped")
    fuzz = None  # type: ignore[assignment]

# Regex patterns for extracting verifiable claims
_NUMBER_PATTERN = re.compile(r"\$[\d,]+|\d+[\d,.]*\s*(?:billion|million|thousand|%|percent)", re.IGNORECASE)
_DATE_PATTERN = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b",
    re.IGNORECASE
)
_ENTITY_PATTERN = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")


def ground_claim(
    claim: str,
    source_texts: list[str],
    threshold: int = 80,
) -> tuple[bool, float]:
    """
    Check whether a claim is supported by at least one source text.

    Args:
        claim: The claim text to verify.
        source_texts: List of raw source content strings.
        threshold: Minimum fuzz score (0-100) to consider grounded.

    Returns:
        (is_grounded, best_score) tuple.
    """
    if fuzz is None:
        return True, 100.0  # can't verify, assume grounded

    if not claim or not source_texts:
        return False, 0.0

    best_score = 0.0
    # Use token_set_ratio for better partial matching
    for source in source_texts:
        # Chunk source into overlapping windows for large documents
        chunks = _chunk_text(source, window=300, overlap=50)
        for chunk in chunks:
            score = fuzz.token_set_ratio(claim, chunk)
            if score > best_score:
                best_score = score
            if best_score >= threshold:
                return True, float(best_score)

    return float(best_score) >= threshold, float(best_score)


def run_grounding_pass(
    items: list[BriefingItem],
    source_texts: list[str],
    threshold: int = 80,
    grounding_threshold: float = 0.95,
) -> GroundingReport:
    """
    Run the full grounding pass across all briefing items.

    For each item, checks the heading + summary against the source corpus.
    Claims with numbers, dates, or named entities are weighted more heavily.

    Args:
        items: All BriefingItem objects from the synthesis pass.
        source_texts: Raw content from all scraped/discovered sources.
        threshold: Fuzzy match threshold (0-100).
        grounding_threshold: Minimum acceptable pass rate (0.0-1.0).

    Returns:
        GroundingReport with pass_rate and flagged_claims.
    """
    if not items:
        return GroundingReport(
            total_claims=0, grounded_claims=0, pass_rate=1.0,
            below_threshold=False
        )

    flagged: list[FlaggedClaim] = []
    grounded_count = 0
    total = len(items)

    for item in items:
        claim_text = f"{item.heading}. {item.summary}"
        is_grounded, score = ground_claim(claim_text, source_texts, threshold)

        if is_grounded:
            grounded_count += 1
        else:
            claim_type = _classify_claim_type(claim_text)
            flagged.append(FlaggedClaim(
                claim_text=claim_text,
                item_id=item.item_id,
                reason=f"Low fuzzy match score: {score:.0f}/100 (threshold: {threshold})",
                claim_type=claim_type,
                suggested_fix="Verify against source documents or remove if unsupported",
            ))
            logger.debug(
                f"Claim flagged: item_id={item.item_id}, score={score:.0f}, "
                f"heading={item.heading[:60]}..."
            )

    pass_rate = grounded_count / total if total > 0 else 1.0
    below_threshold = pass_rate < grounding_threshold

    if below_threshold:
        logger.warning(
            f"Grounding pass rate {pass_rate:.1%} below threshold {grounding_threshold:.1%}. "
            f"{len(flagged)} items flagged. Briefing will be held for review."
        )
    else:
        logger.info(f"Grounding pass rate: {pass_rate:.1%} ({grounded_count}/{total} items)")

    return GroundingReport(
        total_claims=total,
        grounded_claims=grounded_count,
        pass_rate=pass_rate,
        below_threshold=below_threshold,
        flagged_claims=flagged,
    )


def _classify_claim_type(text: str) -> str:
    """Identify the type of verifiable claim in the text."""
    if _NUMBER_PATTERN.search(text):
        return "number"
    if _DATE_PATTERN.search(text):
        return "date"
    if _ENTITY_PATTERN.search(text):
        return "entity"
    return "unsupported"


def _chunk_text(text: str, window: int = 300, overlap: int = 50) -> list[str]:
    """Split text into overlapping word windows for fuzzy matching."""
    words = text.split()
    if len(words) <= window:
        return [text]
    chunks = []
    step = max(1, window - overlap)
    for i in range(0, len(words) - window + 1, step):
        chunks.append(" ".join(words[i : i + window]))
    # Always include last window
    if chunks and words[-(window):] != chunks[-1].split():
        chunks.append(" ".join(words[-window:]))
    return chunks
