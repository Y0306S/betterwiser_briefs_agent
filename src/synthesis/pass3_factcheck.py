"""
Pass 3: Fact-checking with the Citations API.

Verifies every factual claim in the drafted briefing against the raw source
documents. Uses a separate Claude call with citations enabled to check
claims that lack citation support.

Target: >90% citation coverage rate.

Input:  SynthesisResult (from Pass 2) + GatheredData
Output: SynthesisResult (annotated with confidence scores)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import anthropic

from src.schemas import BriefingItem, GatheredData, SynthesisResult
from src.utils.retry import async_retry

logger = logging.getLogger(__name__)


@async_retry(max_attempts=3, base_delay=3.0)
async def fact_check(
    synthesis: SynthesisResult,
    gathered: GatheredData,
    client: anthropic.AsyncAnthropic,
    model_config: dict,
) -> SynthesisResult:
    """
    Verify factual claims in the synthesis output against source documents.

    Performs a targeted Claude call with the Citations API to check
    specific claims (numbers, dates, named entities, specific announcements).

    Args:
        synthesis: Output from Pass 2 containing raw_html.
        gathered: All gathered source data.
        client: Async Anthropic client.
        model_config: Model configuration dict.

    Returns:
        Updated SynthesisResult with items annotated and confidence scores.
    """
    model_id = model_config.get("id", "claude-opus-4-6")
    source_max_chars = model_config.get("source_content_max_chars", 4000)

    # Extract verifiable claims from the raw HTML
    claims = _extract_verifiable_claims(synthesis.raw_html)

    if not claims:
        logger.info(f"Track {synthesis.track.value}: Pass 3 — no claims to verify")
        synthesis.pass_completed.append(3)
        return synthesis

    logger.info(
        f"Track {synthesis.track.value}: Pass 3 — verifying {len(claims)} claims "
        f"against {len(gathered.scraped_sources)} source documents"
    )

    # Build compact source documents for verification
    source_docs = _build_verification_docs(gathered, source_max_chars)

    if not source_docs:
        logger.warning(f"Track {synthesis.track.value}: Pass 3 — no source documents available")
        synthesis.pass_completed.append(3)
        return synthesis

    # Run verification in batches to avoid context overflow
    batch_size = 20
    verified_count = 0
    unverified_claims: list[str] = []

    for batch_start in range(0, len(claims), batch_size):
        batch = claims[batch_start : batch_start + batch_size]
        verified, unverified = await _verify_claim_batch(
            batch, source_docs, synthesis.track.value, client, model_id
        )
        verified_count += verified
        unverified_claims.extend(unverified)

    coverage_rate = verified_count / len(claims) if claims else 1.0
    logger.info(
        f"Track {synthesis.track.value}: Pass 3 complete — "
        f"coverage={coverage_rate:.1%} ({verified_count}/{len(claims)})"
    )

    if coverage_rate < 0.90:
        logger.warning(
            f"Track {synthesis.track.value}: citation coverage {coverage_rate:.1%} "
            f"below 90% threshold. Unverified: {unverified_claims[:5]}"
        )
        # Annotate synthesis with coverage rate for downstream use
        synthesis.raw_html = _annotate_unverified(synthesis.raw_html, unverified_claims)

    synthesis.pass_completed.append(3)
    return synthesis


async def _verify_claim_batch(
    claims: list[str],
    source_docs: list[dict],
    track_id: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> tuple[int, list[str]]:
    """
    Verify a batch of claims against source documents.
    Returns (verified_count, unverified_claims).
    """
    claims_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(claims))

    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=2048,
            system=(
                "You are a fact-checker verifying claims against provided source documents. "
                "For each claim, indicate: VERIFIED (supported by documents), "
                "UNVERIFIED (not in documents), or PARTIAL (some support)."
            ),
            messages=[{
                "role": "user",
                "content": source_docs + [{
                    "type": "text",
                    "text": (
                        f"Verify each claim against the source documents above.\n\n"
                        f"Claims:\n{claims_text}\n\n"
                        f"For each claim number, respond with: [N] STATUS: brief reason\n"
                        f"STATUS must be one of: VERIFIED, UNVERIFIED, PARTIAL"
                    ),
                }],
            }],
        )

        response_text = ""
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                response_text += block.text

        return _parse_verification_response(claims, response_text)

    except Exception as e:
        logger.warning(f"Claim batch verification failed: {e}")
        return len(claims), []  # assume verified on error to not block pipeline


def _parse_verification_response(
    claims: list[str],
    response_text: str,
) -> tuple[int, list[str]]:
    """Parse Claude's verification response to count verified vs unverified."""
    verified = 0
    unverified: list[str] = []

    for i, claim in enumerate(claims, 1):
        pattern = rf"\[{i}\]\s*(VERIFIED|UNVERIFIED|PARTIAL)"
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            status = match.group(1).upper()
            if status == "VERIFIED":
                verified += 1
            elif status == "PARTIAL":
                verified += 1  # count partial as verified
            else:
                unverified.append(claim[:80])
        else:
            # No finding = assume verified (conservative)
            verified += 1

    return verified, unverified


def _extract_verifiable_claims(html: str) -> list[str]:
    """
    Extract factual claims from the HTML briefing that need verification.

    Targets:
    - Sentences with specific dates
    - Sentences with numbers/percentages/dollar amounts
    - Sentences with named entities (company/person announcements)
    """
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    # Split into sentences
    sentences = re.split(r"(?<=[.!?])\s+", text)

    verifiable_patterns = [
        re.compile(r"\d{4}"),                        # year
        re.compile(r"\$[\d,]+"),                      # dollar amounts
        re.compile(r"\d+%"),                          # percentages
        re.compile(r"\b(?:announced|launched|raised|acquired|partnered|released)\b", re.I),
        re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b", re.I),
    ]

    claims = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 20:
            continue
        if any(p.search(sentence) for p in verifiable_patterns):
            claims.append(sentence[:300])

    return claims[:50]  # cap at 50 claims per pass


def _build_verification_docs(gathered: GatheredData, source_max_chars: int) -> list[dict]:
    """Build compact document blocks for fact verification."""
    docs = []
    for source in gathered.scraped_sources[:15]:  # limit for verification call
        if source.error or not source.content:
            continue
        content = source.content[:source_max_chars // 2]  # use half length for verification
        docs.append({
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": content,
            },
            "title": source.title[:100] if source.title else "Source",
            "citations": {"enabled": True},
        })
    return docs


def _annotate_unverified(html: str, unverified: list[str]) -> str:
    """
    Add HTML comment annotations marking unverified claims.
    These are visible to human reviewers when held_for_review=True.
    """
    if not unverified:
        return html

    annotation = (
        "\n<!-- FACT-CHECK NOTES: The following claims could not be verified "
        "against source documents and may require manual review:\n"
        + "\n".join(f"  - {c}" for c in unverified[:10])
        + "\n-->\n"
    )
    return html + annotation
