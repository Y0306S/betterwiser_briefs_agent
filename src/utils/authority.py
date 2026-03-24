"""
Source authority tier classification and sorting.

Tier 1 = official government/regulator sources (highest credibility)
Tier 2 = reputable publications and major consulting firms
Tier 3 = everything else

Tiers are loaded from config/briefing_config.yaml at import time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml

from src.schemas import BriefingItem, SourceTier

logger = logging.getLogger(__name__)

# Cached tier lists loaded from config
_tier_1_domains: list[str] = []
_tier_2_domains: list[str] = []
_config_loaded: bool = False


def _load_config() -> None:
    global _tier_1_domains, _tier_2_domains, _config_loaded
    if _config_loaded:
        return
    config_path = Path("config/briefing_config.yaml")
    if not config_path.exists():
        logger.warning(f"Config not found at {config_path}, using empty tier lists")
        _config_loaded = True
        return
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    tiers = config.get("authority_tiers", {})
    _tier_1_domains = tiers.get("tier_1", [])
    _tier_2_domains = tiers.get("tier_2", [])
    _config_loaded = True
    logger.debug(
        f"Loaded authority tiers: {len(_tier_1_domains)} tier_1, "
        f"{len(_tier_2_domains)} tier_2 domains"
    )


def classify_url(url: str) -> SourceTier:
    """
    Classify a URL's authority tier by checking its domain against config lists.

    Args:
        url: Full URL string.

    Returns:
        SourceTier enum value.
    """
    _load_config()
    if not url:
        return SourceTier.TIER_3

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip("www.")
    except Exception:
        return SourceTier.TIER_3

    for tier_1 in _tier_1_domains:
        if domain == tier_1 or domain.endswith("." + tier_1):
            return SourceTier.TIER_1

    for tier_2 in _tier_2_domains:
        if domain == tier_2 or domain.endswith("." + tier_2):
            return SourceTier.TIER_2

    return SourceTier.TIER_3


def sort_by_authority(items: list[BriefingItem]) -> list[BriefingItem]:
    """
    Sort a list of BriefingItems by authority tier descending (tier_1 first).
    Within the same tier, preserve original order.
    """
    tier_order = {
        SourceTier.TIER_1: 0,
        SourceTier.TIER_2: 1,
        SourceTier.TIER_3: 2,
    }
    return sorted(items, key=lambda item: tier_order.get(item.tier, 3))


def get_tier_label(tier: SourceTier) -> str:
    """Human-readable tier label."""
    labels = {
        SourceTier.TIER_1: "Primary (Official/Government)",
        SourceTier.TIER_2: "Tier 1 Secondary (Major Publications)",
        SourceTier.TIER_3: "Tier 2 Secondary (Other)",
    }
    return labels.get(tier, "Unknown")
