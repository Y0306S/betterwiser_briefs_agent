"""
Robust JSON array extractor for LLM responses.

LLM responses often contain a JSON array wrapped in explanatory text or markdown.
This module provides a reliable extractor that handles:
- Arrays preceded/followed by prose text
- Nested arrays and objects within the target array
- Markdown code fences (```json ... ```)
- Multiple JSON blocks (returns the largest valid array)
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json_array(text: str) -> list:
    """
    Extract the first valid JSON array from an LLM response string.

    Algorithm:
    1. Try to parse the whole string as JSON first (fastest path).
    2. Strip markdown code fences and retry.
    3. Use bracket-depth tracking to find the correct array boundaries,
       handling nested objects/arrays correctly.
    4. Return the largest valid array found among all candidates.

    Args:
        text: Raw string from an LLM response.

    Returns:
        Parsed list, or [] if no valid JSON array is found.
    """
    if not text or not text.strip():
        return []

    # Fast path: entire response is valid JSON
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    stripped = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    stripped = re.sub(r"```", "", stripped).strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Bracket-depth scan: find all candidate [...]  spans
    candidates: list[list] = []
    i = 0
    while i < len(stripped):
        if stripped[i] == "[":
            depth = 0
            in_string = False
            escape_next = False
            j = i
            while j < len(stripped):
                ch = stripped[j]
                if escape_next:
                    escape_next = False
                elif ch == "\\" and in_string:
                    escape_next = True
                elif ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == "[" or ch == "{":
                        depth += 1
                    elif ch == "]" or ch == "}":
                        depth -= 1
                        if depth == 0:
                            candidate_str = stripped[i:j + 1]
                            try:
                                parsed = json.loads(candidate_str)
                                if isinstance(parsed, list):
                                    candidates.append(parsed)
                            except (json.JSONDecodeError, ValueError):
                                pass
                            break
                j += 1
        i += 1

    if not candidates:
        logger.debug(f"json_extractor: no valid JSON array found in text (len={len(text)})")
        return []

    # Return the largest candidate (most items = most useful)
    return max(candidates, key=len)
