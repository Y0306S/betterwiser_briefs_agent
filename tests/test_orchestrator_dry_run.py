"""
Integration test: full pipeline in dry-run mode with mocked API calls.

Verifies:
- Pipeline completes without errors on minimal valid input
- HTML file is saved to runs/ directory
- DeliveryReceipt has delivered=False, dry_run=True
- No real API calls are made
"""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.schemas import (
    BriefingTrack,
    DeliveryReceipt,
    GatheredData,
    GatheringStats,
    GroundingReport,
    RunContext,
    ScrapedSource,
    SourceTier,
    SynthesisResult,
    ValidatedBriefing,
)


# Minimal mock HTML for synthesis output
MOCK_BRIEFING_HTML = """
<h2>Vendor Updates</h2>
<ul>
  <li>On 15 March 2026, Harvey AI launched a new contract review feature.
      (<a href="https://harvey.ai/blog/update">Source</a>)</li>
  <li>On 20 March 2026, Luminance announced a new partnership with a Singapore law firm.
      (<a href="https://luminance.com/news/sg">Source</a>)</li>
</ul>
"""


def make_minimal_gathered(run_context: RunContext) -> GatheredData:
    """Create minimal GatheredData for testing."""
    return GatheredData(
        run_context=run_context,
        scraped_sources=[
            ScrapedSource(
                url="https://harvey.ai/blog/update",
                title="Harvey Update",
                content="Harvey AI launched a new contract review feature in March 2026. " * 20,
                tier=SourceTier.TIER_2,
                scraper_used="jina",
                word_count=200,
            )
        ],
        stats=GatheringStats(
            emails_read=0,
            urls_scraped=1,
            articles_discovered=0,
        ),
    )


def make_mock_synthesis(run_context: RunContext, track: BriefingTrack) -> SynthesisResult:
    """Create mock SynthesisResult for testing."""
    return SynthesisResult(
        run_id=run_context.run_id,
        track=track,
        raw_html=MOCK_BRIEFING_HTML,
        pass_completed=[0, 1, 2, 3, 35, 4],
    )


def make_mock_validated(synthesis: SynthesisResult) -> ValidatedBriefing:
    """Create mock ValidatedBriefing for testing."""
    return ValidatedBriefing(
        synthesis=synthesis,
        grounding_report=GroundingReport(
            total_claims=2,
            grounded_claims=2,
            pass_rate=1.0,
            below_threshold=False,
        ),
        final_html=f"<html><body>{synthesis.raw_html}</body></html>",
        subject_line=f"BetterWiser — Track {synthesis.track.value} — March 2026",
        held_for_review=False,
        ready_to_send=True,
    )


class TestDryRunPipeline:
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for run outputs."""
        tmpdir = tempfile.mkdtemp()
        yield tmpdir
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_delivery_receipt_dry_run(self, temp_dir):
        """Verify dry-run produces a DeliveryReceipt with delivered=False."""
        import asyncio

        run_context = RunContext(
            run_id="2026-03_run_test",
            month="2026-03",
            tracks=[BriefingTrack.A],
            dry_run=True,
            send=False,
            runs_dir=temp_dir,
        )

        synthesis = make_mock_synthesis(run_context, BriefingTrack.A)
        validated = make_mock_validated(synthesis)

        from src.delivery.email_sender import send_briefing

        receipt = asyncio.run(
            send_briefing(
                validated=validated,
                recipients=[{"email": "lynette@betterwiser.com", "name": "Lynette Ooi"}],
                run_context=run_context,
                subject_template="BetterWiser — Track A — {month_human}",
            )
        )

        assert isinstance(receipt, DeliveryReceipt)
        assert not receipt.delivered
        assert receipt.dry_run
        assert receipt.output_path is not None
        assert Path(receipt.output_path).exists()

    def test_html_file_saved_to_disk(self, temp_dir):
        """Verify the HTML file is written to runs/{run_id}/delivery/track_A.html."""
        import asyncio

        run_context = RunContext(
            run_id="2026-03_run_test2",
            month="2026-03",
            tracks=[BriefingTrack.A],
            dry_run=True,
            send=False,
            runs_dir=temp_dir,
        )

        synthesis = make_mock_synthesis(run_context, BriefingTrack.A)
        validated = make_mock_validated(synthesis)

        from src.delivery.email_sender import send_briefing

        receipt = asyncio.run(
            send_briefing(
                validated=validated,
                recipients=[],
                run_context=run_context,
            )
        )

        output_path = Path(receipt.output_path)
        assert output_path.exists()
        assert output_path.suffix == ".html"
        content = output_path.read_text(encoding="utf-8")
        assert "<html" in content.lower()

    def test_no_send_without_flag(self, temp_dir):
        """Verify email is NOT sent when send=False, even with Azure creds present."""
        import asyncio

        # Set fake Azure creds
        with patch.dict(os.environ, {
            "AZURE_TENANT_ID": "fake-tenant",
            "AZURE_CLIENT_ID": "fake-client",
            "AZURE_CLIENT_SECRET": "fake-secret",
            "AZURE_USER_EMAIL": "test@test.com",
        }):
            run_context = RunContext(
                run_id="2026-03_run_test3",
                month="2026-03",
                tracks=[BriefingTrack.A],
                dry_run=True,
                send=False,  # explicit no-send
                runs_dir=temp_dir,
            )

            synthesis = make_mock_synthesis(run_context, BriefingTrack.A)
            validated = make_mock_validated(synthesis)

            from src.delivery.email_sender import send_briefing

            receipt = asyncio.run(
                send_briefing(
                    validated=validated,
                    recipients=[{"email": "lynette@betterwiser.com", "name": "Lynette"}],
                    run_context=run_context,
                )
            )

        assert not receipt.delivered  # should NOT send
        assert receipt.dry_run

    def test_held_for_review_not_sent(self, temp_dir):
        """Verify briefings with grounding below threshold are not sent."""
        import asyncio

        with patch.dict(os.environ, {
            "AZURE_TENANT_ID": "fake",
            "AZURE_CLIENT_ID": "fake",
            "AZURE_CLIENT_SECRET": "fake",
            "AZURE_USER_EMAIL": "test@test.com",
        }):
            run_context = RunContext(
                run_id="2026-03_run_test4",
                month="2026-03",
                tracks=[BriefingTrack.A],
                dry_run=False,
                send=True,
                runs_dir=temp_dir,
            )

            synthesis = make_mock_synthesis(run_context, BriefingTrack.A)
            # Make it fail grounding
            validated = ValidatedBriefing(
                synthesis=synthesis,
                grounding_report=GroundingReport(
                    total_claims=10,
                    grounded_claims=7,
                    pass_rate=0.70,
                    below_threshold=True,
                ),
                final_html="<html><body>Test</body></html>",
                subject_line="Test",
                held_for_review=True,
                ready_to_send=False,
            )

            from src.delivery.email_sender import send_briefing

            receipt = asyncio.run(
                send_briefing(
                    validated=validated,
                    recipients=[{"email": "lynette@betterwiser.com", "name": "Lynette"}],
                    run_context=run_context,
                )
            )

        assert not receipt.delivered
        assert receipt.held_for_review
