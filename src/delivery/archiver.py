"""
Local archiver — Phase 5 (always runs, before any email send attempt).

Saves:
1. Final HTML briefing → runs/{run_id}/delivery/track_{X}.html
2. JSON dumps of GatheredData, SynthesisResult, GroundingReport for auditability
3. Optional SharePoint upload (skipped gracefully if Azure not configured)

This is the FIRST thing called in Phase 5 so a copy always exists even if
email delivery fails.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from src.schemas import GatheredData, GroundingReport, SynthesisResult, ValidatedBriefing

logger = logging.getLogger(__name__)


def archive_locally(
    validated: ValidatedBriefing,
    run_id: str,
    runs_dir: str = "runs",
) -> str:
    """
    Save the validated briefing HTML and supporting JSON to disk.

    Args:
        validated: The fully validated briefing.
        run_id: Current run identifier.
        runs_dir: Root directory for run outputs.

    Returns:
        Absolute path to the saved HTML file.
    """
    base_path = Path(runs_dir) / run_id
    delivery_dir = base_path / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)

    track = validated.synthesis.track
    html_path = delivery_dir / f"track_{track.value}.html"

    # Save HTML
    html_path.write_text(validated.final_html, encoding="utf-8")
    logger.info(f"Track {track.value}: HTML saved to {html_path}")

    # Save grounding report JSON
    grounding_path = delivery_dir / f"grounding_track_{track.value}.json"
    grounding_path.write_text(
        validated.grounding_report.model_dump_json(indent=2),
        encoding="utf-8",
    )

    # Save link check results
    links_path = delivery_dir / f"link_check_track_{track.value}.json"
    links_data = [r.model_dump() for r in validated.link_results]
    links_path.write_text(json.dumps(links_data, indent=2, default=str), encoding="utf-8")

    logger.info(
        f"Track {track.value}: archived — "
        f"HTML={html_path.name}, "
        f"grounding={grounding_path.name}, "
        f"links={links_path.name}"
    )

    return str(html_path.resolve())


def archive_gathered_data(
    gathered: GatheredData,
    run_id: str,
    runs_dir: str = "runs",
) -> None:
    """Save gathered data to JSON for debugging and resume capability."""
    base_path = Path(runs_dir) / run_id
    raw_dir = base_path / "raw_data"
    raw_dir.mkdir(parents=True, exist_ok=True)

    gathered_path = raw_dir / "gathered_data.json"
    try:
        gathered_path.write_text(
            gathered.model_dump_json(indent=2),
            encoding="utf-8",
        )
        logger.debug(f"Gathered data archived to {gathered_path}")
    except Exception as e:
        logger.warning(f"Could not archive gathered data: {e}")


def archive_synthesis(
    synthesis: SynthesisResult,
    run_id: str,
    runs_dir: str = "runs",
) -> None:
    """Save synthesis result to JSON for debugging and resume capability."""
    base_path = Path(runs_dir) / run_id
    synthesis_dir = base_path / "synthesis"
    synthesis_dir.mkdir(parents=True, exist_ok=True)

    synth_path = synthesis_dir / f"synthesis_track_{synthesis.track.value}.json"
    try:
        synth_path.write_text(
            synthesis.model_dump_json(indent=2),
            encoding="utf-8",
        )
        logger.debug(f"Synthesis archived to {synth_path}")
    except Exception as e:
        logger.warning(f"Could not archive synthesis for Track {synthesis.track.value}: {e}")


async def upload_to_sharepoint(
    html_path: str,
    track_name: str,
    month: str,
) -> bool:
    """
    Upload the briefing HTML to SharePoint for team access.

    Returns True if successful, False on any error (always fails gracefully).
    Azure credentials must be configured (AZURE_TENANT_ID, etc.).
    """
    required = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"]
    if not all(os.getenv(v) for v in required):
        logger.debug("SharePoint upload skipped: Azure credentials not configured")
        return False

    try:
        from azure.identity import ClientSecretCredential
        from msgraph import GraphServiceClient

        credential = ClientSecretCredential(
            tenant_id=os.getenv("AZURE_TENANT_ID"),
            client_id=os.getenv("AZURE_CLIENT_ID"),
            client_secret=os.getenv("AZURE_CLIENT_SECRET"),
        )
        client = GraphServiceClient(
            credentials=credential,
            scopes=["https://graph.microsoft.com/.default"],
        )

        # Read local file
        content = Path(html_path).read_bytes()
        filename = f"BetterWiser_{track_name}_{month}.html"

        # Upload to OneDrive root (adjust path as needed)
        user_email = os.getenv("AZURE_USER_EMAIL", "")
        if not user_email:
            return False

        await client.users.by_user_id(user_email)\
            .drive.root\
            .item_with_path(f"BriefingArchive/{filename}")\
            .content.put(content)

        logger.info(f"SharePoint upload successful: {filename}")
        return True

    except Exception as e:
        logger.warning(f"SharePoint upload failed (non-critical): {e}")
        return False
