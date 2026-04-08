"""
Token budget estimation for Anthropic API calls.

Provides a fast, dependency-free token count estimator so Pass 2 and Pass 3
can warn (and auto-trim) before hitting the context window limit.

Estimation method: ~3.5 characters per token for English text (conservative
approximation; actual tiktoken values vary 3.2–4.0 for prose).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Conservative chars-per-token ratio for English prose
_CHARS_PER_TOKEN: float = 3.5

# Anthropic Opus 4.6 context window
_OPUS_CONTEXT_TOKENS: int = 200_000

# Reserved for output (max_tokens) + overhead (system prompt, tool schema)
_RESERVED_OUTPUT_TOKENS: int = 20_000


def estimate_tokens(text: str) -> int:
    """Rough token count for a text string (~3.5 chars/token)."""
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Rough token estimate for a messages array (text content only)."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(block.get("data", "") or block.get("text", ""))
    return total


def trim_documents_to_budget(
    documents: list[dict],
    system_prompt: str,
    user_text: str,
    max_tokens: int = _OPUS_CONTEXT_TOKENS,
    reserved_output: int = _RESERVED_OUTPUT_TOKENS,
    label: str = "",
) -> list[dict]:
    """
    Trim a document list to fit within the context window budget.

    Removes documents from the end of the list (lowest priority) until the
    estimated total token count fits within the budget. Logs a warning if
    trimming occurs.

    Args:
        documents: List of document blocks (Pass 2 / Pass 3 format).
        system_prompt: The system prompt string.
        user_text: The user message text (excluding documents).
        max_tokens: Context window size.
        reserved_output: Tokens reserved for output + overhead.
        label: Prefix for log messages (e.g. "Track A Pass 2").

    Returns:
        Possibly-trimmed copy of documents.
    """
    budget = max_tokens - reserved_output
    overhead = estimate_tokens(system_prompt) + estimate_tokens(user_text)

    if overhead >= budget:
        logger.warning(
            f"{label}: system prompt + user text alone ({overhead} est. tokens) "
            f"exceeds input budget ({budget}). Proceeding with 0 documents."
        )
        return []

    available = budget - overhead
    docs = list(documents)
    total_doc_tokens = sum(
        estimate_tokens(d.get("source", {}).get("data", "") or "")
        for d in docs
    )

    if total_doc_tokens <= available:
        return docs  # fits without trimming

    # Trim from the end (lowest-priority documents removed first)
    trimmed = 0
    while docs and total_doc_tokens > available:
        removed = docs.pop()
        removed_tokens = estimate_tokens(
            removed.get("source", {}).get("data", "") or ""
        )
        total_doc_tokens -= removed_tokens
        trimmed += 1

    logger.warning(
        f"{label}: trimmed {trimmed} source documents to fit context budget "
        f"(~{total_doc_tokens + overhead} / {budget} est. tokens used)"
    )
    return docs
