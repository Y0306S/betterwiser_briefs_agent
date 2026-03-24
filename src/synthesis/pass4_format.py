"""
Pass 4: HTML formatting, link validation, and email packaging.

Takes the grounded synthesis and produces:
1. Outlook-compatible HTML (inline CSS, table layout, no flex/grid)
2. Validated links (HEAD requests; Wayback Machine fallback for dead links)
3. Final ValidatedBriefing ready for delivery

Input:  SynthesisResult + GroundingReport
Output: ValidatedBriefing
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from src.schemas import (
    BriefingTrack,
    GroundingReport,
    LinkCheckResult,
    SynthesisResult,
    ValidatedBriefing,
)

logger = logging.getLogger(__name__)

# Email brand colours
BW_NAVY = "#1B2A4A"
BW_TEAL = "#00B4C8"
BW_LIGHT_GREY = "#F7F8FA"
BW_DARK_GREY = "#4A4A4A"


async def format_and_validate(
    synthesis: SynthesisResult,
    grounding_report: GroundingReport,
    month: str,
    subject_template: str = "",
) -> ValidatedBriefing:
    """
    Format the synthesised briefing as Outlook-compatible HTML and validate links.

    Args:
        synthesis: Output from Pass 3.5 with raw_html.
        grounding_report: Grounding report from Pass 3.5.
        month: Target month "YYYY-MM".
        subject_template: Email subject line template string.

    Returns:
        ValidatedBriefing with final_html, subject_line, link_results, held_for_review.
    """
    track = synthesis.track

    # Validate all links found in the HTML
    urls_in_html = _extract_urls_from_html(synthesis.raw_html)
    logger.info(f"Track {track.value}: Pass 4 — validating {len(urls_in_html)} links")
    link_results = await _validate_links(urls_in_html)

    # Build map of dead URL → Wayback fallback
    url_replacements = _build_url_replacements(link_results)

    # Apply URL replacements to the HTML
    html_with_valid_links = _replace_dead_links(synthesis.raw_html, url_replacements)

    # Wrap in BetterWiser email template
    month_human = _month_human(month)
    track_name = _track_name(track)
    subject_line = (
        subject_template.replace("{month_human}", month_human)
        if subject_template
        else f"BetterWiser Legal Intelligence — {track_name} — {month_human}"
    )

    final_html = _wrap_in_email_template(
        html_with_valid_links,
        track=track,
        month_human=month_human,
        track_name=track_name,
    )

    held_for_review = grounding_report.below_threshold

    if held_for_review:
        logger.warning(
            f"Track {track.value}: ValidatedBriefing held for human review "
            f"(grounding={grounding_report.pass_rate:.1%})"
        )

    dead_links = sum(1 for r in link_results if not r.reachable)
    if dead_links:
        logger.info(f"Track {track.value}: {dead_links} dead links replaced/noted")

    return ValidatedBriefing(
        synthesis=synthesis,
        grounding_report=grounding_report,
        link_results=link_results,
        final_html=final_html,
        subject_line=subject_line,
        held_for_review=held_for_review,
        ready_to_send=not held_for_review,
    )


async def _validate_links(
    urls: list[str],
    concurrency: int = 10,
    timeout: float = 10.0,
) -> list[LinkCheckResult]:
    """Validate all URLs with concurrent HEAD requests."""
    if not urls:
        return []

    semaphore = asyncio.Semaphore(concurrency)

    async def check_url(url: str) -> LinkCheckResult:
        async with semaphore:
            return await _check_single_url(url, timeout)

    results = await asyncio.gather(*[check_url(u) for u in urls], return_exceptions=True)

    link_results: list[LinkCheckResult] = []
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            link_results.append(LinkCheckResult(
                url=url, reachable=False, error=str(result)
            ))
        else:
            link_results.append(result)  # type: ignore[arg-type]

    return link_results


async def _check_single_url(url: str, timeout: float) -> LinkCheckResult:
    """Check a single URL with HEAD request, falling back to GET."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 BetterWiser-BriefingAgent/1.0"},
        ) as client:
            # Try HEAD first (faster)
            try:
                response = await client.head(url)
            except Exception:
                # Some servers don't support HEAD — fallback to GET
                response = await client.get(url)

            reachable = response.status_code < 400
            redirect_url = str(response.url) if str(response.url) != url else None

            return LinkCheckResult(
                url=url,
                status_code=response.status_code,
                reachable=reachable,
                redirect_url=redirect_url,
                wayback_fallback=None if reachable else _wayback_url(url),
            )

    except httpx.TimeoutException:
        return LinkCheckResult(url=url, reachable=False, error="Timeout")
    except Exception as e:
        return LinkCheckResult(url=url, reachable=False, error=str(e)[:100])


