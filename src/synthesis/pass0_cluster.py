"""
Pass 0: Cluster and deduplication.

Groups sources by event, removes duplicates, loads trend data from history,
auto-detects hot vendors (Track A), and identifies dominant themes (Track C).

Input:  GatheredData
Output: list[EventCluster]
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urlparse

from src.schemas import (
    BriefingTrack,
    DiscoveredArticle,
    EventCluster,
    GatheredData,
    ScrapedSource,
)

logger = logging.getLogger(__name__)

try:
    from thefuzz import fuzz
    _fuzz_available = True
except ImportError:
    _fuzz_available = False
    logger.warning("thefuzz not installed — title deduplication will use exact matching only")


def cluster_and_dedup(
    gathered: GatheredData,
    track: BriefingTrack,
    title_similarity_threshold: int = 85,
) -> list[EventCluster]:
    """
    Cluster gathered sources by event and remove duplicates.

    Algorithm:
    1. Collect all sources (scraped + discovered) relevant to this track.
    2. URL-normalise and deduplicate exact duplicates.
    3. Title-similarity group related articles (same event, different sources).
    4. Build EventCluster for each group.
    5. Annotate hot vendors (Track A) and dominant themes (Track C).

    Args:
        gathered: All gathered data from Phase 2.
        track: Which track to cluster for.
        title_similarity_threshold: Fuzz score above which titles are considered duplicates.

    Returns:
        List of EventCluster objects.
    """
    # Collect all candidate sources for this track
    candidates = _collect_candidates(gathered, track)

    if not candidates:
        logger.warning(f"No sources found for Track {track.value} clustering")
        return []

    logger.info(f"Track {track.value}: clustering {len(candidates)} candidates")

    # Step 1: URL deduplication
    url_deduped = _dedup_by_url(candidates)
    logger.debug(f"After URL dedup: {len(url_deduped)} unique sources")

    # Step 2: Title similarity grouping
    clusters = _group_by_title_similarity(url_deduped, title_similarity_threshold)
    logger.debug(f"After title grouping: {len(clusters)} clusters")

    # Step 3: Track-specific annotations
    if track == BriefingTrack.A:
        clusters = _annotate_hot_vendors(clusters, gathered.historical_context)
    elif track == BriefingTrack.C:
        clusters = _annotate_themes(clusters)

    logger.info(f"Track {track.value}: {len(clusters)} event clusters")
    return clusters


def _collect_candidates(
    gathered: GatheredData,
    track: BriefingTrack,
) -> list[dict]:
    """
    Build a flat list of {url, title, snippet, tier} dicts for all sources
    relevant to the specified track.
    """
    candidates: list[dict] = []

    # From scraped sources (all are implicitly relevant to the tracks that scraped them)
    for source in gathered.scraped_sources:
        if source.error or not source.content:
            continue
        candidates.append({
            "url": source.url,
            "title": source.title or _url_to_title(source.url),
            "snippet": source.content[:300],
            "tier": source.tier,
            "source_type": "scraped",
        })

    # From discovered articles (filtered by track)
    for article in gathered.discovered_articles:
        if article.track == track:
            candidates.append({
                "url": article.url,
                "title": article.title,
                "snippet": article.snippet,
                "tier": article.tier,
                "source_type": "discovered",
                "discovery_wave": article.discovery_wave,
            })

    # From email links (harvest links as potential sources)
    for email in gathered.email_sources:
        for link in email.extracted_links[:20]:  # cap per-email links
            candidates.append({
                "url": link,
                "title": link,
                "snippet": "",
                "tier": "tier_3",
                "source_type": "email_link",
            })

    return candidates


def _dedup_by_url(candidates: list[dict]) -> list[dict]:
    """Remove exact URL duplicates, normalising common variations."""
    seen: dict[str, dict] = {}
    for c in candidates:
        normalised = _normalise_url(c["url"])
        if normalised not in seen:
            seen[normalised] = c
        else:
            # Prefer higher tier or richer snippet when merging
            existing = seen[normalised]
            if c.get("snippet") and len(c["snippet"]) > len(existing.get("snippet", "")):
                seen[normalised] = c
    return list(seen.values())


def _group_by_title_similarity(
    candidates: list[dict],
    threshold: int,
) -> list[EventCluster]:
    """
    Group candidates into event clusters by title similarity.
    Uses greedy clustering: first unassigned item starts a new cluster.
    """
    assigned = [False] * len(candidates)
    clusters: list[EventCluster] = []

    for i, candidate in enumerate(candidates):
        if assigned[i]:
            continue

        # Start a new cluster with this candidate as representative
        group = [candidate]
        assigned[i] = True

        if _fuzz_available:
            for j in range(i + 1, len(candidates)):
                if assigned[j]:
                    continue
                score = fuzz.token_sort_ratio(
                    candidate["title"].lower(),
                    candidates[j]["title"].lower(),
                )
                if score >= threshold:
                    group.append(candidates[j])
                    assigned[j] = True

        # Representative = first / highest-tier item in group
        rep = group[0]
        cluster_id = hashlib.md5(rep["url"].encode()).hexdigest()[:8]

        clusters.append(EventCluster(
            cluster_id=cluster_id,
            theme=rep["title"],
            member_urls=[g["url"] for g in group],
            representative_snippet=rep["snippet"][:400],
            duplicate_count=max(0, len(group) - 1),
        ))

    return clusters


def _annotate_hot_vendors(
    clusters: list[EventCluster],
    historical_context: str | None,
) -> list[EventCluster]:
    """
    Mark clusters that represent new vendor entrants (Track A).
    A vendor is "new" if it doesn't appear in historical context.
    """
    if not historical_context:
        return clusters

    history_lower = historical_context.lower()
    # Common stopwords to skip when picking a representative vendor token
    _STOPWORDS = {
        "the", "a", "an", "on", "in", "at", "of", "to", "for",
        "and", "or", "is", "are", "was", "were", "with", "from",
        "new", "ai", "legal", "law", "firm", "global", "update",
    }

    for cluster in clusters:
        # Use the longest non-stopword token as a proxy for the vendor/entity name.
        # This avoids picking common words like "Singapore" or "On" as the key.
        words = [w.lower().strip(".,;:()[]") for w in cluster.theme.split() if w]
        candidates = [w for w in words if w not in _STOPWORDS and len(w) > 3]
        vendor_name = max(candidates, key=len) if candidates else (words[0] if words else "")

        if vendor_name and vendor_name not in history_lower:
            cluster.is_new_entrant = True
        # Check for repeat mentions
        count = history_lower.count(vendor_name) if vendor_name else 0
        if count >= 2:
            cluster.trend_annotation = f"Mentioned in {count} prior month(s)"

    return clusters


def _annotate_themes(clusters: list[EventCluster]) -> list[EventCluster]:
    """
    Identify dominant themes across clusters (Track C).
    Simple keyword-based theme detection.
    """
    theme_keywords = {
        "AI Workforce Transformation": ["workforce", "jobs", "skills", "talent", "reskill", "upskill"],
        "Change Management": ["change management", "adoption", "culture", "resistance", "transformation"],
        "Strategic AI Adoption": ["strategy", "ROI", "value", "productivity", "competitive", "enterprise"],
        "AI Governance & Risk": ["governance", "risk", "compliance", "responsible", "bias", "ethics"],
        "Pilot to Production": ["pilot", "scale", "production", "deployment", "implementation"],
    }

    for cluster in clusters:
        text_lower = (cluster.theme + " " + cluster.representative_snippet).lower()
        for theme, keywords in theme_keywords.items():
            if any(kw in text_lower for kw in keywords):
                cluster.trend_annotation = theme
                break

    return clusters


def _normalise_url(url: str) -> str:
    """Normalise a URL for deduplication (remove scheme, trailing slash, query params)."""
    try:
        parsed = urlparse(url.lower())
        # Remove tracking params
        path = parsed.path.rstrip("/")
        return f"{parsed.netloc}{path}"
    except Exception:
        return url.lower().rstrip("/")


def _url_to_title(url: str) -> str:
    """Convert a URL to a readable title fallback."""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if path:
            segment = path.split("/")[-1]
            return segment.replace("-", " ").replace("_", " ").title()
        return parsed.netloc
    except Exception:
        return url
