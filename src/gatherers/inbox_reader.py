"""
Microsoft 365 inbox reader — Phase 2, Sub-pipeline A.

Reads all emails from the agent's dedicated M365 mailbox for the target month.
Extracts email bodies, harvests links, and parses attachments.

GRACEFUL DEGRADATION: If Azure AD credentials are missing, logs a warning
and returns an empty list. The pipeline continues with web-only gathering.

Prerequisites (see SETUP.md):
  - Azure AD App Registration with Mail.Read + Mail.Send permissions
  - Admin consent granted
  - AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_USER_EMAIL set in .env
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from src.gatherers.attachment_parser import parse_attachment
from src.schemas import AttachmentContent, EmailSource
from src.utils.email_parser import extract_links_from_html, extract_text_from_html

logger = logging.getLogger(__name__)


async def read_inbox(month: str) -> list[EmailSource]:
    """
    Read all emails from the agent's M365 inbox for the target month.

    Args:
        month: Target month in "YYYY-MM" format.

    Returns:
        List of EmailSource objects (empty if Azure creds missing or on error).
    """
    required_vars = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_USER_EMAIL"]
    missing = [v for v in required_vars if not os.getenv(v)]

    if missing:
        logger.warning(
            f"Azure AD credentials not configured ({', '.join(missing)} missing). "
            f"Inbox reading is disabled. "
            f"See SETUP.md for Azure AD setup instructions. "
            f"Continuing with web-only intelligence gathering."
        )
        return []

    try:
        return await _read_inbox_with_graph(month)
    except Exception as e:
        logger.warning(
            f"Inbox reading failed: {type(e).__name__}: {e}. "
            f"Continuing with web-only gathering."
        )
        return []


async def _read_inbox_with_graph(month: str) -> list[EmailSource]:
    """Internal: perform actual MS Graph API calls."""
    try:
        from azure.identity import ClientSecretCredential
        from msgraph import GraphServiceClient
    except ImportError as e:
        logger.warning(f"msgraph-sdk or azure-identity not installed: {e}. Skipping inbox.")
        return []

    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    user_email = os.getenv("AZURE_USER_EMAIL")

    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    scopes = ["https://graph.microsoft.com/.default"]
    graph_client = GraphServiceClient(credentials=credential, scopes=scopes)

    # Build date filter for target month
    year, mon = int(month[:4]), int(month[5:7])
    month_start = f"{year:04d}-{mon:02d}-01T00:00:00Z"
    # End of month
    import calendar
    last_day = calendar.monthrange(year, mon)[1]
    month_end = f"{year:04d}-{mon:02d}-{last_day:02d}T23:59:59Z"

    logger.info(f"Reading inbox for {user_email} from {month_start} to {month_end}")

    email_sources: list[EmailSource] = []

    try:
        from msgraph.generated.users.item.messages.messages_request_builder import (
            MessagesRequestBuilder,
        )
        from kiota_abstractions.base_request_configuration import RequestConfiguration
    except ImportError as e:
        logger.warning(f"msgraph request builder not available: {e}")
        return []

    # Build the first-page request. Subsequent pages use @odata.nextLink from
    # the response — Graph does not support reliable $skip + $filter pagination.
    query_params = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        filter=f"receivedDateTime ge {month_start} and receivedDateTime le {month_end}",
        top=50,
        select=["id", "subject", "from", "receivedDateTime",
                "body", "hasAttachments", "bodyPreview"],
        orderby=["receivedDateTime desc"],
    )
    request_config = RequestConfiguration(query_parameters=query_params)

    messages_page = None
    page_num = 0
    while True:
        try:
            if messages_page is None:
                # First page
                messages_page = await graph_client.users.by_user_id(user_email)\
                    .messages.get(request_configuration=request_config)
            else:
                # Subsequent pages via nextLink cursor
                next_link = getattr(messages_page, "odata_next_link", None)
                if not next_link:
                    break
                messages_page = await graph_client.users.by_user_id(user_email)\
                    .messages.with_url(next_link).get()
        except Exception as e:
            logger.warning(f"MS Graph messages query failed (page {page_num}): {e}")
            break

        if not messages_page or not messages_page.value:
            break

        page_num += 1
        logger.debug(f"Fetched {len(messages_page.value)} emails (page {page_num})")

        for msg in messages_page.value:
            email_source = _map_message_to_email_source(msg)
            if msg.has_attachments:
                email_source.attachments = await _fetch_attachments(
                    graph_client, user_email, msg.id
                )
            email_sources.append(email_source)

        if not getattr(messages_page, "odata_next_link", None):
            break  # no more pages

    logger.info(f"Read {len(email_sources)} emails from inbox for {month}")
    return email_sources


def _map_message_to_email_source(msg) -> EmailSource:
    """Convert a Graph API Message object to our EmailSource schema."""
    body_html = ""
    body_text = ""

    if msg.body:
        if msg.body.content_type and "html" in str(msg.body.content_type).lower():
            body_html = msg.body.content or ""
            body_text = extract_text_from_html(body_html)
        else:
            body_text = msg.body.content or ""

    extracted_links = extract_links_from_html(body_html) if body_html else []

    sender = ""
    if msg.sender and msg.sender.email_address:
        sender = msg.sender.email_address.address or ""

    received_at = msg.received_date_time or datetime.now(tz=timezone.utc)

    return EmailSource(
        message_id=msg.id or "",
        subject=msg.subject or "(No subject)",
        sender=sender,
        received_at=received_at,
        body_text=body_text,
        body_html=body_html if body_html else None,
        has_attachments=bool(msg.has_attachments),
        extracted_links=extracted_links,
    )


async def _fetch_attachments(graph_client, user_email: str, message_id: str) -> list[AttachmentContent]:
    """Fetch and parse attachments for a specific message."""
    try:
        attachments_page = await graph_client.users.by_user_id(user_email)\
            .messages.by_message_id(message_id)\
            .attachments.get()

        if not attachments_page or not attachments_page.value:
            return []

        parsed: list[AttachmentContent] = []
        for att in attachments_page.value:
            filename = att.name or "unknown"
            content_type = att.content_type or "application/octet-stream"

            # Graph API returns base64-encoded content bytes
            raw_bytes = b""
            if hasattr(att, "content_bytes") and att.content_bytes:
                try:
                    raw_bytes = base64.b64decode(att.content_bytes)
                except Exception:
                    raw_bytes = att.content_bytes if isinstance(att.content_bytes, bytes) else b""

            if raw_bytes:
                attachment_content = parse_attachment(filename, raw_bytes, content_type)
                parsed.append(attachment_content)

        return parsed

    except Exception as e:
        logger.warning(f"Failed to fetch attachments for message {message_id}: {e}")
        return []
