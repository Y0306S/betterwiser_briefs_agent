"""
RSS feed reader — Phase 2, Sub-pipeline F.

Ingests curated RSS/Atom feeds from Track A/B publications and converts each
feed entry into a DiscoveredArticle for downstream scraping and synthesis.

This sub-pipeline is fast (HTTP + XML parse, no AI calls) and runs in parallel
with the main gathering phases.  It is the highest-signal-to-cost source of
fresh Track A and B content — feeds from Artificial Lawyer, LawNext, and Legal
Futures publish within minutes of new articles going live.

Feed list is configured in config/briefing_config.yaml under the `rss_feeds` key:

  rss_feeds:
    - url: "https://www.artificiallawyer.com/feed/"
      track: A
      source_name: "Artificial Lawyer"
      tier: tier_2
    - url: "https://lawnext.com/feed/"
      track: A
      source_name: "LawNext"
      tier: tier_2
    ...
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from src.schemas import BriefingTrack, DiscoveredArticle, SourceTier

logger = logging.getLogger(__name__)

# HTTP headers that make feed servers happy
_HEADERS = {
    "User-Agent": "BetterWiser-BriefingAgent/1.0 (+https://betterwiser.com) feed-reader",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.9",
}

# How many characters of article description to use as the snippet
_SNIPPET_MAX = 500

# Namespace map for Atom feeds
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


async def read_feeds(
    feed_configs: list[dict],
    month: str,
    concurrency: int = 6,
    timeout: float = 15.0,
) -> list[DiscoveredArticle]:
    """
    Fetch and parse all configured RSS/Atom feeds, returning articles published
    in the target month.

    Args:
        feed_configs: List of feed config dicts from briefing_config.yaml.
                      Each dict must have `url` and `track`; optional:
                      `source_name`, `tier`, `max_age_days`.
        month:        Target month "YYYY-MM" — only articles from this month
                      (or within the last 45 days if date parsing fails) are kept.
        concurrency:  Max simultaneous HTTP connections.
        timeout:      Per-feed timeout in seconds.

    Returns:
        Deduplicated list of DiscoveredArticles.
    """
    if not feed_configs:
        logger.debug("RSS reader: no feeds configured")
        return []

    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_one(cfg: dict) -> list[DiscoveredArticle]:
        async with semaphore:
            return await _fetch_feed(cfg, month, timeout)

    results = await asyncio.gather(
        *[fetch_one(cfg) for cfg in feed_configs],
        return_exceptions=True,
    )

    articles: list[DiscoveredArticle] = []
    seen_urls: set[str] = set()

    for cfg, result in zip(feed_configs, results):
        if isinstance(result, Exception):
            logger.warning(
                f"RSS reader: feed fetch failed for {cfg.get('url', '?')}: {result}"
            )
            continue
        for article in result:  # type: ignore[union-attr]
            if article.url not in seen_urls:
                seen_urls.add(article.url)
                articles.append(article)

    logger.info(
        f"RSS reader: {len(articles)} articles from {len(feed_configs)} feeds "
        f"for {month}"
    )
    return articles


async def _fetch_feed(
    cfg: dict,
    month: str,
    timeout: float,
) -> list[DiscoveredArticle]:
    """Fetch and parse a single RSS or Atom feed."""
    url = cfg["url"]
    try:
        track_str = cfg.get("track", "A")
        track = BriefingTrack(track_str)
    except ValueError:
        logger.warning(f"RSS reader: unknown track '{cfg.get('track')}' for {url}; defaulting to A")
        track = BriefingTrack.A

    tier_str = cfg.get("tier", "tier_2")
    try:
        tier = SourceTier(tier_str)
    except ValueError:
        tier = SourceTier.TIER_2

    source_name = cfg.get("source_name", "")

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.text

    except Exception as e:
        logger.debug(f"RSS reader: HTTP error for {url}: {e}")
        return []

    try:
        return _parse_feed(content, url, track, tier, source_name, month)
    except Exception as e:
        logger.debug(f"RSS reader: parse error for {url}: {e}")
        return []


def _parse_feed(
    content: str,
    feed_url: str,
    track: BriefingTrack,
    tier: SourceTier,
    source_name: str,
    month: str,
) -> list[DiscoveredArticle]:
    """
    Parse RSS 2.0 or Atom 1.0 XML into DiscoveredArticles.

    Filters to articles published in the target month.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.debug(f"RSS reader: XML parse error: {e}")
        return []

    articles: list[DiscoveredArticle] = []

    # Detect feed type
    tag = root.tag.lower()
    if "feed" in tag:
        # Atom 1.0
        articles = _parse_atom(root, track, tier, source_name, month)
    else:
        # RSS 2.0 — channel is the first child of <rss>
        channel = root.find("channel")
        if channel is None:
            channel = root  # some feeds omit <rss> wrapper
        articles = _parse_rss(channel, track, tier, source_name, month)

    return articles


