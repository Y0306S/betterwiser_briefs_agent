"""
Track C thought leadership deep research — Phase 2, Sub-pipeline D.

Runs 6 sequential waves maximising unique thought leadership sources:

  Wave 1 — Newsletter extraction + dynamic watchlist building
  Wave 2 — Person-specific deep search (4+ searches per person)
  Wave 3 — Firm thought leadership pages (direct web_fetch)
  Wave 4 — Tavily deep research (thematic queries)
  Wave 5 — Semantic similarity expansion (top 5-10 articles)
  Wave 6 — Conference/event speaker mining

Estimated: 80–150 web searches + 50–100 additional pages per run.
Cost is NOT a constraint — exhaust all waves.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

import anthropic
import yaml

from src.gatherers.discovery import WEB_SEARCH_TOOL, _parameterise_query
from src.schemas import BriefingTrack, DiscoveredArticle, EmailSource, SourceTier
from src.utils.authority import classify_url
from src.utils.retry import async_retry

logger = logging.getLogger(__name__)

# Built-in Claude web_fetch tool
WEB_FETCH_TOOL = {
    "type": "web_fetch",
    "name": "web_fetch",
}


async def run_waves(
    month: str,
    email_sources: list[EmailSource],
    watchlist_config: dict,
    client: anthropic.AsyncAnthropic,
    model_id: str = "claude-opus-4-6",
) -> list[DiscoveredArticle]:
    """
    Execute all 6 research waves for Track C thought leadership.

    Args:
        month: Target month "YYYY-MM".
        email_sources: Email sources from inbox reader (may be empty).
        watchlist_config: Parsed vendor_watchlist.yaml content.
        client: Async Anthropic client.
        model_id: Claude model ID.

    Returns:
        All discovered articles across all 6 waves.
    """
    all_articles: list[DiscoveredArticle] = []
    seen_urls: set[str] = set()
    dynamic_watchlist: list[dict] = list(watchlist_config.get("thought_leaders", []))
    consulting_firms: list[dict] = watchlist_config.get("consulting_firms", [])
    conferences: list[dict] = watchlist_config.get("conferences", [])

    def add_articles(articles: list[DiscoveredArticle], wave: int) -> None:
        for a in articles:
            if a.url not in seen_urls:
                seen_urls.add(a.url)
                a.discovery_wave = wave
                all_articles.append(a)

    # Wave 1: Newsletter extraction + watchlist building
    logger.info("Wave 1: Newsletter extraction and watchlist building")
    w1_articles, new_names = await _wave1_newsletter_extraction(
        email_sources, month, client, model_id
    )
    add_articles(w1_articles, wave=1)
    # Add newly discovered people to dynamic watchlist
    for name in new_names:
        if not any(p["name"] == name for p in dynamic_watchlist):
            dynamic_watchlist.append({
                "name": name,
                "affiliation": "Discovered",
                "search_terms": [
                    f'"{name}" legal AI',
                    f'"{name}" legal technology',
                    f'"{name}" site:linkedin.com OR site:medium.com',
                    f'"{name}" keynote OR panel OR conference',
                ],
            })
    logger.info(f"Wave 1: {len(w1_articles)} articles, {len(new_names)} new watchlist entries")

    # Wave 2: Person-specific deep search
    logger.info(f"Wave 2: Person-specific deep search ({len(dynamic_watchlist)} people)")
    w2_articles = await _wave2_person_search(dynamic_watchlist, month, client, model_id)
    add_articles(w2_articles, wave=2)
    logger.info(f"Wave 2: {len(w2_articles)} articles")

    # Wave 3: Firm thought leadership pages
    logger.info(f"Wave 3: Firm insights pages ({len(consulting_firms)} firms)")
    w3_articles = await _wave3_firm_pages(consulting_firms, month, client, model_id)
    add_articles(w3_articles, wave=3)
    logger.info(f"Wave 3: {len(w3_articles)} articles")

    # Wave 4: Tavily deep research
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        logger.info("Wave 4: Tavily deep research")
        w4_articles = await _wave4_tavily(month, tavily_key)
        add_articles(w4_articles, wave=4)
        logger.info(f"Wave 4: {len(w4_articles)} articles")
    else:
        logger.warning("Wave 4: TAVILY_API_KEY not set — skipping Tavily deep research")

    # Wave 5: Semantic similarity expansion
    logger.info("Wave 5: Semantic similarity expansion")
    top_articles = all_articles[:10]  # top articles found so far
    w5_articles = await _wave5_semantic_expansion(top_articles, month, client, model_id)
    add_articles(w5_articles, wave=5)
    logger.info(f"Wave 5: {len(w5_articles)} additional articles")

    # Wave 6: Conference speaker mining
    logger.info(f"Wave 6: Conference speaker mining ({len(conferences)} events)")
    w6_articles, speaker_names = await _wave6_conference_mining(
        conferences, month, client, model_id
    )
    add_articles(w6_articles, wave=6)
    # If new speakers found, run brief Wave 2-style searches for them
    if speaker_names:
        logger.info(f"Wave 6 bonus: {len(speaker_names)} new speakers → supplementary search")
        speaker_persons = [
            {
                "name": n,
                "affiliation": "Conference Speaker",
                "search_terms": [
                    f'"{n}" legal AI',
                    f'"{n}" legal technology {month[:4]}',
                ],
            }
            for n in speaker_names[:5]  # cap at 5 to avoid runaway searches
        ]
        bonus_articles = await _wave2_person_search(speaker_persons, month, client, model_id)
        add_articles(bonus_articles, wave=6)

    logger.info(
        f"Thought leadership research complete: {len(all_articles)} unique articles "
        f"across 6 waves"
    )
    return all_articles


# ---------------------------------------------------------------------------
# Wave implementations
# ---------------------------------------------------------------------------

async def _wave1_newsletter_extraction(
    email_sources: list[EmailSource],
    month: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> tuple[list[DiscoveredArticle], list[str]]:
    """
    Extract thought leadership content and named people from newsletter emails.
    Returns (articles, newly_discovered_person_names).
    """
    if not email_sources:
        logger.debug("No email sources for Wave 1 — inbox not configured or empty")
        return [], []

    articles: list[DiscoveredArticle] = []
    new_people: list[str] = []

    # Build a condensed summary of all newsletter content for Claude to analyse
    newsletter_content = _summarise_email_sources(email_sources, max_chars=20000)
    if not newsletter_content.strip():
        return [], []

    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=4096,
            system=(
                "You are a research analyst extracting structured intelligence from newsletter emails. "
                "Identify thought leadership articles and named individuals who are commenting on legal AI."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"From these newsletter emails for {_month_human(month)}, extract:\n\n"
                    f"1. All article links and summaries (JSON array: "
                    f"{{\"url\", \"title\", \"snippet\", \"source_name\", \"published_date\"}})\n"
                    f"2. All named individuals mentioned as thought leaders or commentators "
                    f"(JSON array of name strings)\n\n"
                    f"Return as JSON: {{\"articles\": [...], \"people\": [...]}}\n\n"
                    f"Newsletter content:\n{newsletter_content}"
                ),
            }],
        )

        text = _extract_text(response)
        parsed = _parse_json_response(text)
        if parsed:
            for item in parsed.get("articles", []):
                url = item.get("url", "")
                if url and url.startswith(("http://", "https://")):
                    articles.append(DiscoveredArticle(
                        url=url,
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        source_name=item.get("source_name", ""),
                        published_date=item.get("published_date"),
                        track=BriefingTrack.C,
                        tier=classify_url(url),
                        discovered_via="email_link",
                    ))
            new_people = [p for p in parsed.get("people", []) if isinstance(p, str) and p.strip()]

    except Exception as e:
        logger.warning(f"Wave 1 newsletter extraction failed: {e}")

    return articles, new_people


@async_retry(max_attempts=2, base_delay=1.0)
async def _wave2_person_search(
    persons: list[dict],
    month: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> list[DiscoveredArticle]:
    """Run 4+ targeted searches per person on the watchlist."""
    articles: list[DiscoveredArticle] = []
    seen_urls: set[str] = set()

    for person in persons:
        name = person.get("name", "")
        search_terms = person.get("search_terms", [])

        if not name or not search_terms:
            continue

        for raw_query in search_terms:
            query = _parameterise_query(raw_query, month)
            try:
                response = await client.messages.create(
                    model=model_id,
                    max_tokens=1024,
                    tools=[WEB_SEARCH_TOOL],
                    system=(
                        f"You are researching thought leadership by {name} in legal AI. "
                        f"Find their most recent and insightful publications."
                    ),
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Search: {query}\n"
                            f"Return JSON array: "
                            f"[{{\"url\", \"title\", \"snippet\", \"source_name\", \"published_date\"}}]. "
                            f"Only include content actually written by or featuring {name}. "
                            f"Return ONLY valid JSON."
                        ),
                    }],
                )

                for item in _parse_article_array(_extract_text(response)):
                    url = item.get("url", "")
                    if url and url not in seen_urls and url.startswith(("http://", "https://")):
                        seen_urls.add(url)
                        articles.append(DiscoveredArticle(
                            url=url,
                            title=item.get("title", ""),
                            snippet=item.get("snippet", ""),
                            source_name=item.get("source_name", name),
                            published_date=item.get("published_date"),
                            track=BriefingTrack.C,
                            tier=classify_url(url),
                            discovered_via="claude_web_search",
                        ))

            except Exception as e:
                logger.debug(f"Wave 2 search failed for '{query}': {e}")
                continue

    return articles


@async_retry(max_attempts=2, base_delay=1.0)
async def _wave3_firm_pages(
    firms: list[dict],
    month: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> list[DiscoveredArticle]:
    """Scrape firm thought leadership pages using Claude's web_fetch tool."""
    articles: list[DiscoveredArticle] = []
    seen_urls: set[str] = set()

    for firm in firms:
        firm_name = firm.get("name", "")
        sg_url = firm.get("sg_insights_url")
        global_url = firm.get("insights_url")
        urls_to_try = [u for u in [sg_url, global_url] if u]

        for insights_url in urls_to_try:
            try:
                response = await client.messages.create(
                    model=model_id,
                    max_tokens=2048,
                    tools=[WEB_FETCH_TOOL],
                    system=(
                        f"You are extracting recent thought leadership content from {firm_name}'s "
                        f"insights page. Focus on legal AI, AI governance, and professional services."
                    ),
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Fetch {insights_url} and extract all recent articles "
                            f"(published in {_month_human(month)} or recent months) "
                            f"about AI, legal technology, workforce transformation, or related themes.\n"
                            f"Return JSON array: "
                            f"[{{\"url\", \"title\", \"snippet\", \"source_name\", \"published_date\"}}]"
                        ),
                    }],
                )

                for item in _parse_article_array(_extract_text(response)):
                    url = item.get("url", "")
                    if url and url not in seen_urls and url.startswith(("http://", "https://")):
                        seen_urls.add(url)
                        articles.append(DiscoveredArticle(
                            url=url,
                            title=item.get("title", ""),
                            snippet=item.get("snippet", ""),
                            source_name=item.get("source_name", firm_name),
                            published_date=item.get("published_date"),
                            track=BriefingTrack.C,
                            tier=classify_url(url),
                            discovered_via="claude_web_search",
                        ))
                break  # if sg_url worked, don't try global_url

            except Exception as e:
                logger.debug(f"Wave 3 firm page fetch failed for {firm_name} ({insights_url}): {e}")
                continue

    return articles


