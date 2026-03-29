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

    # Check that every briefing entry carries at least one citation URL.
    # Entries without citations are logged as warnings and annotated in the HTML
    # so reviewers can spot and fix them before the briefing is sent.
    html_with_citations, uncited_count = _enforce_citation_coverage(synthesis.raw_html, urls_in_html)
    if uncited_count:
        logger.warning(
            f"Track {track.value}: {uncited_count} entry/entries appear to have no "
            f"source URL attached — annotated in HTML for review"
        )
    else:
        logger.info(f"Track {track.value}: citation coverage check passed")

    # Build map of dead URL → Wayback fallback
    url_replacements = _build_url_replacements(link_results)

    # Apply URL replacements to the HTML
    html_with_valid_links = _replace_dead_links(html_with_citations, url_replacements)

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
        source_count=len(urls_in_html),
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
    # Use /web/2/ to redirect to the most recent archived snapshot
    return f"https://web.archive.org/web/2/{url}"


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
    source_count: int = 0,
) -> str:
    """
    Wrap the briefing content in a BetterWiser-branded Outlook-compatible HTML template.

    Style: Structured Executive Brief (Option A) — consulting/McKinsey report format.
    Uses table-based layout for Outlook compatibility. All CSS is inline.
    """
    track_accent = {
        BriefingTrack.A: "#0078A8",
        BriefingTrack.B: "#4A8A28",
        BriefingTrack.C: "#C87800",
    }.get(track, "#C87800")

    source_note = f"{source_count} sources cited" if source_count else "sources cited"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BetterWiser Legal Intelligence — {track_name} — {month_human}</title>
</head>
<body style="margin:0;padding:0;background-color:#ECECEC;font-family:Calibri,Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#ECECEC;">
<tr><td align="center" style="padding:24px 12px;">

<table width="680" cellpadding="0" cellspacing="0" border="0"
       style="background-color:#FFFFFF;border-radius:4px;">

  <!-- Header band -->
  <tr>
    <td style="background-color:{BW_NAVY};padding:28px 36px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td>
          <p style="margin:0;font-size:24px;font-weight:bold;color:#FFFFFF;
                    font-family:Calibri,Arial,sans-serif;letter-spacing:0.5px;">BetterWiser</p>
          <p style="margin:5px 0 0 0;font-size:11px;color:{BW_TEAL};letter-spacing:2px;
                    text-transform:uppercase;font-family:Calibri,Arial,sans-serif;">Legal Intelligence</p>
        </td>
        <td align="right" style="vertical-align:top;">
          <p style="margin:0;font-size:12px;color:#A0B0C8;font-family:Calibri,Arial,sans-serif;">
            Track {track.value} — {track_name}
          </p>
          <p style="margin:4px 0 0 0;font-size:12px;color:#A0B0C8;font-family:Calibri,Arial,sans-serif;">
            {month_human} Edition
          </p>
        </td>
      </tr></table>
    </td>
  </tr>

  <!-- Accent bar -->
  <tr>
    <td style="background-color:{track_accent};height:3px;font-size:1px;line-height:1px;">&nbsp;</td>
  </tr>

  <!-- Summary bar -->
  <tr>
    <td style="background-color:#FFF9F0;padding:16px 36px;border-bottom:1px solid #E8E0D0;">
      <p style="margin:0;font-size:13px;color:#5A4A28;font-family:Calibri,Arial,sans-serif;line-height:1.5;">
        <strong>This month:</strong> Track {track.value} — {track_name}
        &nbsp;·&nbsp; {month_human} &nbsp;·&nbsp; {source_note}
      </p>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:28px 36px;">
      {_normalise_content_html(content_html)}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background-color:{BW_NAVY};padding:16px 36px;border-top:2px solid {BW_TEAL};">
      <p style="margin:0;font-size:11px;color:#7A8AA0;font-family:Calibri,Arial,sans-serif;line-height:1.5;">
        BetterWiser Legal Intelligence &nbsp;·&nbsp; {month_human} &nbsp;·&nbsp;
        Generated automatically — verify before external use.
        &nbsp;·&nbsp; BetterWiser Pte. Ltd., Singapore
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""


