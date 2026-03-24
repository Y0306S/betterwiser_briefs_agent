"""
Claude autonomous discovery — Phase 2, Sub-pipeline C.

Uses Claude's built-in web_search tool (web_search_20260209) to run
targeted queries per track and discover articles not in curated lists
or newsletters.

Estimated: 40–60 searches across all three tracks per run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import anthropic

from src.schemas import BriefingTrack, DiscoveredArticle, SourceTier
from src.utils.authority import classify_url
from src.utils.retry import async_retry

logger = logging.getLogger(__name__)

# Built-in Claude web search tool definition
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
}


async def discover_articles_all_tracks(
    tracks: list[BriefingTrack],
    month: str,
    queries_by_track: dict[str, list[str]],
    client: anthropic.AsyncAnthropic,
    model_id: str = "claude-opus-4-6",
) -> list[DiscoveredArticle]:
    """
    Run discovery searches for all specified tracks.

    Args:
        tracks: Which tracks to run discovery for.
        month: Target month "YYYY-MM" for query parameterisation.
        queries_by_track: Dict mapping "track_A"/"track_B"/"track_C" to query lists.
        client: Async Anthropic client.
        model_id: Claude model ID.

    Returns:
        Combined list of DiscoveredArticle from all tracks.
    """
    all_articles: list[DiscoveredArticle] = []

    for track in tracks:
        track_key = f"track_{track.value}"
        raw_queries = queries_by_track.get(track_key, [])
        queries = [_parameterise_query(q, month) for q in raw_queries]

        if not queries:
            logger.debug(f"No discovery queries configured for Track {track.value}")
            continue

        logger.info(f"Running {len(queries)} discovery searches for Track {track.value}")
        articles = await _search_for_track(queries, track, client, model_id)
        all_articles.extend(articles)
        logger.info(
            f"Track {track.value} discovery: {len(articles)} articles found "
            f"across {len(queries)} queries"
        )

    return all_articles


async def _search_for_track(
    queries: list[str],
    track: BriefingTrack,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> list[DiscoveredArticle]:
    """Run all queries for a single track and return discovered articles."""
    all_articles: list[DiscoveredArticle] = []
    seen_urls: set[str] = set()

    system_prompt = _get_discovery_system_prompt(track)

    for query in queries:
        try:
            # Retry at per-query level so a failure in one query doesn't
            # restart all queries from scratch.
            articles = await _run_single_query_with_retry(
                query, track, system_prompt, client, model_id
            )
            for article in articles:
                if article.url not in seen_urls:
                    seen_urls.add(article.url)
                    all_articles.append(article)
        except Exception as e:
            logger.warning(f"Discovery query failed for Track {track.value}: '{query}': {e}")
            continue

    return all_articles


@async_retry(max_attempts=3, base_delay=2.0)
async def _run_single_query_with_retry(
    query: str,
    track: BriefingTrack,
    system_prompt: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> list[DiscoveredArticle]:
    """Wrapper that applies per-query retry around _run_single_query."""
    return await _run_single_query(query, track, system_prompt, client, model_id)


async def _run_single_query(
    query: str,
    track: BriefingTrack,
    system_prompt: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> list[DiscoveredArticle]:
    """Run a single web search query and extract structured article results."""
    messages = [
        {
            "role": "user",
            "content": (
                f"Search for: {query}\n\n"
                f"Return a JSON array of the most relevant articles found. "
                f"Each item: {{\"url\": \"...\", \"title\": \"...\", \"snippet\": \"...\", "
                f"\"source_name\": \"...\", \"published_date\": \"YYYY-MM-DD or null\"}}. "
                f"Only include articles with real, working URLs. "
                f"Return ONLY valid JSON, no explanation."
            ),
        }
    ]

    response = await client.messages.create(
        model=model_id,
        max_tokens=2048,
        tools=[WEB_SEARCH_TOOL],
        system=system_prompt,
        messages=messages,
    )

    return _extract_articles_from_response(response, track, query)


def _extract_articles_from_response(
    response: anthropic.types.Message,
    track: BriefingTrack,
    query: str,
) -> list[DiscoveredArticle]:
    """Parse Claude's response to extract structured article data."""
    articles: list[DiscoveredArticle] = []

    # Collect all text content from the response
    text_parts: list[str] = []
    for block in response.content:
        if hasattr(block, "type") and block.type == "text":
            text_parts.append(block.text)

    full_text = "\n".join(text_parts)
    if not full_text.strip():
        return []

    # Try to parse JSON array from the response
    try:
        import json
        # Find the first complete JSON array. Use rfind to locate the matching
        # closing bracket for the first '[', avoiding greedy over-matching when
        # the model outputs explanation text before or after the JSON.
        start = full_text.find("[")
        end = full_text.rfind("]")
        json_match = (start != -1 and end != -1 and end > start)
        if json_match:
            raw_items = json.loads(full_text[start : end + 1])
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url", "")
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                source_name = item.get("source_name", "")
                published_date = item.get("published_date")

                if not url or not url.startswith(("http://", "https://")):
                    continue
                if not title:
                    continue

                tier = classify_url(url)
                articles.append(DiscoveredArticle(
                    url=url,
                    title=title,
                    snippet=snippet or title,
                    source_name=source_name or _extract_domain(url),
                    published_date=published_date,
                    track=track,
                    tier=tier,
                    discovered_via="claude_web_search",
                ))
    except (ValueError, KeyError) as e:
        logger.debug(f"Could not parse JSON from discovery response: {e}. Raw: {full_text[:200]}")

    return articles


def _get_discovery_system_prompt(track: BriefingTrack) -> str:
    """Return a track-appropriate system prompt for discovery searches."""
    prompts = {
        BriefingTrack.A: (
            "You are an expert legal technology analyst. Search for recent news about "
            "legal AI vendors, law firm AI adoption, and Singapore legal technology "
            "initiatives. Return only factual, recent articles from credible sources."
        ),
        BriefingTrack.B: (
            "You are an expert in AI policy and regulation. Search for recent AI regulatory "
            "and policy developments globally, with priority on APAC, EU, UK, and US. "
            "Return only official or highly credible sources."
        ),
        BriefingTrack.C: (
            "You are a senior legal technology strategist. Search for thought leadership "
            "on legal AI transformation, change management, workforce impact, and strategic "
            "AI adoption in professional services. Prioritise named authors and major firms."
        ),
    }
    return prompts.get(track, "You are a research assistant. Find recent, relevant articles.")


def _parameterise_query(query_template: str, month: str) -> str:
    """
    Replace {month} and {year} placeholders in query templates.

    Args:
        query_template: Query string with optional {month} and {year} placeholders.
        month: "YYYY-MM" string.
    """
    year = month[:4]
    # Convert "2026-03" → "March 2026" for human-readable month
    try:
        dt = datetime.strptime(month, "%Y-%m")
        month_human = dt.strftime("%B %Y")  # e.g. "March 2026"
    except ValueError:
        month_human = month

    return (
        query_template
        .replace("{month}", month_human)
        .replace("{year}", year)
        .replace("{month_human}", month_human)
    )


def _extract_domain(url: str) -> str:
    """Extract domain name from URL for source_name fallback."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.lstrip("www.")
    except Exception:
        return url