def _wayback_url(url: str) -> str:
    """Generate a Wayback Machine fallback URL for a dead link."""
    return f"https://web.archive.org/web/*/{url}"


def _build_url_replacements(link_results: list[LinkCheckResult]) -> dict[str, str]:
    """Build a dict of dead URL → replacement URL."""
    replacements = {}
    for result in link_results:
        if not result.reachable and result.wayback_fallback:
            replacements[result.url] = result.wayback_fallback
    return replacements


def _replace_dead_links(html: str, replacements: dict[str, str]) -> str:
    """Replace dead URLs in HTML with their Wayback Machine fallbacks."""
    for dead_url, replacement in replacements.items():
        html = html.replace(f'href="{dead_url}"', f'href="{replacement}"')
        html = html.replace(f"href='{dead_url}'", f"href='{replacement}'")
    return html


def _extract_urls_from_html(html: str) -> list[str]:
    """Extract all unique HTTP/HTTPS URLs from HTML href attributes."""
    pattern = re.compile(r'href=["\']?(https?://[^\s"\'<>]+)["\']?', re.IGNORECASE)
    urls = pattern.findall(html)
    # Deduplicate
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _wrap_in_email_template(
    content_html: str,
    track: BriefingTrack,
    month_human: str,
    track_name: str,
) -> str:
    """
    Wrap the briefing content in a BetterWiser-branded Outlook-compatible HTML template.

    Uses table-based layout for Outlook compatibility. All CSS is inline.
    """
    track_badge_colour = {
        BriefingTrack.A: "#E8F4F8",
        BriefingTrack.B: "#F0F8E8",
        BriefingTrack.C: "#FFF4E8",
    }.get(track, BW_LIGHT_GREY)

    track_accent = {
        BriefingTrack.A: "#0078A8",
        BriefingTrack.B: "#4A8A28",
        BriefingTrack.C: "#C87800",
    }.get(track, BW_TEAL)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BetterWiser Legal Intelligence — {track_name} — {month_human}</title>
</head>
<body style="margin:0;padding:0;background-color:#F0F0F0;font-family:Calibri,Arial,sans-serif;">

<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F0F0F0;">
<tr><td align="center" style="padding:20px 10px;">

<!-- Email container -->
<table width="680" cellpadding="0" cellspacing="0" border="0"
       style="background-color:#FFFFFF;border-radius:4px;overflow:hidden;
              box-shadow:0 1px 3px rgba(0,0,0,0.1);">

<!-- Header -->
<tr>
  <td style="background-color:{BW_NAVY};padding:24px 32px;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td>
        <p style="margin:0;font-size:22px;font-weight:bold;color:#FFFFFF;
                  font-family:Calibri,Arial,sans-serif;letter-spacing:0.5px;">
          BetterWiser
        </p>
        <p style="margin:4px 0 0 0;font-size:12px;color:{BW_TEAL};
                  font-family:Calibri,Arial,sans-serif;letter-spacing:1px;text-transform:uppercase;">
          Legal Intelligence
        </p>
      </td>
      <td align="right" style="vertical-align:middle;">
        <span style="background-color:{track_accent};color:#FFFFFF;padding:4px 12px;
                     border-radius:3px;font-size:11px;font-weight:bold;
                     font-family:Calibri,Arial,sans-serif;letter-spacing:0.5px;">
          TRACK {track.value}
        </span>
      </td>
    </tr>
    </table>
  </td>
