"""
Pass 3: Fact-checking with the Citations API.

Verifies every factual claim in the drafted briefing against the raw source
documents. Operates on the structured SynthesisDraft produced by Pass 2 —
never parses raw HTML.

Scoring:
  VERIFIED  → item.confidence unchanged (1.0 unless already reduced)
  PARTIAL   → item.confidence *= 0.7  (partial support noted)
  UNVERIFIED → triggers one correction-loop re-lookup; if still unverified,
               item.verified = False, item.confidence = 0.0

Target: >90% citation coverage (verified + partial / total).

Input:  SynthesisResult (from Pass 2, draft populated) + GatheredData
Output: SynthesisResult with DraftBriefingItem.confidence/verified updated
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import anthropic

from src.schemas import (
    BriefingItem,
    DraftBriefingItem,
    DraftSection,
    GatheredData,
    SynthesisDraft,
    SynthesisResult,
)
from src.utils.retry import async_retry
from src.utils.token_budget import trim_documents_to_budget

logger = logging.getLogger(__name__)

# Maximum number of individual claims to check in one pass
_CLAIM_CAP = 100

# Confidence multiplier when a claim is only partially supported
_PARTIAL_CONFIDENCE = 0.7

# Tool schema for structured verification output — eliminates regex parsing
_VERIFY_CLAIMS_TOOL = {
    "name": "submit_verification",
    "description": (
        "Submit verification results for all claims. "
        "Call this tool ONCE with results for every claim in the batch."
    ),
    "input_schema": {
        "type": "object",
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "description": "One entry per claim, in the same order as the input claims.",
                "items": {
                    "type": "object",
                    "required": ["claim_number", "status"],
                    "properties": {
                        "claim_number": {
                            "type": "integer",
                            "description": "1-based index matching the input claim list.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["VERIFIED", "PARTIAL", "UNVERIFIED"],
                            "description": "Verification outcome.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "One sentence explaining the verdict.",
                        },
                    },
                },
            },
        },
    },
}


@async_retry(max_attempts=3, base_delay=3.0)
async def fact_check(
    synthesis: SynthesisResult,
    gathered: GatheredData,
    client: anthropic.AsyncAnthropic,
    model_config: dict,
) -> SynthesisResult:
    """
    Verify factual claims in the synthesis output against source documents.

    Primary path: operates on synthesis.draft (structured SynthesisDraft).
    Fallback path: if draft is missing, falls back to HTML-based claim extraction
    and annotates raw_html with unverified markers (preserving backward compat).

    Args:
        synthesis: Output from Pass 2 (draft should be populated).
        gathered:  All gathered source data (scraped pages + article snippets).
        client:    Async Anthropic client.
        model_config: Model configuration dict (uses research_model / sonnet).

    Returns:
        Updated SynthesisResult; DraftBriefingItem.confidence and .verified
        fields are mutated in-place on synthesis.draft.
    """
    # Use the lighter research model (Sonnet) for fact-checking — Opus is Pass 2 only
    model_id = model_config.get("research_id", model_config.get("id", "claude-sonnet-4-6"))
    source_max_chars = model_config.get("source_content_max_chars", 4000)

    source_docs = _build_verification_docs(gathered, source_max_chars)
    source_docs = trim_documents_to_budget(
        source_docs, "", "",
        reserved_output=4096,
        label=f"Track {synthesis.track.value} Pass 3",
    )

    if not source_docs:
        logger.warning(
            f"Track {synthesis.track.value}: Pass 3 — no source documents; skipping verification"
        )
        if 3 not in synthesis.pass_completed:
            synthesis.pass_completed.append(3)
        return synthesis

    # ------------------------------------------------------------------ #
    # Primary path: structured draft available                            #
    # ------------------------------------------------------------------ #
    if synthesis.draft is not None:
        synthesis = await _factcheck_draft(synthesis, source_docs, client, model_id)

    # ------------------------------------------------------------------ #
    # Fallback path: no structured draft (Pass 2 used plain-text output) #
    # ------------------------------------------------------------------ #
    else:
        synthesis = await _factcheck_html_fallback(synthesis, source_docs, client, model_id)

    if 3 not in synthesis.pass_completed:
        synthesis.pass_completed.append(3)
    return synthesis


# ---------------------------------------------------------------------------
# Primary path: structured draft
# ---------------------------------------------------------------------------

async def _factcheck_draft(
    synthesis: SynthesisResult,
    source_docs: list[dict],
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> SynthesisResult:
    """Verify claims extracted from SynthesisDraft items."""
    draft = synthesis.draft
    assert draft is not None

    # Collect all (section_idx, item_idx, claim_text) tuples
    claim_index: list[tuple[int, int, str]] = []
    for s_idx, section in enumerate(draft.sections):
        for i_idx, item in enumerate(section.items):
            claims = _extract_item_claims(item)
            for claim in claims:
                if len(claim_index) >= _CLAIM_CAP:
                    break
                claim_index.append((s_idx, i_idx, claim))
        if len(claim_index) >= _CLAIM_CAP:
            break

    if not claim_index:
        total_draft_items = sum(len(s.items) for s in draft.sections)
        if total_draft_items > 0:
            logger.warning(
                f"Track {synthesis.track.value}: Pass 3 — claim extractor yielded 0 claims "
                f"from {total_draft_items} draft items. Coverage defaulting to 0.0 (not vacuously 1.0)."
            )
            # Propagate the 0 coverage so grounding / delivery can flag it
            if 3 not in synthesis.pass_completed:
                synthesis.pass_completed.append(3)
            return synthesis
        logger.info(f"Track {synthesis.track.value}: Pass 3 — no items and no claims; skipping")
        return synthesis

    logger.info(
        f"Track {synthesis.track.value}: Pass 3 — verifying {len(claim_index)} claims "
        f"from {sum(len(s.items) for s in draft.sections)} items "
        f"against {len(source_docs)} source documents"
    )

    # Run verification in batches of 20
    batch_size = 20
    # Map (s_idx, i_idx) → list of (status, reason) per batch result
    item_results: dict[tuple[int, int], list[tuple[str, str]]] = {}

    for batch_start in range(0, len(claim_index), batch_size):
        batch = claim_index[batch_start : batch_start + batch_size]
        claim_texts = [c for _, _, c in batch]

        results = await _verify_claim_batch(claim_texts, source_docs, synthesis.track.value, client, model_id)

        for (s_idx, i_idx, _), (status, reason) in zip(batch, results):
            key = (s_idx, i_idx)
            item_results.setdefault(key, []).append((status, reason))

    # Apply results to DraftBriefingItems; run correction loop for UNVERIFIED
    unverified_total = 0
    partial_total = 0
    verified_total = 0

    for (s_idx, i_idx), statuses in item_results.items():
        item = draft.sections[s_idx].items[i_idx]
        worst = _worst_status(statuses)

        if worst == "VERIFIED":
            verified_total += 1
            # confidence unchanged

        elif worst == "PARTIAL":
            partial_total += 1
            item.confidence = round(item.confidence * _PARTIAL_CONFIDENCE, 3)
            item.correction_note = _collect_reasons(statuses, "PARTIAL")

        else:  # UNVERIFIED — attempt correction loop
            corrected = await _attempt_correction(
                item, source_docs, synthesis.track.value, client, model_id
            )
            if corrected:
                partial_total += 1  # corrected → treat as partial
                item.confidence = round(item.confidence * _PARTIAL_CONFIDENCE, 3)
                item.correction_note = f"[Corrected] {corrected}"
            else:
                unverified_total += 1
                item.verified = False
                item.confidence = 0.0
                item.correction_note = _collect_reasons(statuses, "UNVERIFIED")

    total_checked = len(item_results)
    # Use 0.0 when nothing was checked but items exist — avoids vacuous 100% coverage
    coverage_rate = (verified_total + partial_total) / total_checked if total_checked else 0.0

    logger.info(
        f"Track {synthesis.track.value}: Pass 3 complete — "
        f"verified={verified_total}, partial={partial_total}, "
        f"unverified={unverified_total}, coverage={coverage_rate:.1%}"
    )

    if coverage_rate < 0.90:
        logger.warning(
            f"Track {synthesis.track.value}: citation coverage {coverage_rate:.1%} below 90% threshold"
        )

    # Sync confidence/verified back to synthesis.items (if populated in Pass 2)
    _sync_items_from_draft(synthesis, draft)

    return synthesis


async def _attempt_correction(
    item: DraftBriefingItem,
    source_docs: list[dict],
    track_id: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> Optional[str]:
    """
    Targeted re-lookup for an UNVERIFIED item.

    Asks Claude to search specifically for the item's heading claim in the
    provided documents. Returns a corrected summary string if a correction
    is found, or None if still unverifiable.
    """
    prompt = (
        f"The following briefing item could NOT be verified:\n\n"
        f"HEADING: {item.heading}\n"
        f"SUMMARY: {item.summary}\n"
        f"SOURCE URL: {item.source_url}\n\n"
        f"Search the provided source documents for any evidence that supports, "
        f"contradicts, or partially confirms this claim. "
        f"If you find a correction or clarification, write it as one sentence starting "
        f"with 'CORRECTION:'. If there is truly no supporting evidence, write 'NO_EVIDENCE'."
    )

    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=512,
            temperature=0.0,
            system="You are a fact-checker. Search documents carefully and be precise. You must ONLY use information from the provided source documents. Do not use any external knowledge.",
            messages=[{
                "role": "user",
                "content": source_docs + [{"type": "text", "text": prompt}],
            }],
        )
        text = _extract_text(response.content)
        if "NO_EVIDENCE" in text.upper():
            return None
        m = re.search(r"CORRECTION:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()[:300]
        return None
    except Exception as e:
        logger.debug(f"Correction loop failed for item '{item.heading[:40]}': {e}")
        return None


# ---------------------------------------------------------------------------
# Fallback path: HTML-based (backward compat when draft is None)
# ---------------------------------------------------------------------------

async def _factcheck_html_fallback(
    synthesis: SynthesisResult,
    source_docs: list[dict],
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> SynthesisResult:
    """Legacy fact-check path: parse claims from raw HTML."""
    claims = _extract_verifiable_claims(synthesis.raw_html)

    if not claims:
        logger.info(f"Track {synthesis.track.value}: Pass 3 (HTML fallback) — no claims to verify")
        return synthesis

    logger.info(
        f"Track {synthesis.track.value}: Pass 3 (HTML fallback) — "
        f"verifying {len(claims)} claims"
    )

    batch_size = 20
    verified_count = 0
    unverified_claims: list[str] = []

    for batch_start in range(0, len(claims), batch_size):
        batch = claims[batch_start : batch_start + batch_size]
        results = await _verify_claim_batch(batch, source_docs, synthesis.track.value, client, model_id)
        for claim, (status, _) in zip(batch, results):
            if status in ("VERIFIED", "PARTIAL"):
                verified_count += 1
            else:
                unverified_claims.append(claim[:80])

    coverage_rate = verified_count / len(claims) if claims else 1.0
    logger.info(
        f"Track {synthesis.track.value}: Pass 3 (HTML fallback) — coverage={coverage_rate:.1%}"
    )

    if coverage_rate < 0.90:
        synthesis.raw_html = _annotate_unverified(synthesis.raw_html, unverified_claims)

    return synthesis


# ---------------------------------------------------------------------------
# Shared verification helpers
# ---------------------------------------------------------------------------

async def _verify_claim_batch(
    claims: list[str],
    source_docs: list[dict],
    track_id: str,
    client: anthropic.AsyncAnthropic,
    model_id: str,
) -> list[tuple[str, str]]:
    """
    Verify a batch of claim strings against source documents.

    Primary path: tool use with submit_verification schema (100% machine-parseable).
    Fallback path: regex parsing of freeform text (backward compatibility).

    Returns a list of (status, reason) tuples aligned with the input claims.
    Status is one of: VERIFIED | PARTIAL | UNVERIFIED.
    """
    claims_text = "\n".join(f"[{i+1}] {c}" for i, c in enumerate(claims))

    system_prompt = (
        "You are a precise fact-checker. Your only job is to verify claims against "
        "provided source documents. Do not use external knowledge. "
        "Do not use any information not present in the provided documents.\n\n"
        "For each claim, determine if it is VERIFIED (clearly supported), "
        "PARTIAL (partially supported or unclear), or UNVERIFIED (not found or contradicted). "
        "Call the submit_verification tool with results for ALL claims."
    )

    user_text = (
        f"Verify each claim against the source documents above.\n\n"
        f"Claims:\n{claims_text}"
    )

    # --- Primary path: tool use ---
    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=2048,
            temperature=0.0,
            tools=[_VERIFY_CLAIMS_TOOL],
            tool_choice={"type": "any"},
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": source_docs + [{"type": "text", "text": user_text}],
            }],
        )

        # Extract tool use block
        for block in response.content:
            if hasattr(block, "type") and block.type == "tool_use" and block.name == "submit_verification":
                return _parse_tool_verification(claims, block.input)

        # Tool block not found — fall through to text parsing
        response_text = _extract_text(response.content)
        if response_text.strip():
            logger.debug("Fact-check tool use returned no tool block; falling back to regex parsing")
            return _parse_batch_response(claims, response_text)

    except Exception as e:
        logger.warning(
            f"Claim batch verification (tool use) failed: {e}. "
            f"Attempting regex fallback."
        )
        # --- Fallback: text-only call with regex parsing ---
        try:
            response = await client.messages.create(
                model=model_id,
                max_tokens=2048,
                temperature=0.0,
                system=(
                    "You are a precise fact-checker. Your only job is to verify claims against "
                    "provided source documents. Do not use external knowledge.\n\n"
                    "For each claim respond EXACTLY as:\n"
                    "[N] STATUS: one-sentence reason\n"
                    "STATUS must be VERIFIED, PARTIAL, or UNVERIFIED."
                ),
                messages=[{
                    "role": "user",
                    "content": source_docs + [{"type": "text", "text": user_text}],
                }],
            )
            response_text = _extract_text(response.content)
            return _parse_batch_response(claims, response_text)
        except Exception as e2:
            logger.warning(
                f"Claim batch verification fallback also failed: {e2}. "
                f"Marking {len(claims)} claims UNVERIFIED (conservative)."
            )

    return [("UNVERIFIED", "verification failed") for _ in claims]


def _parse_tool_verification(
    claims: list[str],
    tool_input: dict,
) -> list[tuple[str, str]]:
    """
    Parse the submit_verification tool input into aligned (status, reason) tuples.
    """
    valid_statuses = {"VERIFIED", "PARTIAL", "UNVERIFIED"}
    # Build a dict of claim_number → (status, reason)
    result_map: dict[int, tuple[str, str]] = {}
    for entry in tool_input.get("results", []):
        n = entry.get("claim_number")
        status = str(entry.get("status", "UNVERIFIED")).upper()
        reason = str(entry.get("reason", ""))[:200]
        if isinstance(n, int) and status in valid_statuses:
            result_map[n] = (status, reason)

    # Return aligned list; default UNVERIFIED for any missing entries
    return [
        result_map.get(i + 1, ("UNVERIFIED", "not returned by verifier"))
        for i in range(len(claims))
    ]


def _parse_batch_response(
    claims: list[str],
    response_text: str,
) -> list[tuple[str, str]]:
    """
    Parse Claude's freeform batch verification response (fallback path).
    Returns aligned list of (status, reason) tuples.
    """
    results: list[tuple[str, str]] = []

    for i in range(len(claims)):
        n = i + 1
        # Match [N] STATUS: reason
        pattern = rf"\[{n}\]\s*(VERIFIED|UNVERIFIED|PARTIAL)\s*:?\s*(.+?)(?=\[{n+1}\]|$)"
        m = re.search(pattern, response_text, re.IGNORECASE | re.DOTALL)
        if m:
            status = m.group(1).upper()
            reason = m.group(2).strip()[:200]
            results.append((status, reason))
        else:
            fallback = re.search(rf"\[{n}\]\s*(VERIFIED|UNVERIFIED|PARTIAL)", response_text, re.I)
            if fallback:
                results.append((fallback.group(1).upper(), ""))
            else:
                results.append(("UNVERIFIED", "no response found for this claim"))

    return results


def _extract_item_claims(item: DraftBriefingItem) -> list[str]:
    """
    Extract verifiable claims from a DraftBriefingItem.

    Returns 0–5 specific claims per item:
    - heading (always verifiable)
    - date_str + heading compound if date present
    - up to 2 fact-dense sentences from summary
    - compound claims split on conjunctions within a summary sentence
    """
    claims: list[str] = []

    # The heading is the primary claim
    if item.heading:
        claims.append(item.heading)

    # Date-anchored compound claim
    if item.date_str and item.heading:
        claims.append(f"{item.date_str}: {item.heading}")

    # Extract fact-dense sentences from summary (up to 2 per item)
    if item.summary:
        verifiable_patterns = [
            re.compile(r"\d{4}"),
            re.compile(r"\$[\d,]+"),
            re.compile(r"\d+%"),
            re.compile(r"\b(?:announced|launched|raised|acquired|partnered|released|signed|expanded|merged)\b", re.I),
            re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b", re.I),
        ]
        sentences = re.split(r"(?<=[.!?])\s+", item.summary)
        summary_claims_added = 0
        for sent in sentences:
            sent = sent.strip()
            if len(sent) < 20:
                continue
            if not any(p.search(sent) for p in verifiable_patterns):
                continue
            # Check if sentence contains a compound assertion — split on conjunctions
            # e.g. "X acquired Y for $100M and will integrate by Q3" → two claims
            compound_parts = re.split(r"\s+(?:and|while|which|who)\s+", sent, flags=re.I)
            if len(compound_parts) > 1:
                for part in compound_parts:
                    part = part.strip()
                    if len(part) >= 20 and any(p.search(part) for p in verifiable_patterns):
                        claims.append(part[:300])
                        summary_claims_added += 1
                        if summary_claims_added >= 2:
                            break
            else:
                claims.append(sent[:300])
                summary_claims_added += 1
            if summary_claims_added >= 2:
                break

    return claims[:5]


def _worst_status(statuses: list[tuple[str, str]]) -> str:
    """Return the worst verification status across all claims for an item."""
    if any(s == "UNVERIFIED" for s, _ in statuses):
        return "UNVERIFIED"
    if any(s == "PARTIAL" for s, _ in statuses):
        return "PARTIAL"
    return "VERIFIED"


def _collect_reasons(statuses: list[tuple[str, str]], for_status: str) -> str:
    """Collect reason strings for a given status."""
    reasons = [r for s, r in statuses if s == for_status and r]
    return "; ".join(reasons[:3]) if reasons else ""


def _extract_text(content_blocks) -> str:
    """Extract text from an Anthropic response content block list."""
    return "".join(
        block.text
        for block in content_blocks
        if hasattr(block, "type") and block.type == "text"
    )


def _sync_items_from_draft(synthesis: SynthesisResult, draft: SynthesisDraft) -> None:
    """
    Sync confidence and verified flags back to synthesis.items.

    Pass 2 already populated synthesis.items from the draft.  After Pass 3
    mutates the DraftBriefingItem objects, this propagates those changes to
    the BriefingItem list so Pass 3.5 and Pass 4 see consistent data.
    """
    if not synthesis.items:
        return

    # Build a lookup: heading → DraftBriefingItem
    draft_lookup: dict[str, DraftBriefingItem] = {}
    for section in draft.sections:
        for item in section.items:
            draft_lookup[item.heading.lower().strip()] = item

    for brief_item in synthesis.items:
        key = brief_item.heading.lower().strip()
        if key in draft_lookup:
            draft_item = draft_lookup[key]
            brief_item.confidence_score = draft_item.confidence


# ---------------------------------------------------------------------------
# Source document builder
# ---------------------------------------------------------------------------

def _build_verification_docs(gathered: GatheredData, source_max_chars: int) -> list[dict]:
    """
    Build compact document blocks for fact verification.

    Includes scraped page content (up to 30) and discovered article snippets
    for any URL not already covered by a scraped source.
    """
    docs = []

    for source in gathered.scraped_sources[:30]:
        if source.error or not source.content:
            continue
        docs.append({
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": source.content[:source_max_chars],
            },
            "title": source.title[:100] if source.title else "Source",
            "citations": {"enabled": True},
        })

    scraped_urls = {s.url for s in gathered.scraped_sources if not s.error}
    for article in gathered.discovered_articles:
        if article.url in scraped_urls or not article.snippet:
            continue
        docs.append({
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": article.snippet[:source_max_chars],
            },
            "title": (article.title or article.url)[:100],
            "citations": {"enabled": True},
        })

    return docs


# ---------------------------------------------------------------------------
# HTML fallback helpers (backward compat)
# ---------------------------------------------------------------------------

def _extract_verifiable_claims(html: str) -> list[str]:
    """Extract factual claims from raw HTML for the legacy fallback path."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    sentences = re.split(r"(?<=[.!?])\s+", text)

    verifiable_patterns = [
        re.compile(r"\d{4}"),
        re.compile(r"\$[\d,]+"),
        re.compile(r"\d+%"),
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

    return claims[:_CLAIM_CAP]


def _annotate_unverified(html: str, unverified: list[str]) -> str:
    """Add HTML comment annotations marking unverified claims (legacy fallback)."""
    if not unverified:
        return html
    annotation = (
        "\n<!-- FACT-CHECK NOTES: The following claims could not be verified "
        "against source documents and may require manual review:\n"
        + "\n".join(f"  - {c}" for c in unverified[:10])
        + "\n-->\n"
    )
    return html + annotation
