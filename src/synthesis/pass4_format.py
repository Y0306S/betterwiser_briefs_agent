"""
Pass 4: HTML formatting, link validation, and email packaging.

Takes the grounded synthesis and produces:
1. Outlook-compatible HTML (inline CSS, table layout, no flex/grid)
2. Validated links (HEAD requests; Wayback Machine fallback for dead links)
3. Final ValidatedBriefing ready for delivery

Primary path: deterministic rendering from SynthesisDraft (structured data).
Fallback path: normalise raw_html from Claude's freeform output when draft is absent.

Input:  SynthesisResult + GroundingReport
Output: ValidatedBriefing
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from typing import Optional

import httpx

from src.schemas import (
    BriefingItem,
    BriefingTrack,
    DraftBriefingItem,
    DraftSection,
    GroundingReport,
    LinkCheckResult,
    SourceTier,
    SynthesisDraft,
    SynthesisResult,
    ValidatedBriefing,
)
from src.utils.wayback import batch_verify as _wayback_batch_verify

logger = logging.getLogger(__name__)

# Email brand colours
BW_NAVY = "#1B2A4A"
BW_TEAL = "#00B4C8"
BW_ORANGE = "#C87800"

# Feedback mailto — recipients can reply with a structured subject line
_FEEDBACK_MAILTO = "ai-briefing@betterwiser.com"


async def format_and_validate(
    synthesis: SynthesisResult,
    grounding_report: GroundingReport,
    month: str,
    subject_template: str = "",
    min_output_confidence: float = 0.5,
    exclude_confidence_below: float = 0.3,
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

    # ------------------------------------------------------------------ #
    # Primary path: render deterministically from SynthesisDraft          #
    # ------------------------------------------------------------------ #
    if synthesis.draft is not None:
        logger.info(
            f"Track {track.value}: Pass 4 — rendering from structured draft "
            f"({sum(len(s.items) for s in synthesis.draft.sections)} items)"
        )
        content_html = _format_from_draft(
            synthesis.draft,
            min_output_confidence=min_output_confidence,
            exclude_confidence_below=exclude_confidence_below,
        )
        source_count = synthesis.draft.total_sources_used
    else:
        # ------------------------------------------------------------------ #
        # Fallback path: normalise freeform raw_html                         #
        # ------------------------------------------------------------------ #
        logger.info(f"Track {track.value}: Pass 4 — rendering from raw_html (draft unavailable)")
        content_html = synthesis.raw_html
        source_count = 0

    # Validate all links found in the generated HTML
    urls_in_html = _extract_urls_from_html(content_html)
    logger.info(f"Track {track.value}: Pass 4 — validating {len(urls_in_html)} links")
    link_results = await _validate_links(urls_in_html)

    # Check citation coverage (warn on missing source links)
    html_with_citations, uncited_count = _enforce_citation_coverage(content_html, urls_in_html)
    if uncited_count:
        logger.warning(
            f"Track {track.value}: {uncited_count} entry/entries appear to have no "
            f"source URL attached — annotated in HTML for review"
        )
    else:
        logger.info(f"Track {track.value}: citation coverage check passed")

    # Verify dead links have actual Wayback snapshots before substituting
    await _resolve_wayback_fallbacks(link_results)

    # Build map of dead URL → verified Wayback snapshot URL and apply
    url_replacements = _build_url_replacements(link_results)
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
        source_count=source_count if source_count else len(urls_in_html),
        month=month,
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


async def _resolve_wayback_fallbacks(link_results: list[LinkCheckResult]) -> None:
    """
    Replace speculative /web/2/<url> fallbacks with CDX-verified snapshot URLs.

    Mutates link_results in-place: sets wayback_fallback to the verified
    snapshot URL, or clears it to None if no snapshot exists in the archive.
    """
    dead = [r for r in link_results if not r.reachable]
    if not dead:
        return

    dead_urls = [r.url for r in dead]
    verified = await _wayback_batch_verify(dead_urls)

    for result in dead:
        result.wayback_fallback = verified.get(result.url)


def _build_url_replacements(link_results: list[LinkCheckResult]) -> dict[str, str]:
    """Build a dict of dead URL → verified Wayback snapshot URL."""
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


def _build_feedback_links(track: BriefingTrack, month: str) -> tuple[str, str, str]:
    """
    Build mailto: links for the footer feedback row.

    Returns (useful_href, not_useful_href, correction_href).
    The reader's email client opens a pre-addressed message with a subject line
    the inbox reader can parse for feedback aggregation.
    """
    import urllib.parse
    base = f"mailto:{_FEEDBACK_MAILTO}"
    tag = f"Track {track.value} {month}"

    def _mailto(subject: str) -> str:
        return f"{base}?subject={urllib.parse.quote(subject)}"

    return (
        _mailto(f"[FEEDBACK] Useful — {tag}"),
        _mailto(f"[FEEDBACK] Not useful — {tag}"),
        _mailto(f"[FEEDBACK] Error — {tag}"),
    )


def _wrap_in_email_template(
    content_html: str,
    track: BriefingTrack,
    month_human: str,
    track_name: str,
    source_count: int = 0,
    month: str = "",
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

    # Singular/plural and zero-source fallback
    if source_count > 1:
        source_note = f"{source_count} sources cited"
    elif source_count == 1:
        source_note = "1 source cited"
    else:
        source_note = "generated from web sources"

    # Feedback mailto links
    feedback_useful, feedback_not_useful, feedback_correction = _build_feedback_links(
        track, month or month_human
    )

    # Graceful fallback when synthesis produced nothing
    if content_html.strip():
        body_content = _normalise_content_html(content_html)
    else:
        body_content = (
            '<p style="margin:0;color:#888888;font-style:italic;'
            'font-family:Calibri,Arial,sans-serif;font-size:13px;">'
            'No briefing content was generated for this track. '
            'Please check the run logs and retry.'
            '</p>'
        )

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

<!-- max-width for browsers; width attribute for Outlook -->
<table width="680" cellpadding="0" cellspacing="0" border="0"
       style="width:100%;max-width:680px;background-color:#FFFFFF;">

  <!-- Header band -->
  <tr>
    <td style="background-color:{BW_NAVY};padding:28px 36px;">
      <!-- Fixed right-column width prevents long track names from crowding the logo -->
      <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td style="vertical-align:top;">
          <p style="margin:0;font-size:24px;font-weight:bold;color:#FFFFFF;
                    font-family:Calibri,Arial,sans-serif;letter-spacing:0.5px;">BetterWiser</p>
          <p style="margin:5px 0 0 0;font-size:11px;color:{BW_TEAL};letter-spacing:2px;
                    text-transform:uppercase;font-family:Calibri,Arial,sans-serif;">Legal Intelligence</p>
        </td>
        <td width="230" align="right" style="vertical-align:top;padding-left:16px;">
          <p style="margin:0;font-size:12px;color:#A0B0C8;font-family:Calibri,Arial,sans-serif;
                    white-space:nowrap;overflow:hidden;">
            Track {track.value}
          </p>
          <p style="margin:4px 0 0 0;font-size:12px;color:#A0B0C8;font-family:Calibri,Arial,sans-serif;
                    word-break:break-word;">
            {track_name}
          </p>
          <p style="margin:4px 0 0 0;font-size:11px;color:#7A8AA0;font-family:Calibri,Arial,sans-serif;">
            {month_human}
          </p>
        </td>
      </tr></table>
    </td>
  </tr>

  <!-- Accent bar -->
  <tr>
    <td style="background-color:{track_accent};height:3px;font-size:1px;line-height:1px;">&nbsp;</td>
  </tr>

  <!-- Summary bar — month + source count only (track info already in header) -->
  <tr>
    <td style="background-color:#FFF9F0;padding:14px 36px;border-bottom:1px solid #E8E0D0;">
      <p style="margin:0;font-size:12px;color:#7A6040;font-family:Calibri,Arial,sans-serif;line-height:1.5;">
        {month_human} Edition &nbsp;·&nbsp; {source_note}
      </p>
    </td>
  </tr>

  <!-- Body — word-break prevents long URLs overflowing the 680px container -->
  <tr>
    <td style="padding:28px 36px;word-break:break-word;overflow-wrap:break-word;">
      {body_content}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background-color:{BW_NAVY};padding:16px 36px;border-top:2px solid {BW_TEAL};">
      <p style="margin:0 0 8px 0;font-size:11px;color:#7A8AA0;font-family:Calibri,Arial,sans-serif;line-height:1.5;">
        BetterWiser Legal Intelligence &nbsp;·&nbsp; {month_human} &nbsp;·&nbsp;
        Generated automatically — verify before external use.
        &nbsp;·&nbsp; BetterWiser Pte. Ltd., Singapore
      </p>
      <p style="margin:0;font-size:11px;color:#7A8AA0;font-family:Calibri,Arial,sans-serif;line-height:1.8;">
        Feedback:
        &nbsp;
        <a href="{feedback_useful}" style="color:{BW_TEAL};text-decoration:none;">Useful</a>
        &nbsp;·&nbsp;
        <a href="{feedback_not_useful}" style="color:{BW_TEAL};text-decoration:none;">Not useful</a>
        &nbsp;·&nbsp;
        <a href="{feedback_correction}" style="color:{BW_TEAL};text-decoration:none;">Report an error</a>
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""


def _format_from_draft(
    draft: SynthesisDraft,
    min_output_confidence: float = 0.5,
    exclude_confidence_below: float = 0.3,
) -> str:
    """
    Deterministically render a SynthesisDraft to HTML.

    Items are split into three tiers:
    - confidence >= min_output_confidence: rendered inline with the main briefing
    - exclude_confidence_below <= confidence < min_output_confidence: collected and
      rendered in a separate "Items Pending Verification" section at the bottom
    - confidence < exclude_confidence_below OR (not verified AND confidence == 0.0):
      excluded entirely

    All values are HTML-escaped at the point of insertion.
    """
    parts: list[str] = []
    pending_parts: list[str] = []  # items segregated for review

    # Optional hot vendor callout (Track A)
    if draft.hot_vendor:
        hv = escape(draft.hot_vendor)
        parts.append(
            f'<p><strong>Vendor to Watch:</strong> {hv}</p>'
        )

    for section in draft.sections:
        # Section heading
        heading = escape(section.heading)
        if section.eyebrow:
            eyebrow = escape(section.eyebrow)
            parts.append(f'<h3>{eyebrow}</h3>')
        parts.append(f'<h2>{heading}</h2>')

        # Section-level BW relevance (Track C)
        if section.section_relevance:
            rel = escape(section.section_relevance)
            parts.append(
                f'<p><strong>Relevance to BetterWiser:</strong> {rel}</p>'
            )

        # Items as an unordered list — only include high-confidence items here
        section_items_html: list[str] = []
        for item in section.items:
            # Completely failed fact-checking or below exclusion threshold → drop
            if not item.verified and item.confidence == 0.0:
                continue
            if item.confidence < exclude_confidence_below:
                continue

            item_html = _render_item(item)

            if item.confidence < min_output_confidence:
                # Segregate to pending section — include section context
                pending_parts.append(
                    f'<li style="border-left-color:#856404;">'
                    f'<span style="font-size:11px;color:#856404;font-weight:bold;">'
                    f'[From: {escape(section.heading)}]</span><br>'
                    + item_html +
                    '<span style="color:#856404;font-size:11px;margin-left:6px;">'
                    '&#9888; Pending verification (confidence: '
                    f'{item.confidence:.0%})</span>'
                    '</li>'
                )
            else:
                section_items_html.append(f'<li>{item_html}</li>')

        if section_items_html:
            parts.append('<ul>')
            parts.extend(section_items_html)
            parts.append('</ul>')

    # Append segregated pending items in a clearly demarcated section
    if pending_parts:
        parts.append(
            '<hr>'
            '<h2>Items Pending Verification</h2>'
            '<p><em>The following items could not be fully verified against source documents. '
            'Please review before citing externally.</em></p>'
        )
        parts.append('<ul>')
        parts.extend(pending_parts)
        parts.append('</ul>')

    return '\n'.join(parts)


def _render_item(item: DraftBriefingItem) -> str:
    """Render a single DraftBriefingItem to an HTML fragment (without <li> wrapper)."""
    parts: list[str] = []

    # Heading + optional date
    item_heading = escape(item.heading)
    if item.date_str:
        date = escape(item.date_str)
        parts.append(f'<strong>{item_heading}</strong> <span>({date})</span>')
    else:
        parts.append(f'<strong>{item_heading}</strong>')

    # Summary
    if item.summary:
        summary = escape(item.summary)
        parts.append(f'<br>{summary}')

    # Source link
    if item.source_url:
        url = item.source_url  # URLs not escaped — they go in href attr
        name = escape(item.source_name or _domain_from_url(item.source_url))
        parts.append(f' <a href="{url}">[{name}]</a>')

    # Track C: opinion takeaway
    if item.opinion_takeaway:
        ot = escape(item.opinion_takeaway)
        parts.append(f'<br><em><strong>Opinion Takeaway:</strong> {ot}</em>')

    # Track C: BW relevance
    if item.betterwiser_relevance:
        bwr = escape(item.betterwiser_relevance)
        parts.append(f'<br><strong>Relevance to BetterWiser:</strong> {bwr}')

    return '\n'.join(parts)


def _domain_from_url(url: str) -> str:
    """Extract a readable domain name from a URL for use as link text."""
    import re as _re
    m = _re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url[:40]


def _normalise_content_html(html: str) -> str:
    """
    Apply inline styles to common HTML elements for Outlook compatibility.

    Styled to match the Structured Executive Brief (Option A) format:
    - Theme headings (h2) with orange bottom border and top spacing for section separation
    - Briefing items (li) with orange left-border accent, no bullets
    - Citation links (a) in orange at inherited font size (not forced-small)
    - Superscript citation numbers in orange
    - Opinion/Relevance labels (strong) in navy
    - word-break on block elements prevents long URLs overflowing the container
    - Handles ol, hr, blockquote, img for robustness regardless of synthesis output
    """
    # Skip elements that already carry a style attribute throughout
    html = re.sub(
        r"<h1(?![^>]*\bstyle=)([^>]*)>",
        r'<h1\1 style="font-size:20px;font-weight:bold;color:#1B2A4A;'
        r'font-family:Calibri,Arial,sans-serif;margin:32px 0 12px 0;'
        r'word-break:break-word;">',
        html, flags=re.IGNORECASE
    )
    # Theme heading — orange underline; margin-top:28px separates consecutive sections
    html = re.sub(
        r"<h2(?![^>]*\bstyle=)([^>]*)>",
        r'<h2\1 style="font-size:17px;font-weight:bold;color:#1B2A4A;'
        r'font-family:Calibri,Arial,sans-serif;margin:28px 0 14px 0;'
        r'padding-bottom:8px;border-bottom:2px solid #C87800;'
        r'word-break:break-word;">',
        html, flags=re.IGNORECASE
    )
    # Sub-label eyebrow (e.g. "Theme 01") — uppercase orange
    html = re.sub(
        r"<h3(?![^>]*\bstyle=)([^>]*)>",
        r'<h3\1 style="font-size:11px;font-weight:bold;color:#C87800;'
        r'font-family:Calibri,Arial,sans-serif;letter-spacing:2px;'
        r'text-transform:uppercase;margin:24px 0 4px 0;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<h4(?![^>]*\bstyle=)([^>]*)>",
        r'<h4\1 style="font-size:13px;font-weight:bold;color:#1B2A4A;'
        r'font-family:Calibri,Arial,sans-serif;margin:16px 0 6px 0;">',
        html, flags=re.IGNORECASE
    )
    html = re.sub(
        r"<p(?![^>]*\bstyle=)([^>]*)>",
        r'<p\1 style="margin:0 0 8px 0;line-height:1.6;color:#4A4A4A;'
        r'font-size:13px;font-family:Calibri,Arial,sans-serif;'
        r'word-break:break-word;overflow-wrap:break-word;">',
        html, flags=re.IGNORECASE
    )
    # Unordered list — no bullets; items carry their own orange left-border accent.
    # margin-bottom:28px provides clear visual gap before the next theme heading.
    html = re.sub(
        r"<ul(?![^>]*\bstyle=)([^>]*)>",
        r'<ul\1 style="margin:0 0 28px 0;padding:0;list-style:none;">',
        html, flags=re.IGNORECASE
    )
    # Ordered list — keep numbers, indent with standard padding
    html = re.sub(
        r"<ol(?![^>]*\bstyle=)([^>]*)>",
        r'<ol\1 style="margin:0 0 16px 0;padding-left:20px;">',
        html, flags=re.IGNORECASE
    )
    # Each briefing item: orange left-border accent (Option A style).
    # word-break prevents long article titles or URLs breaking the layout.
    html = re.sub(
        r"<li(?![^>]*\bstyle=)([^>]*)>",
        r'<li\1 style="margin-bottom:20px;padding:0 0 0 16px;'
        r'border-left:3px solid #C87800;list-style:none;'
        r'font-size:13px;line-height:1.6;color:#4A4A4A;'
        r'font-family:Calibri,Arial,sans-serif;'
        r'word-break:break-word;overflow-wrap:break-word;">',
        html, flags=re.IGNORECASE
    )
    # Citation and source links — orange, inherits font-size from parent (13px in body
    # text, 10px when inside <sup>). word-break:break-all handles raw URL link text.
    html = re.sub(
        r"<a ([^>]*href=[^>]+)>",
        lambda m: (
            f'<a {m.group(1)} style="color:#C87800;text-decoration:none;'
            f'word-break:break-all;">'
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
    # Superscript citation numbers — orange, bold
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
    # Horizontal rule — thin orange divider instead of the default grey bar
    html = re.sub(
        r"<hr(?![^>]*\bstyle=)([^>]*)>",
        r'<hr\1 style="border:none;border-top:1px solid #E8C880;margin:24px 0;">',
        html, flags=re.IGNORECASE
    )
    # Blockquote — indented italic with left accent (opinion or pull-quote)
    html = re.sub(
        r"<blockquote(?![^>]*\bstyle=)([^>]*)>",
        r'<blockquote\1 style="margin:12px 0 12px 16px;padding:8px 16px;'
        r'border-left:3px solid #C87800;font-style:italic;color:#555555;'
        r'font-family:Calibri,Arial,sans-serif;font-size:13px;'
        r'word-break:break-word;">',
        html, flags=re.IGNORECASE
    )
    # Images — constrain to container width and prevent broken layout
    html = re.sub(
        r"<img(?![^>]*\bstyle=)([^>]*)>",
        r'<img\1 style="max-width:100%;height:auto;display:block;border:0;">',
        html, flags=re.IGNORECASE
    )
    # Inline code
    html = re.sub(
        r"<code(?![^>]*\bstyle=)([^>]*)>",
        r'<code\1 style="font-family:Consolas,monospace;font-size:12px;'
        r'background-color:#F5F5F5;padding:1px 4px;color:#333333;">',
        html, flags=re.IGNORECASE
    )
    # Strip div tags to avoid invalid nesting with <p> or <li> elements
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