def _normalise_content_html(html: str) -> str:
    """
    Apply inline styles to common HTML elements for Outlook compatibility.

    Styled to match the Structured Executive Brief (Option A) format:
    - Theme headings (h2) with orange bottom border
    - Briefing items (li) with orange left-border accent, no bullets
    - Citation links (a) in orange
    - Superscript citation numbers in orange
    - Opinion/Relevance labels (strong) in navy
    """
    # Skip elements that already carry a style attribute
    html = re.sub(
        r"<h1(?![^>]*\bstyle=)([^>]*)>",
        r'<h1\1 style="font-size:20px;font-weight:bold;color:#1B2A4A;'
        r'font-family:Calibri,Arial,sans-serif;margin:24px 0 12px 0;">',
        html, flags=re.IGNORECASE
    )
    # Theme heading — orange underline, matches Option A section dividers
    html = re.sub(
        r"<h2(?![^>]*\bstyle=)([^>]*)>",
        r'<h2\1 style="font-size:17px;font-weight:bold;color:#1B2A4A;'
        r'font-family:Calibri,Arial,sans-serif;margin:0 0 16px 0;'
        r'padding-bottom:8px;border-bottom:2px solid #C87800;">',
        html, flags=re.IGNORECASE
    )
    # Sub-label (e.g. "Theme 01" eyebrow) — uppercase orange
    html = re.sub(
        r"<h3(?![^>]*\bstyle=)([^>]*)>",
        r'<h3\1 style="font-size:11px;font-weight:bold;color:#C87800;'
        r'font-family:Calibri,Arial,sans-serif;letter-spacing:2px;'
        r'text-transform:uppercase;margin:24px 0 4px 0;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<p(?![^>]*\bstyle=)([^>]*)>",
        r'<p\1 style="margin:0 0 8px 0;line-height:1.6;color:#4A4A4A;'
        r'font-size:13px;font-family:Calibri,Arial,sans-serif;">',
        html, flags=re.IGNORECASE
    )
    # Remove bullets from lists; items carry their own left-border accent
    html = re.sub(
        r"<ul(?![^>]*\bstyle=)([^>]*)>",
        r'<ul\1 style="margin:0 0 20px 0;padding:0;list-style:none;">',
        html, flags=re.IGNORECASE
    )
    # Each briefing item: orange left-border accent (Option A style)
    html = re.sub(
        r"<li(?![^>]*\bstyle=)([^>]*)>",
        r'<li\1 style="margin-bottom:20px;padding:0 0 0 16px;'
        r'border-left:3px solid #C87800;list-style:none;'
        r'font-size:13px;line-height:1.6;color:#4A4A4A;'
        r'font-family:Calibri,Arial,sans-serif;">',
        html, flags=re.IGNORECASE
    )
    # Citation and source links — orange, small, no underline (matches Option A [1] links)
    html = re.sub(
        r"<a ([^>]*href=[^>]+)>",
        lambda m: (
            f'<a {m.group(1)} style="color:#C87800;text-decoration:none;font-size:11px;">'
            if 'style=' not in m.group(1) else f'<a {m.group(1)}>'
        ),
        html, flags=re.IGNORECASE
    )
    # Bold labels ("Opinion Takeaway:", "Relevance to BetterWiser:") — navy
    html = re.sub(
        r"<strong(?![^>]*\bstyle=)([^>]*)>",
        r'<strong\1 style="font-weight:bold;color:#1B2A4A;font-family:Calibri,Arial,sans-serif;">',
        html, flags=re.IGNORECASE
    )
    # Superscript citation numbers — orange, bold (matches Option A [1] superscripts)
    html = re.sub(
        r"<sup(?![^>]*\bstyle=)([^>]*)>",
        r'<sup\1 style="font-size:10px;color:#C87800;font-weight:bold;vertical-align:super;">',
        html, flags=re.IGNORECASE
    )
    # Italic text (Opinion Takeaway body, em tags)
    html = re.sub(
        r"<em(?![^>]*\bstyle=)([^>]*)>",
        r'<em\1 style="font-style:italic;color:#666666;font-family:Calibri,Arial,sans-serif;">',
        html, flags=re.IGNORECASE
    )
    # Strip div tags to avoid invalid nesting
    html = re.sub(r"<div[^>]*>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"</div>", "", html, flags=re.IGNORECASE)

    return html


def _enforce_citation_coverage(html: str, cited_urls: list[str]) -> tuple[str, int]:
    """
    Scan each briefing entry block and flag any that contain no hyperlink.

    An "entry" is identified as a <li> or <p> that contains a bold heading
    (i.e. the formatted output of a briefing item).  Each entry is expected to
    carry at least one <a href="..."> pointing to the source.

    Any entry found without a URL gets an inline warning annotation appended so
    that the human reviewer can see exactly which items are uncited before the
    briefing leaves the system.

    Returns:
        (annotated_html, count_of_uncited_entries)
    """
    # Match each <li>...</li> or standalone <p> that opens with a bold element
    # — these are the patterns that Pass 2 produces for briefing items.
    entry_pattern = re.compile(
        r"(<(?:li|p)[^>]*>)((?:(?!</(?:li|p)>).)*?)(</(?:li|p)>)",
        re.DOTALL | re.IGNORECASE,
    )
    url_pattern = re.compile(r'href=["\']https?://', re.IGNORECASE)
    # A block qualifies as a "briefing entry" if it contains a bold tag and
    # is long enough to be substantive (avoids flagging short structural <p>s).
    bold_pattern = re.compile(r"<(?:strong|b)[^>]*>", re.IGNORECASE)

    uncited = 0

    def annotate_if_uncited(m: re.Match) -> str:
        nonlocal uncited
        open_tag, body, close_tag = m.group(1), m.group(2), m.group(3)
        text_only = re.sub(r"<[^>]+>", "", body).strip()
        if len(text_only) < 60:
            return m.group(0)  # too short to be a real entry
        if not bold_pattern.search(body):
            return m.group(0)  # no bold heading — not a briefing item
        if url_pattern.search(body):
            return m.group(0)  # already has a citation link
        # No URL found — annotate
        uncited += 1
        warning = (
            '<span style="background-color:#FFF3CD;color:#856404;font-size:11px;'
            'padding:2px 6px;border-radius:3px;margin-left:8px;">'
            '&#9888; NO SOURCE CITED'
            '</span>'
        )
        return f"{open_tag}{body} {warning}{close_tag}"

    annotated = entry_pattern.sub(annotate_if_uncited, html)
    return annotated, uncited


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
