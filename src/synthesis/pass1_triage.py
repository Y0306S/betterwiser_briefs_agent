"""
Pass 1: Triage and sorting.

Sorts event clusters by authority tier, ensures every source is accounted for,
flags coverage gaps, and truncates to track-specific item count limits.

Input:  list[EventCluster] (from Pass 0)
Output: list[EventCluster] (sorted, truncated)
"""

from __future__ import annotations

import logging

from src.schemas import BriefingTrack, EventCluster, SourceTier
from src.utils.authority import classify_url

logger = logging.getLogger(__name__)

# Default item count limits by track (overridden by config)
_DEFAULT_LIMITS = {
    BriefingTrack.A: (10, 15),
    BriefingTrack.B: (6, 8),
    BriefingTrack.C: (5, 20),
}


def triage_clusters(
    clusters: list[EventCluster],
    track: BriefingTrack,
    item_count_min: int | None = None,
    item_count_max: int | None = None,
) -> list[EventCluster]:
    """
    Sort clusters by authority tier and truncate to track limits.

    Sorting priority:
    1. Clusters where representative URL is tier_1 (government/official)
    2. Clusters where representative URL is tier_2 (major publications)
    3. Clusters with multiple corroborating sources (higher duplicate_count)
    4. All others

    Args:
        clusters: Output of pass0_cluster.
        track: Current briefing track.
        item_count_min: Minimum items to include (warn if fewer available).
        item_count_max: Maximum items to include (truncate at this count).

    Returns:
        Sorted and truncated list of EventCluster.
    """
    if not clusters:
        logger.warning(f"Track {track.value} triage: no clusters to sort")
        return []

    default_min, default_max = _DEFAULT_LIMITS.get(track, (5, 15))
    min_count = item_count_min or default_min
    max_count = item_count_max or default_max

    # Classify each cluster's representative URL tier
    def cluster_sort_key(cluster: EventCluster) -> tuple:
        rep_url = cluster.member_urls[0] if cluster.member_urls else ""
        tier = classify_url(rep_url)
        tier_rank = {
            SourceTier.TIER_1: 0,
            SourceTier.TIER_2: 1,
            SourceTier.TIER_3: 2,
        }.get(tier, 3)
        corroboration_bonus = -min(cluster.duplicate_count, 5)  # more sources = higher rank
        is_new = 0 if cluster.is_new_entrant else 1
        return (tier_rank, is_new, corroboration_bonus)

    sorted_clusters = sorted(clusters, key=cluster_sort_key)

    available = len(sorted_clusters)
    if available < min_count:
        logger.warning(
            f"Track {track.value}: only {available} clusters available "
            f"(minimum target: {min_count}). Consider adding more sources."
        )

    # Truncate to maximum
    result = sorted_clusters[:max_count]

    logger.info(
        f"Track {track.value} triage: {available} clusters → "
        f"{len(result)} selected (max={max_count})"
    )

    # Log coverage gaps
    _check_coverage_gaps(result, track)

    return result


def _check_coverage_gaps(clusters: list[EventCluster], track: BriefingTrack) -> None:
    """Log warnings for expected sources that appear to be missing."""
    if track != BriefingTrack.A:
        return

    expected_vendors = ["harvey", "legora", "luminance", "spellbook", "spotdraft", "clio"]
    cluster_text = " ".join(c.theme.lower() for c in clusters)

    missing = [v for v in expected_vendors if v not in cluster_text]
    if missing:
        logger.info(
            f"Track A: no clusters found for vendor(s): {', '.join(missing)}. "
            f"May have been quiet this month or sources unavailable."
        )