async def _wave4_tavily(month: str, tavily_api_key: str) -> list[DiscoveredArticle]:
    """Use Tavily for deep thematic research (catches non-watchlist sources)."""
    articles: list[DiscoveredArticle] = []

    thematic_queries = [
        f"AI workforce transformation legal profession {month[:4]}",
        f"enterprise AI adoption maturity legal services {month[:4]}",
        f"change management generative AI law firms {month[:4]}",
        f"strategic AI value creation professional services {month[:4]}",
        f"legal AI governance responsible use {month[:4]}",
    ]

    try:
        from tavily import TavilyClient
        tavily = TavilyClient(api_key=tavily_api_key)

        for query in thematic_queries:
            try:
                result = tavily.search(
                    query=query,
                    search_depth="advanced",
                    max_results=10,
                    include_answer=False,
                )
                for r in result.get("results", []):
                    url = r.get("url", "")
                    if url and url.startswith(("http://", "https://")):
                        articles.append(DiscoveredArticle(
                            url=url,
                            title=r.get("title", ""),
                            snippet=r.get("content", "")[:500],
                            source_name=r.get("source", ""),
                            published_date=r.get("published_date"),
                            track=BriefingTrack.C,
                            tier=classify_url(url),
                            discovered_via="tavily",
                        ))
                logger.debug(f"Tavily '{query}': {len(result.get('results', []))} results")
            except Exception as e:
                logger.warning(f"Tavily search failed for '{query}': {e}")

    except ImportError:
        logger.warning("tavily-python not installed — Wave 4 skipped")

    return articles


