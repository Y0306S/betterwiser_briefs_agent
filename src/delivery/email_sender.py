"""
Email sender — Phase 5.

Sends the validated briefing via Microsoft 365 Graph API.

SAFE DEFAULTS:
- dry_run=True by default → ALWAYS archives to disk, NEVER sends email
- send=False by default → requires explicit --send flag to email
- Both dry_run AND missing Azure creds → archive only, warn user

The caller (orchestrator) controls dry_run and send flags via RunContext.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from src.delivery.archiver import archive_locally, upload_to_sharepoint
from src.schemas import BriefingTrack, DeliveryReceipt, RunContext, ValidatedBriefing

logger = logging.getLogger(__name__)


async def send_briefing(
    validated: ValidatedBriefing,
    recipients: list[dict],
    run_context: RunContext,
    subject_template: str = "",
) -> DeliveryReceipt:
    """
    Archive the briefing and optionally send via MS Graph.

    Always archives locally first. Only sends email if:
    - run_context.dry_run is False, AND
    - run_context.send is True, AND
    - Azure AD credentials are configured

    Args:
        validated: The validated briefing ready for delivery.
        recipients: List of {"email": ..., "name": ...} dicts.
        run_context: Current pipeline run context (controls dry_run, send).
        subject_template: Email subject template with {month_human} placeholder.

    Returns:
        DeliveryReceipt (delivered=False if dry_run or error).
    """
    track = validated.synthesis.track
    recipient_emails = [r.get("email", "") for r in recipients if r.get("email")]

    # Step 1: Always archive locally first
    output_path = archive_locally(validated, run_context.run_id, run_context.runs_dir)

    # Step 2: Check if we should actually send email
    if run_context.dry_run or not run_context.send:
        logger.info(
            f"Track {track.value}: DRY RUN — briefing saved to {output_path}. "
            f"To send via email: use --send flag and configure Azure AD credentials."
        )
        return DeliveryReceipt(
            run_id=run_context.run_id,
            track=track,
            delivered=False,
            dry_run=True,
            output_path=output_path,
            recipients=recipient_emails,
            held_for_review=validated.held_for_review,
        )

    # Try SharePoint upload only when actually sending (non-blocking failure)
    try:
        month = run_context.month
        track_name = _track_name(track)
        await upload_to_sharepoint(output_path, track_name, month)
    except Exception:
        pass  # SharePoint is optional — never block delivery

    # Step 3: Check if held for review (before credentials — grounding failure is
    # the real reason we can't send, not missing Azure keys)
    if validated.held_for_review:
        logger.warning(
            f"Track {track.value}: Briefing HELD FOR REVIEW "
            f"(grounding below threshold). NOT sending. "
            f"Review at {output_path}."
        )
        return DeliveryReceipt(
            run_id=run_context.run_id,
            track=track,
            delivered=False,
            dry_run=False,
            output_path=output_path,
            recipients=recipient_emails,
            held_for_review=True,
            error="Held for human review: grounding below threshold",
        )

    # Step 4: Check Azure credentials
    required = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_USER_EMAIL"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        logger.warning(
            f"Track {track.value}: Cannot send email — "
            f"missing Azure AD credentials: {', '.join(missing)}. "
            f"Briefing saved locally at {output_path}."
        )
        return DeliveryReceipt(
            run_id=run_context.run_id,
            track=track,
            delivered=False,
            dry_run=False,
            output_path=output_path,
            recipients=recipient_emails,
            error=f"Missing Azure credentials: {', '.join(missing)}",
        )

    # Step 5: Send via MS Graph
    return await _send_via_graph(
        validated=validated,
        recipients=recipients,
        run_context=run_context,
        output_path=output_path,
        recipient_emails=recipient_emails,
    )


async def _send_via_graph(
    validated: ValidatedBriefing,
    recipients: list[dict],
    run_context: RunContext,
    output_path: str,
    recipient_emails: list[str],
) -> DeliveryReceipt:
    """Internal: perform the actual MS Graph sendMail API call."""
    track = validated.synthesis.track

    try:
        from azure.identity import ClientSecretCredential
        from msgraph import GraphServiceClient
        from msgraph.generated.models.body_type import BodyType
        from msgraph.generated.models.email_address import EmailAddress
        from msgraph.generated.models.item_body import ItemBody
        from msgraph.generated.models.message import Message
        from msgraph.generated.models.recipient import Recipient
        from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
            SendMailPostRequestBody,
        )

    except ImportError as e:
        logger.error(f"msgraph-sdk not installed: {e}")
        return DeliveryReceipt(
            run_id=run_context.run_id,
            track=track,
            delivered=False,
            dry_run=False,
            output_path=output_path,
            recipients=recipient_emails,
            error=f"msgraph-sdk not installed: {e}",
        )

    try:
        credential = ClientSecretCredential(
            tenant_id=os.getenv("AZURE_TENANT_ID"),
            client_id=os.getenv("AZURE_CLIENT_ID"),
            client_secret=os.getenv("AZURE_CLIENT_SECRET"),
        )
        graph_client = GraphServiceClient(
            credentials=credential,
            scopes=["https://graph.microsoft.com/.default"],
        )
        sender_email = os.getenv("AZURE_USER_EMAIL")

        # Build recipient list
        to_recipients = [
            Recipient(
                email_address=EmailAddress(
                    address=r.get("email", ""),
                    name=r.get("name", ""),
                )
            )
            for r in recipients if r.get("email")
        ]

        message = Message(
            subject=validated.subject_line,
            body=ItemBody(
                content=validated.final_html,
                content_type=BodyType.Html,
            ),
            to_recipients=to_recipients,
        )

        request_body = SendMailPostRequestBody(
            message=message,
            save_to_sent_items=True,
        )

        await graph_client.users.by_user_id(sender_email)\
            .send_mail.post(request_body)

        logger.info(
            f"Track {track.value}: Email sent successfully to "
            f"{', '.join(recipient_emails)}"
        )

        return DeliveryReceipt(
            run_id=run_context.run_id,
            track=track,
            delivered=True,
            dry_run=False,
            output_path=output_path,
            recipients=recipient_emails,
            delivered_at=datetime.now(tz=timezone.utc),
        )

    except Exception as e:
        logger.error(f"Track {track.value}: Email send failed: {type(e).__name__}: {e}")
        return DeliveryReceipt(
            run_id=run_context.run_id,
            track=track,
            delivered=False,
            dry_run=False,
            output_path=output_path,
            recipients=recipient_emails,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )


def _track_name(track: BriefingTrack) -> str:
    names = {
        BriefingTrack.A: "Vendor_Customer_Intelligence",
        BriefingTrack.B: "Global_AI_Policy_Watch",
        BriefingTrack.C: "Thought_Leadership_Digest",
    }
    return names.get(track, f"Track_{track.value}")