</tr>

<!-- Title bar -->
<tr>
  <td style="background-color:{track_badge_colour};padding:16px 32px;
             border-bottom:2px solid {track_accent};">
    <p style="margin:0;font-size:16px;font-weight:bold;color:{BW_NAVY};
              font-family:Calibri,Arial,sans-serif;">
      {track_name}
    </p>
    <p style="margin:4px 0 0 0;font-size:13px;color:{BW_DARK_GREY};
              font-family:Calibri,Arial,sans-serif;">
      {month_human} Edition
    </p>
  </td>
</tr>

<!-- Content -->
<tr>
  <td style="padding:24px 32px;color:{BW_DARK_GREY};font-family:Calibri,Arial,sans-serif;
             font-size:14px;line-height:1.6;">
    {_normalise_content_html(content_html)}
  </td>
</tr>

<!-- Footer -->
<tr>
  <td style="background-color:{BW_NAVY};padding:16px 32px;border-top:1px solid {BW_TEAL};">
    <p style="margin:0;font-size:11px;color:#A0A8B8;font-family:Calibri,Arial,sans-serif;
              line-height:1.5;">
      This briefing is generated by the BetterWiser AI Briefing Agent for internal use.
      Produced automatically — verify key facts before external use.
      <br>BetterWiser Pte. Ltd. · Singapore
    </p>
  </td>
</tr>

</table>
<!-- /Email container -->

</td></tr>
</table>
<!-- /Outer wrapper -->

</body>
</html>"""


def _normalise_content_html(html: str) -> str:
    """
    Apply inline styles to common HTML elements for Outlook compatibility.
    Converts CSS classes/tag defaults to inline styles.
    """
    # Apply inline styles to headings
    html = re.sub(
        r"<h1([^>]*)>",
        r'<h1\1 style="font-size:20px;font-weight:bold;color:#1B2A4A;margin:24px 0 8px 0;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<h2([^>]*)>",
        r'<h2\1 style="font-size:17px;font-weight:bold;color:#1B2A4A;margin:20px 0 8px 0;'
        r'padding-bottom:4px;border-bottom:1px solid #E0E0E0;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<h3([^>]*)>",
        r'<h3\1 style="font-size:15px;font-weight:bold;color:#0078A8;margin:16px 0 6px 0;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<p([^>]*)>",
        r'<p\1 style="margin:0 0 10px 0;line-height:1.6;color:#4A4A4A;font-size:14px;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<ul([^>]*)>",
        r'<ul\1 style="margin:0 0 12px 0;padding-left:20px;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<li([^>]*)>",
        r'<li\1 style="margin-bottom:8px;line-height:1.6;color:#4A4A4A;font-size:14px;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<a ([^>]*href=[^>]+)>",
        lambda m: f'<a {m.group(1)} style="color:#0078A8;text-decoration:none;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<strong([^>]*)>",
        r'<strong\1 style="font-weight:bold;color:#1B2A4A;">',
        html, flags=re.IGNORECASE
    )
    # Remove divs that break Outlook layout
    html = re.sub(r"<div([^>]*)>", r"<p\1>", html, flags=re.IGNORECASE)
    html = re.sub(r"</div>", "</p>", html, flags=re.IGNORECASE)

    return html


def _month_human(month: str) -> str:
    from datetime import datetime
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month


def _track_name(track: BriefingTrack) -> str:
    names = {
        BriefingTrack.A: "Vendor & Customer Intelligence",
        BriefingTrack.B: "Global AI Policy Watch",
        BriefingTrack.C: "Thought Leadership Digest",
    }
    return names.get(track, f"Track {track.value}")