@async_retry(max_attempts=2, base_delay=1.0)
async def _wave5_semantic_expansion(
    seed_articles: list[DiscoveredArticle],
    month: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> list[DiscoveredArticle]:
    """Find conceptually similar articles to the best discoveries so far."""
    if not seed_articles:
        return []

    articles: list[DiscoveredArticle] = []
    seen_urls: set[str] = {a.url for a in seed_articles}

    for seed in seed_articles[:7]:  # limit to top 7 seeds
        try:
            query = (
                f"Find articles published in {_month_human(month)} discussing similar themes to: "
                f'"{seed.title}" — {seed.snippet[:200]}'
            )
            response = await client.messages.create(
                model=model_id,
                max_tokens=1024,
                tools=[WEB_SEARCH_TOOL],
                system=(
                    "You are finding conceptually related thought leadership on legal AI. "
                    "Avoid repeating already-known sources — find new perspectives."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"{query}\n"
                        f"Return JSON array: "
                        f"[{{\"url\", \"title\", \"snippet\", \"source_name\", \"published_date\"}}]. "
                        f"Return ONLY valid JSON."
                    ),
                }],
            )

            for item in _parse_article_array(_extract_text(response)):
                url = item.get("url", "")
                if url and url not in seen_urls and url.startswith(("http://", "https://")):
                    seen_urls.add(url)
                    articles.append(DiscoveredArticle(
                        url=url,
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        source_name=item.get("source_name", ""),
                        published_date=item.get("published_date"),
                        track=BriefingTrack.C,
                        tier=classify_url(url),
                        discovered_via="claude_web_search",
                    ))

        except Exception as e:
            logger.debug(f"Wave 5 expansion failed for '{seed.title[:50]}': {e}")

    return articles


