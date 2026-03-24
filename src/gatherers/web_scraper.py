"""
Tiered web scraper — Phase 2, Sub-pipeline B.

Tier 1: Jina Reader (free, no API key) — https://r.jina.ai/{url}
Tier 2: Spider API (pay-as-you-go, requires SPIDER_API_KEY)
Tier 3: Crawl4AI (self-hosted via playwright, no API key)

Falls back gracefully when a tier is unavailable or fails.
Returns ScrapedSource with error field on total failure rather than raising.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

import httpx

from src.schemas import ScrapedSource, SourceTier
from src.utils.authority import classify_url
from src.utils.retry import async_retry

logger = logging.getLogger(__name__)

# Jina Reader base URL
JINA_BASE = "https://r.jina.ai"

# Minimum useful word count — below this, try next tier
MIN_WORD_COUNT = 100


async def scrape_url(url: str) -> ScrapedSource:
    """
    Scrape a single URL using tiered fallback strategy.

    Args:
        url: The URL to scrape.

    Returns:
        ScrapedSource with content populated (or error field set on failure).
    """
    tier = classify_url(url)

    # Tier 1: Jina Reader (free, robust, handles most pages)
    result = await _try_jina(url)
    if result and result.word_count >= MIN_WORD_COUNT:
        result.tier = tier
        return result

    # Tier 2: Spider (requires API key, better JS rendering)
    spider_key = os.getenv("SPIDER_API_KEY")
    if spider_key:
        result = await _try_spider(url, spider_key)
        if result and result.word_count >= MIN_WORD_COUNT:
            result.tier = tier
            return result
    else:
        logger.debug(f"SPIDER_API_KEY not set — skipping Spider tier for {url}")

    # Tier 3: Crawl4AI (local playwright, slowest but most capable)
    result = await _try_crawl4ai(url)
    if result:
        result.tier = tier
        return result

    # All tiers failed
    logger.error(f"All scrapers failed for {url}")
    return ScrapedSource(
        url=url,
        title="[Scrape failed]",
        content="",
        tier=tier,
        scraper_used="none",
        word_count=0,
        error="All scraping tiers exhausted",
    )


async def scrape_urls(urls: list[str], concurrency: int = 5) -> list[ScrapedSource]:
    """
    Scrape multiple URLs with bounded concurrency.

    Args:
        urls: List of URLs to scrape.
        concurrency: Max simultaneous scraping tasks.

    Returns:
        List of ScrapedSource objects (one per input URL).
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_scrape(url: str) -> ScrapedSource:
        async with semaphore:
            return await scrape_url(url)

    tasks = [bounded_scrape(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output: list[ScrapedSource] = []
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            logger.error(f"scrape_url raised unexpected exception for {url}: {result}")
            output.append(ScrapedSource(
                url=url, title="[Error]", content="", tier=SourceTier.TIER_3,
                scraper_used="none", word_count=0, error=str(result),
            ))
        else:
            output.append(result)  # type: ignore[arg-type]

    successful = sum(1 for s in output if not s.error)
    logger.info(f"Scraped {len(urls)} URLs: {successful} successful, {len(urls)-successful} failed")
    return output


@async_retry(max_attempts=3, base_delay=1.0, exceptions=(httpx.RequestError, httpx.TimeoutException))
async def _try_jina(url: str) -> ScrapedSource | None:
    """Attempt scraping via Jina Reader API."""
    jina_url = f"{JINA_BASE}/{url}"
    headers = {
        "Accept": "application/json",
        "X-Return-Format": "markdown",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(jina_url, headers=headers)
            response.raise_for_status()
            data = response.json()

        content = data.get("data", {}).get("content", "") or data.get("content", "")
        title = data.get("data", {}).get("title", "") or data.get("title", "") or _extract_title(url)
        word_count = len(content.split()) if content else 0

        if word_count < 50:
            logger.debug(f"Jina returned too little content for {url} ({word_count} words)")
            return None

        logger.debug(f"Jina scraped {url}: {word_count} words")
        return ScrapedSource(
            url=url,
            title=title,
            content=content,
            tier=SourceTier.TIER_3,  # will be overridden by caller
            scraper_used="jina",
            word_count=word_count,
        )

    except httpx.HTTPStatusError as e:
        logger.debug(f"Jina HTTP error for {url}: {e.response.status_code}")
        return None
    except Exception as e:
        logger.debug(f"Jina failed for {url}: {type(e).__name__}: {e}")
        return None


async def _try_spider(url: str, api_key: str) -> ScrapedSource | None:
    """Attempt scraping via Spider API."""
    try:
        # Use httpx directly since spider-client SDK may not be available
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": url,
            "return_format": "markdown",
            "readability": True,
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                "https://api.spider.cloud/scrape",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        # Spider returns list or dict
        if isinstance(data, list) and data:
            item = data[0]
        elif isinstance(data, dict):
            item = data
        else:
            return None

        content = item.get("content", "") or item.get("markdown", "")
        title = item.get("metadata", {}).get("title", "") or _extract_title(url)
        word_count = len(content.split()) if content else 0

        if word_count < 50:
            return None

        logger.debug(f"Spider scraped {url}: {word_count} words")
        return ScrapedSource(
            url=url,
            title=title,
            content=content,
            tier=SourceTier.TIER_3,
            scraper_used="spider",
            word_count=word_count,
        )

    except Exception as e:
        logger.debug(f"Spider failed for {url}: {type(e).__name__}: {e}")
        return None


async def _try_crawl4ai(url: str) -> ScrapedSource | None:
    """Attempt scraping via Crawl4AI (requires playwright installed)."""
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

        config = CrawlerRunConfig(
            word_count_threshold=50,
            exclude_external_links=False,
            remove_overlay_elements=True,
        )

        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(url=url, config=config)

        if not result.success:
            logger.debug(f"Crawl4AI failed for {url}: {result.error_message}")
            return None

        content = result.markdown or result.cleaned_html or ""
        word_count = len(content.split()) if content else 0

        if word_count < 50:
            return None

        logger.debug(f"Crawl4AI scraped {url}: {word_count} words")
        return ScrapedSource(
            url=url,
            title=result.metadata.get("title", _extract_title(url)) if result.metadata else _extract_title(url),
            content=content,
            tier=SourceTier.TIER_3,
            scraper_used="crawl4ai",
            word_count=word_count,
        )

    except ImportError:
        logger.debug("crawl4ai not installed — Tier 3 scraping unavailable")
        return None
    except Exception as e:
        logger.debug(f"Crawl4AI failed for {url}: {type(e).__name__}: {e}")
        return None


def _extract_title(url: str) -> str:
    """Extract a human-readable title from a URL as a last resort."""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if path:
            return path.split("/")[-1].replace("-", " ").replace("_", " ").title()
        return parsed.netloc
    except Exception:
        return url
