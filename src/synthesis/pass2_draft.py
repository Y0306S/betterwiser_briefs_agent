"""
Pass 2: Deep synthesis with extended thinking and citations.

The primary synthesis pass. Generates the full briefing text using:
- Claude claude-opus-4-6 with extended thinking (budget_tokens=10000)
- Document blocks with citations: {enabled: True}
- Track-specific prompt templates
- BetterWiser context injection (Track C)

Input:  GatheredData + list[EventCluster] (from Passes 0-1)
Output: SynthesisResult with raw_html populated
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import anthropic

from src.schemas import (
    BriefingItem,
    BriefingTrack,
    EventCluster,
    GatheredData,
    SynthesisResult,
    ThemeGroup,
)
from src.utils.retry import async_retry

logger = logging.getLogger(__name__)

PROMPT_TEMPLATES_DIR = Path("config/prompt_templates")
BETTERWISER_CONTEXT_PATH = Path("config/betterwiser_context.txt")


@async_retry(max_attempts=3, base_delay=5.0)
async def draft_briefing(
    track: BriefingTrack,
    gathered: GatheredData,
    clusters: list[EventCluster],
    client: anthropic.AsyncAnthropic,
    model_config: dict,
) -> SynthesisResult:
    """
    Generate the full briefing text for one track using extended thinking + citations.

    Args:
        track: The briefing track (A, B, or C).
        gathered: All gathered data from Phase 2.
        clusters: Sorted event clusters from Passes 0-1.
        client: Async Anthropic client.
        model_config: Dict with 'id', 'max_tokens', 'extended_thinking_budget',
                      'max_context_sources', 'source_content_max_chars'.

    Returns:
        SynthesisResult with raw_html and thinking_summary populated.
    """
    model_id = model_config.get("id", "claude-opus-4-6")
    max_tokens = model_config.get("max_tokens", 16000)
    thinking_budget = model_config.get("extended_thinking_budget", 10000)
    max_context_sources = model_config.get("max_context_sources", 30)
    source_max_chars = model_config.get("source_content_max_chars", 4000)

    # Ensure max_tokens > thinking_budget + buffer
    if max_tokens <= thinking_budget:
        max_tokens = thinking_budget + 2000
        logger.warning(f"max_tokens adjusted to {max_tokens} (must exceed thinking_budget)")

    system_prompt = _load_system_prompt(track)
    betterwiser_context = _load_betterwiser_context() if track == BriefingTrack.C else ""

    # Build document blocks with citations enabled
    source_docs = _build_source_documents(gathered, clusters, max_context_sources, source_max_chars)

    # Build month context for the user message
    month_human = _month_human(gathered.run_context.month)
    month_start = f"{gathered.run_context.month}-01"
    year = gathered.run_context.month[:4]
    month_end_day = _last_day_of_month(gathered.run_context.month)
    month_end = f"{gathered.run_context.month}-{month_end_day:02d}"

    # Inject BetterWiser context into system prompt for Track C
    if betterwiser_context:
        system_prompt = system_prompt.replace("{betterwiser_context}", betterwiser_context)

    # Build user message with cluster context
    cluster_summary = _build_cluster_summary(clusters)
    user_content_parts = source_docs + [
        {
            "type": "text",
            "text": (
                f"Generate the Track {track.value} briefing for {month_human}.\n\n"
                f"Date range: {month_start} to {month_end}\n"
                f"Target month: {month_human}\n\n"
                f"Key events/themes discovered this month:\n{cluster_summary}\n\n"
                f"Historical context:\n{gathered.historical_context or 'None (first run).'}\n\n"
                f"Use the source documents above as your primary evidence. "
                f"Every factual claim must be supported by a document citation. "
                f"Follow the format instructions in your system prompt exactly."
            ),
        }
    ]

    # Template parameter substitution in system prompt
    system_prompt = (
        system_prompt
        .replace("{target_month_human}", month_human)
        .replace("{month_start}", month_start)
        .replace("{month_end}", month_end)
        .replace("{year}", year)
    )

    logger.info(
        f"Track {track.value}: Pass 2 draft — {len(source_docs)} source documents, "
        f"extended thinking budget={thinking_budget}"
    )

    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            thinking={
                "type": "enabled",
                "budget_tokens": thinking_budget,
            },
            system=system_prompt,
            messages=[{"role": "user", "content": user_content_parts}],
        )
    except anthropic.BadRequestError as e:
        # Extended thinking may not be available for all model versions
        logger.warning(
            f"Extended thinking failed ({e}), retrying without thinking parameter"
        )
        response = await client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content_parts}],
        )

    # Extract thinking block and text block from response
    thinking_text: Optional[str] = None
    html_text = ""

    for block in response.content:
        if hasattr(block, "type"):
            if block.type == "thinking":
                thinking_text = getattr(block, "thinking", "")
            elif block.type == "text":
                html_text += block.text

    if not html_text.strip():
        logger.error(f"Track {track.value}: Pass 2 returned empty content")
        html_text = f"<p><em>Briefing generation failed for Track {track.value}. Please retry.</em></p>"

    logger.info(
        f"Track {track.value}: Pass 2 complete — "
        f"{len(html_text)} chars, thinking={'yes' if thinking_text else 'no'}"
    )

    # Extract hot vendor suggestion from Track A thinking
    hot_vendor = None
    if track == BriefingTrack.A and thinking_text:
        hot_vendor = _extract_hot_vendor(html_text)

    return SynthesisResult(
        run_id=gathered.run_context.run_id,
        track=track,
        raw_html=html_text,
        items=[],  # populated in pass4_format.py
        theme_groups=[],  # populated in pass4_format.py
        thinking_summary=thinking_text,
        hot_vendor_suggestion=hot_vendor,
        pass_completed=[0, 1, 2],
    )


def _build_source_documents(
    gathered: GatheredData,
    clusters: list[EventCluster],
    max_sources: int,
    source_max_chars: int,
) -> list[dict]:
    """
    Build document blocks for the Claude API call with citations enabled.

    Prioritises scraped sources that correspond to top clusters.
    """
    # Build URL → content map from scraped sources
    url_to_source = {s.url: s for s in gathered.scraped_sources if not s.error}

    # Collect sources in cluster priority order
    selected_urls: list[str] = []
    seen: set[str] = set()

    # First: cluster representative URLs (prioritised by triage)
    for cluster in clusters:
        for url in cluster.member_urls[:2]:  # top 2 per cluster
            if url not in seen:
                selected_urls.append(url)
                seen.add(url)

    # Then: all remaining scraped sources
    for source in gathered.scraped_sources:
        if source.url not in seen and not source.error:
            selected_urls.append(source.url)
            seen.add(source.url)

    # Also include discovered article snippets as mini-documents
    for article in gathered.discovered_articles[:20]:
        if article.url not in seen and article.snippet:
            selected_urls.append(article.url)
            seen.add(article.url)

    # Truncate to limit
    selected_urls = selected_urls[:max_sources]

    # Build document blocks
    documents = []
    for url in selected_urls:
        source = url_to_source.get(url)
        if source:
            content = source.content[:source_max_chars]
            title = source.title or url
        else:
            # Use snippet from discovered articles
            article = next(
                (a for a in gathered.discovered_articles if a.url == url), None
            )
            if article:
                content = article.snippet
                title = article.title
            else:
                continue

        if not content.strip():
            continue

        documents.append({
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": content,
            },
            "title": title[:200],
            "context": f"Source URL: {url}",
            "citations": {"enabled": True},
        })

    logger.debug(f"Built {len(documents)} source documents for Pass 2")
    return documents


def _load_system_prompt(track: BriefingTrack) -> str:
    """Load the system prompt template for the given track."""
    template_files = {
        BriefingTrack.A: "track_a_vendor_customer.txt",
        BriefingTrack.B: "track_b_global_policy.txt",
        BriefingTrack.C: "track_c_thought_leadership.txt",
    }
    filename = template_files.get(track)
    if not filename:
        return "You are an expert analyst producing a briefing."

    path = PROMPT_TEMPLATES_DIR / filename
    if not path.exists():
        logger.warning(f"Prompt template not found: {path}")
        return "You are an expert analyst producing a briefing."

    return path.read_text(encoding="utf-8")


def _load_betterwiser_context() -> str:
    """Load the BetterWiser company context for Track C injection."""
    if BETTERWISER_CONTEXT_PATH.exists():
        return BETTERWISER_CONTEXT_PATH.read_text(encoding="utf-8")
    logger.warning(f"BetterWiser context not found at {BETTERWISER_CONTEXT_PATH}")
    return "[BetterWiser context not configured. See config/betterwiser_context.txt]"


def _build_cluster_summary(clusters: list[EventCluster]) -> str:
    """Build a compact text summary of event clusters for the user message."""
    if not clusters:
        return "No specific events pre-identified — rely on source documents."
    lines = []
    for i, cluster in enumerate(clusters[:20], 1):
        annotation = f" [{cluster.trend_annotation}]" if cluster.trend_annotation else ""
        new_tag = " [NEW ENTRANT]" if cluster.is_new_entrant else ""
        lines.append(f"{i}. {cluster.theme}{annotation}{new_tag}")
    return "\n".join(lines)


def _extract_hot_vendor(html_text: str) -> Optional[str]:
    """Look for 'Hot Vendor to Watch' section in the drafted HTML."""
    import re
    match = re.search(
        r"(?:Hot Vendor to Watch|Hot New Vendor|Emerging Vendor)[:\s]*(.+?)(?:\n|<|$)",
        html_text, re.IGNORECASE
    )
    return match.group(1).strip() if match else None


def _month_human(month: str) -> str:
    """Convert 'YYYY-MM' to 'Month YYYY'."""
    from datetime import datetime
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month


def _last_day_of_month(month: str) -> int:
    """Return the last day of the given YYYY-MM month."""
    import calendar
    year, mon = int(month[:4]), int(month[5:7])
    return calendar.monthrange(year, mon)[1]