@async_retry(max_attempts=2, base_delay=1.0)
async def _wave6_conference_mining(
    conferences: list[dict],
    month: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> tuple[list[DiscoveredArticle], list[str]]:
    """Mine conference agendas for speaker names and their publications."""
    articles: list[DiscoveredArticle] = []
    new_speaker_names: list[str] = []
    seen_urls: set[str] = set()

    for conf in conferences:
        conf_name = conf.get("name", "")
        search_query = conf.get("website_search", f"{conf_name} {month[:4]} speakers")
        search_query = _parameterise_query(search_query, month)

        try:
            response = await client.messages.create(
                model=model_id,
                max_tokens=2048,
                tools=[WEB_SEARCH_TOOL],
                system=(
                    f"You are mining {conf_name} for speaker names and their recent publications "
                    f"on legal AI topics."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Search: {search_query}\n"
                        f"Extract: (1) names of speakers presenting on legal AI topics, "
                        f"(2) any articles or publications by those speakers.\n"
                        f"Return JSON: {{\"speakers\": [\"name1\", ...], "
                        f"\"articles\": [{{\"url\", \"title\", \"snippet\", \"source_name\", "
                        f"\"published_date\"}}]}}"
                    ),
                }],
            )

            text = _extract_text(response)
            parsed = _parse_json_response(text)
            if parsed:
                speakers = parsed.get("speakers", [])
                new_speaker_names.extend([s for s in speakers if isinstance(s, str)])

                for item in parsed.get("articles", []):
                    url = item.get("url", "")
                    if url and url not in seen_urls and url.startswith(("http://", "https://")):
                        seen_urls.add(url)
                        articles.append(DiscoveredArticle(
                            url=url,
                            title=item.get("title", ""),
                            snippet=item.get("snippet", ""),
                            source_name=item.get("source_name", conf_name),
                            published_date=item.get("published_date"),
                            track=BriefingTrack.C,
                            tier=classify_url(url),
                            discovered_via="claude_web_search",
                        ))

        except Exception as e:
            logger.debug(f"Wave 6 conference mining failed for {conf_name}: {e}")

    return articles, list(set(new_speaker_names))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(response: anthropic.types.Message) -> str:
    """Extract concatenated text from all text blocks in a Claude response."""
    parts = []
    for block in response.content:
        if hasattr(block, "type") and block.type == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _parse_json_response(text: str) -> Optional[dict]:
    """Try to extract a JSON object from Claude's response text."""
    import json
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group())
    except (ValueError, KeyError):
        pass
    return None


def _parse_article_array(text: str) -> list[dict]:
    """Try to extract a JSON array of article objects from text."""
    import json
    try:
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            data = json.loads(match.group())
            return [item for item in data if isinstance(item, dict)]
    except (ValueError, KeyError):
        pass
    return []


def _summarise_email_sources(
    email_sources: list[EmailSource],
    max_chars: int = 20000,
) -> str:
    """Concatenate email bodies into a single context string, truncated."""
    parts = []
    total = 0
    for email in email_sources:
        if total >= max_chars:
            break
        body = email.body_text[:2000] if email.body_text else ""
        if body.strip():
            parts.append(f"From: {email.sender}\nSubject: {email.subject}\n{body}")
            total += len(body)
    return "\n\n---\n\n".join(parts)


def _month_human(month: str) -> str:
    """Convert "YYYY-MM" to "Month YYYY" (e.g. "March 2026")."""
    try:
        from datetime import datetime
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month
