"""
Cross-track connector pass — runs after all three tracks have been synthesised.

Identifies entities (vendors, people, regulations, themes) that appear in more
than one track and injects brief cross-reference annotations into each relevant
SynthesisDraft, so readers can immediately see when something spans tracks.

Example annotation added to a Track A DraftBriefingItem:
  betterwiser_relevance += " [Also covered in Track B: EU AI Act obligations]"

Example annotation added to a Track B DraftBriefingItem:
  correction_note = "[Cross-track] Harvey AI (Track A vendor) is subject to this regulation"

This pass is non-destructive: it only appends to existing fields and never
removes or replaces any content.  If it fails entirely, the briefings are still
complete and deliverable.

Input:  dict[BriefingTrack, SynthesisResult] — all three tracks after Pass 3
Output: dict[BriefingTrack, SynthesisResult] — same structure, annotations added
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Optional

from src.schemas import (
    BriefingTrack,
    DraftBriefingItem,
    SynthesisDraft,
    SynthesisResult,
)

logger = logging.getLogger(__name__)

# Minimum token length to consider a heading a meaningful entity
_MIN_ENTITY_LEN = 4

# How many cross-reference annotations to add per item (cap for readability)
_MAX_ANNOTATIONS_PER_ITEM = 2


def annotate_cross_track(
    results: dict[BriefingTrack, SynthesisResult],
) -> dict[BriefingTrack, SynthesisResult]:
    """
    Identify shared entities across all three track drafts and inject
    cross-reference annotations.

    Args:
        results: Mapping of track → SynthesisResult (after Pass 3 factcheck).

    Returns:
        The same mapping with cross-reference annotations added in-place.
    """
    # Only operates on tracks that have a populated SynthesisDraft
    drafts: dict[BriefingTrack, SynthesisDraft] = {
        track: result.draft
        for track, result in results.items()
        if result.draft is not None
    }

    if len(drafts) < 2:
        logger.info("Cross-track pass: fewer than 2 structured drafts available — skipping")
        return results

    # Build an entity index: entity_token → list of (track, section_heading, item_heading)
    entity_index: dict[str, list[tuple[BriefingTrack, str, str]]] = defaultdict(list)

    for track, draft in drafts.items():
        for section in draft.sections:
            for item in section.items:
                for token in _extract_entities(item.heading):
                    entity_index[token].append((track, section.heading, item.heading))

    # Find tokens that appear in more than one track
    cross_track_entities: dict[str, list[tuple[BriefingTrack, str, str]]] = {
        token: mentions
        for token, mentions in entity_index.items()
        if len({m[0] for m in mentions}) > 1  # at least 2 distinct tracks
    }

    if not cross_track_entities:
        logger.info("Cross-track pass: no shared entities found across tracks")
        return results

    logger.info(
        f"Cross-track pass: found {len(cross_track_entities)} shared entities "
        f"across {len(drafts)} tracks"
    )

    # Inject annotations
    annotation_count = 0
    for track, draft in drafts.items():
        for section in draft.sections:
            for item in section.items:
                item_tokens = set(_extract_entities(item.heading))
                refs: list[str] = []

                for token in item_tokens:
                    if token not in cross_track_entities:
                        continue
                    # Find mentions in OTHER tracks
                    other_mentions = [
                        m for m in cross_track_entities[token]
                        if m[0] != track
                    ]
                    if not other_mentions:
                        continue

                    for other_track, other_section, _other_item in other_mentions[:2]:
                        ref = f"Track {other_track.value}: {_truncate(other_section, 40)}"
                        if ref not in refs:
                            refs.append(ref)

                    if len(refs) >= _MAX_ANNOTATIONS_PER_ITEM:
                        break

                if refs:
                    cross_ref = " | ".join(refs)
                    _append_cross_ref(item, cross_ref)
                    annotation_count += 1

    logger.info(
        f"Cross-track pass: added {annotation_count} cross-reference annotations"
    )
    return results


def _extract_entities(heading: str) -> list[str]:
    """
    Extract significant tokens from a heading for entity matching.

    Keeps multi-word capitalised phrases (proper nouns / entity names) and
    quoted strings.  Filters common stop words and short tokens.
    """
    entities: list[str] = []

    # Quoted strings (exact names/titles)
    for m in re.finditer(r'"([^"]{4,60})"', heading):
        entities.append(m.group(1).lower().strip())

    # Capitalised multi-word phrases (2+ consecutive title-case words)
    for m in re.finditer(r'(?:[A-Z][a-z]+\s+){1,4}[A-Z][a-z]+', heading):
        token = m.group(0).strip().lower()
        if len(token) >= _MIN_ENTITY_LEN and token not in _STOP_PHRASES:
            entities.append(token)

    # Single capitalised tokens that are at least 5 chars (brand names etc.)
    for m in re.finditer(r'\b[A-Z][a-z]{3,}\b', heading):
        token = m.group(0).lower()
        if token not in _STOP_WORDS:
            entities.append(token)

    return list(dict.fromkeys(entities))  # preserve order, deduplicate


def _append_cross_ref(item: DraftBriefingItem, cross_ref: str) -> None:
    """
    Append a cross-reference note to an item's betterwiser_relevance or
    correction_note field (whichever is most appropriate).
    """
    note = f"[See also: {cross_ref}]"

    if item.betterwiser_relevance:
        item.betterwiser_relevance = f"{item.betterwiser_relevance} {note}"
    elif item.correction_note:
        item.correction_note = f"{item.correction_note} {note}"
    else:
        # Use correction_note as a general annotation carrier
        item.correction_note = note


def _truncate(text: str, max_len: int) -> str:
    """Truncate a string with ellipsis if over max_len."""
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


# ---------------------------------------------------------------------------
# Stop word / phrase lists
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "the", "this", "that", "and", "for", "with", "from", "into", "onto",
    "over", "under", "their", "about", "after", "before", "during",
    "through", "between", "among", "within", "without", "against",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "track", "wave", "pass", "phase", "section", "item",
})

_STOP_PHRASES = frozenset({
    "new york", "hong kong", "united states", "united kingdom",
    "european union", "south east", "south east asia",
})
