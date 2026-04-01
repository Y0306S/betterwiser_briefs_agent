"""
Wayback Machine CDX API helper.

Before substituting a dead link with a Wayback fallback URL, this module
verifies that an actual snapshot exists in the Internet Archive using the
CDX Server API.  This prevents serving broken /web/2/<url> redirect URLs
to readers when the page was never archived.

CDX API docs: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server

Public API:
  verify_and_get_wayback_url(url) → str | None
      Returns a direct snapshot URL like
      "https://web.archive.org/web/20260301120000/https://example.com/article"
      or None if no usable snapshot exists.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_CDX_API = "https://web.archive.org/cdx/search/cdx"
_WAYBACK_BASE = "https://web.archive.org/web"

# We only want HTTP 200 snapshots (not redirects or errors)
_DEFAULT_PARAMS = {
    "output": "json",
    "limit": "1",
    "fl": "timestamp,statuscode",
    "filter": "statuscode:200",
    "fastLatest": "true",
    "collapse": "urlkey",
}


async def verify_and_get_wayback_url(
    url: str,
    timeout: float = 8.0,
) -> Optional[str]:
    """
    Check the Wayback CDX API for an archived snapshot of `url`.

    Returns the direct snapshot URL if one exists with HTTP 200, or None.

    Args:
        url:     The dead URL to look up in the archive.
        timeout: HTTP timeout for the CDX API call.

    Returns:
        A direct snapshot URL string, or None if no snapshot found.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None

    params = {**_DEFAULT_PARAMS, "url": url}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_CDX_API, params=params)
            response.raise_for_status()
            data = response.json()

    except httpx.TimeoutException:
        logger.debug(f"Wayback CDX timeout for {url}")
        return None
    except httpx.HTTPStatusError as e:
        logger.debug(f"Wayback CDX HTTP error {e.response.status_code} for {url}")
        return None
    except Exception as e:
        logger.debug(f"Wayback CDX error for {url}: {e}")
        return None

    # CDX returns a list of [header_row, result_row...] or just [header_row]
    # header: ["timestamp", "statuscode"]
    if not isinstance(data, list) or len(data) < 2:
        logger.debug(f"Wayback CDX: no snapshot found for {url}")
        return None

    # data[0] is the header row; data[1] is the first result
    result_row = data[1]
    if len(result_row) < 2:
        return None

    timestamp, statuscode = result_row[0], result_row[1]

    if statuscode != "200":
        logger.debug(
            f"Wayback CDX: only non-200 snapshots (status={statuscode}) for {url}"
        )
        return None

    wayback_url = f"{_WAYBACK_BASE}/{timestamp}/{url}"
    logger.debug(f"Wayback CDX: snapshot found at {wayback_url}")
    return wayback_url


async def batch_verify(
    urls: list[str],
    concurrency: int = 5,
    timeout: float = 8.0,
) -> dict[str, Optional[str]]:
    """
    Verify multiple dead URLs concurrently.

    Args:
        urls:        List of dead URLs to look up.
        concurrency: Max simultaneous CDX API calls.
        timeout:     Per-request timeout.

    Returns:
        Dict mapping each input URL to its Wayback snapshot URL (or None).
    """
    import asyncio

    if not urls:
        return {}

    semaphore = asyncio.Semaphore(concurrency)

    async def _check(url: str) -> tuple[str, Optional[str]]:
        async with semaphore:
            result = await verify_and_get_wayback_url(url, timeout)
            return url, result

    pairs = await asyncio.gather(*[_check(u) for u in urls])
    return dict(pairs)
