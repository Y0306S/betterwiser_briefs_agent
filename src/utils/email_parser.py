"""
Email content utilities: HTML-to-text conversion, link extraction,
and newsletter sender matching against subscription config.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import yaml

logger = logging.getLogger(__name__)

try:
    import html2text
    _html2text_available = True
except ImportError:
    logger.warning("html2text not installed — falling back to regex-based text extraction")
    _html2text_available = False

try:
    from bs4 import BeautifulSoup
    _bs4_available = True
except ImportError:
    logger.warning("beautifulsoup4 not installed — link extraction will be limited")
    _bs4_available = False


def extract_text_from_html(html: str) -> str:
    """
    Convert HTML email body to clean plain text / markdown.

    Args:
        html: Raw HTML string from email body.

    Returns:
        Cleaned text suitable for Claude context.
    """
    if not html:
        return ""

    if _html2text_available:
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.body_width = 0  # no line wrapping
        converter.unicode_snob = True
        try:
            return converter.handle(html).strip()
        except Exception as e:
            logger.warning(f"html2text failed: {e}, falling back to regex")

    # Fallback: strip HTML tags with regex
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_links_from_html(html: str, base_url: str = "") -> list[str]:
    """
    Extract all unique HTTP/HTTPS links from an HTML document.

    Args:
        html: Raw HTML string.
        base_url: Optional base URL for resolving relative links.

    Returns:
        Deduplicated list of absolute URLs.
    """
    if not html:
        return []

    links: list[str] = []

    if _bs4_available:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.find_all("a", href=True):
                href = tag["href"].strip()
                if href.startswith(("http://", "https://")):
                    links.append(href)
                elif base_url and href.startswith("/"):
                    links.append(urljoin(base_url, href))
        except Exception as e:
            logger.warning(f"BeautifulSoup link extraction failed: {e}")
    else:
        # Fallback: regex-based URL extraction
        pattern = re.compile(r'href=["\']?(https?://[^\s"\'<>]+)["\']?', re.IGNORECASE)
        links = pattern.findall(html)

    # Deduplicate while preserving order, filter noise
    seen: set[str] = set()
    result: list[str] = []
    for link in links:
        # Strip tracking parameters that bloat the URL
        clean = _clean_url(link)
        if clean not in seen and _is_useful_link(clean):
            seen.add(clean)
            result.append(clean)

    return result


def is_newsletter(email_sender: str, email_subject: str, subscriptions: list[dict]) -> bool:
    """
    Check whether an email matches any configured newsletter subscription.

    Args:
        email_sender: Sender email/address string.
        email_subject: Email subject line.
        subscriptions: List of subscription dicts from newsletter_subscriptions.yaml.

    Returns:
        True if the email matches a known newsletter subscription.
    """
    sender_lower = email_sender.lower()
    subject_lower = email_subject.lower()

    for sub in subscriptions:
        sender_patterns = sub.get("sender_patterns", [])
        subject_patterns = sub.get("subject_patterns", [])

        sender_match = any(p.lower() in sender_lower for p in sender_patterns)
        subject_match = any(p.lower() in subject_lower for p in subject_patterns) if subject_patterns else True

        if sender_match and subject_match:
            return True

    return False


def get_newsletter_tracks(email_sender: str, email_subject: str, subscriptions: list[dict]) -> list[str]:
    """
    Return the track IDs associated with a newsletter email.

    Returns:
        List of track IDs (e.g. ["A", "C"]) or empty list if no match.
    """
    sender_lower = email_sender.lower()
    subject_lower = email_subject.lower()

    for sub in subscriptions:
        sender_patterns = sub.get("sender_patterns", [])
        subject_patterns = sub.get("subject_patterns", [])

        sender_match = any(p.lower() in sender_lower for p in sender_patterns)
        subject_match = any(p.lower() in subject_lower for p in subject_patterns) if subject_patterns else True

        if sender_match and subject_match:
            return sub.get("tracks", [])

    return []


def load_subscriptions(config_path: str = "config/newsletter_subscriptions.yaml") -> list[dict]:
    """Load newsletter subscription definitions from YAML config."""
    path = Path(config_path)
    if not path.exists():
        logger.warning(f"Newsletter subscriptions config not found: {config_path}")
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("subscriptions", [])


def _clean_url(url: str) -> str:
    """Remove common tracking parameters from URLs."""
    try:
        parsed = urlparse(url)
        # Strip utm_* and other tracking params
        if parsed.query:
            params = [
                p for p in parsed.query.split("&")
                if not p.startswith(("utm_", "fbclid=", "gclid=", "mc_cid=", "mc_eid="))
            ]
            clean_query = "&".join(params)
            return parsed._replace(query=clean_query).geturl()
    except Exception:
        pass
    return url


def _is_useful_link(url: str) -> bool:
    """Filter out links that are unlikely to be useful content sources."""
    if not url:
        return False
    skip_domains = {
        "twitter.com", "x.com", "facebook.com", "instagram.com",
        "youtube.com", "tiktok.com", "pinterest.com",
        "mailto:", "tel:", "javascript:",
    }
    url_lower = url.lower()
    return not any(d in url_lower for d in skip_domains)