def _parse_rss(
    channel: ET.Element,
    track: BriefingTrack,
    tier: SourceTier,
    source_name: str,
    month: str,
) -> list[DiscoveredArticle]:
    """Parse RSS 2.0 <item> elements."""
    articles: list[DiscoveredArticle] = []

    # Use channel title as source_name fallback
    if not source_name:
        title_el = channel.find("title")
        source_name = title_el.text.strip() if title_el is not None and title_el.text else ""

    for item in channel.findall("item"):
        link_el = item.find("link")
        title_el = item.find("title")
        desc_el = item.find("description")
        pubdate_el = item.find("pubDate")

        url = (link_el.text or "").strip() if link_el is not None else ""
        if not url or not url.startswith(("http://", "https://")):
            continue

        title = (title_el.text or "").strip() if title_el is not None else ""
        description = (desc_el.text or "").strip() if desc_el is not None else ""

        # Strip HTML tags from description
        snippet = _strip_html(description)[:_SNIPPET_MAX]

        pub_date_str = (pubdate_el.text or "").strip() if pubdate_el is not None else ""
        published_date = _parse_rss_date(pub_date_str)

        if not _is_in_month(published_date, month):
            continue

        articles.append(DiscoveredArticle(
            url=url,
            title=title,
            snippet=snippet,
            source_name=source_name,
            published_date=published_date,
            track=track,
            tier=tier,
            discovered_via="rss",
        ))

    return articles


def _parse_atom(
    root: ET.Element,
    track: BriefingTrack,
    tier: SourceTier,
    source_name: str,
    month: str,
) -> list[DiscoveredArticle]:
    """Parse Atom 1.0 <entry> elements."""
    articles: list[DiscoveredArticle] = []

    # Strip namespace prefix from all tags for uniform access
    def _detag(el: ET.Element) -> str:
        return re.sub(r"\{[^}]+\}", "", el.tag)

    import re

    # Source name from feed <title>
    if not source_name:
        for child in root:
            if _detag(child) == "title" and child.text:
                source_name = child.text.strip()
                break

    for entry in root:
        if _detag(entry) != "entry":
            continue

        url = ""
        title = ""
        snippet = ""
        published_date: Optional[str] = None

        for child in entry:
            child_tag = _detag(child)

            if child_tag == "link":
                rel = child.get("rel", "alternate")
                href = child.get("href", "")
                if rel in ("alternate", "") and href.startswith(("http://", "https://")):
                    url = href

            elif child_tag == "title" and child.text:
                title = child.text.strip()

            elif child_tag in ("summary", "content") and child.text and not snippet:
                snippet = _strip_html(child.text)[:_SNIPPET_MAX]

            elif child_tag in ("published", "updated") and child.text and not published_date:
                published_date = child.text.strip()[:10]  # ISO date prefix

        if not url or not url.startswith(("http://", "https://")):
            continue

        if not _is_in_month(published_date, month):
            continue

        articles.append(DiscoveredArticle(
            url=url,
            title=title,
            snippet=snippet,
            source_name=source_name,
            published_date=published_date,
            track=track,
            tier=tier,
            discovered_via="rss",
        ))

    return articles


def _parse_rss_date(date_str: str) -> Optional[str]:
    """Parse RFC 2822 RSS date string to ISO date string "YYYY-MM-DD"."""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.date().isoformat()
    except Exception:
        # Try ISO format directly
        try:
            return date_str[:10]  # take first 10 chars: "YYYY-MM-DD"
        except Exception:
            return None


def _is_in_month(date_str: Optional[str], month: str) -> bool:
    """
    Return True if date_str falls within the target month.

    If date_str is None (unparseable), return True so we don't silently
    drop entries just because they lack a pub date.
    """
    if date_str is None:
        return True
    # date_str should be "YYYY-MM-DD" at this point
    return date_str.startswith(month)


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string (simple, no external deps needed)."""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
