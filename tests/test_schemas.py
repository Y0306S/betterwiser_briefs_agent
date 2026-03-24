"""
Tests for Pydantic v2 schemas — ensures all models instantiate correctly.
"""

import pytest
from datetime import datetime, timezone

from src.schemas import (
    AttachmentContent,
    BriefingItem,
    BriefingTrack,
    DeliveryReceipt,
    DiscoveredArticle,
    EmailSource,
    EventCluster,
    FlaggedClaim,
    GatheredData,
    GatheringStats,
    GroundingReport,
    LinkCheckResult,
    RunContext,
    ScrapedSource,
    SourceTier,
    SynthesisResult,
    ThemeGroup,
    ValidatedBriefing,
)


class TestRunContext:
    def test_basic_instantiation(self):
        ctx = RunContext(
            run_id="2026-03_run_test",
            month="2026-03",
            tracks=[BriefingTrack.A, BriefingTrack.B, BriefingTrack.C],
        )
        assert ctx.run_id == "2026-03_run_test"
        assert ctx.month == "2026-03"
        assert ctx.dry_run is True
        assert ctx.send is False

    def test_invalid_month_format(self):
        with pytest.raises(Exception):
            RunContext(
                run_id="test",
                month="March 2026",
                tracks=[BriefingTrack.A],
            )

    def test_model_dump(self):
        ctx = RunContext(run_id="test", month="2026-03", tracks=[BriefingTrack.C])
        data = ctx.model_dump()
        assert data["month"] == "2026-03"
        assert "created_at" in data


class TestScrapedSource:
    def test_valid_source(self):
        source = ScrapedSource(
            url="https://harvey.ai/blog/post",
            title="Harvey launches new feature",
            content="Harvey AI today announced...",
            tier=SourceTier.TIER_2,
            scraper_used="jina",
            word_count=100,
        )
        assert source.url == "https://harvey.ai/blog/post"
        assert source.tier == SourceTier.TIER_2
        assert source.error is None

    def test_invalid_url(self):
        with pytest.raises(Exception):
            ScrapedSource(
                url="not-a-url",
                title="Test",
                content="Test content",
                tier=SourceTier.TIER_3,
                scraper_used="jina",
            )

    def test_source_with_error(self):
        source = ScrapedSource(
            url="https://example.com",
            title="",
            content="",
            tier=SourceTier.TIER_3,
            scraper_used="none",
            error="All scrapers failed",
        )
        assert source.error == "All scrapers failed"


class TestEmailSource:
    def test_basic_email(self):
        email = EmailSource(
            message_id="AAMkABCD1234",
            subject="Harvey Product Update",
            sender="updates@harvey.ai",
            received_at=datetime.now(tz=timezone.utc),
            body_text="Dear subscriber, Harvey has launched...",
        )
        assert email.has_attachments is False
        assert email.attachments == []


class TestDiscoveredArticle:
    def test_valid_article(self):
        article = DiscoveredArticle(
            url="https://artificiallawyer.com/article",
            title="Legal AI Trends",
            snippet="A summary of the article...",
            source_name="Artificial Lawyer",
            track=BriefingTrack.C,
            discovery_wave=2,
        )
        assert article.discovery_wave == 2
        assert article.discovered_via == "claude_web_search"


class TestGatheredData:
    def test_empty_gathered_data(self):
        ctx = RunContext(run_id="test", month="2026-03", tracks=[BriefingTrack.A])
        gathered = GatheredData(run_context=ctx)
        assert gathered.scraped_sources == []
        assert gathered.email_sources == []
        assert gathered.discovered_articles == []
        assert gathered.stats.emails_read == 0


class TestBriefingItem:
    def test_track_a_item(self):
        item = BriefingItem(
            item_id="a_001",
            track=BriefingTrack.A,
            date_str="On 15 March 2026",
            heading="Harvey launches contract review feature",
            summary="Harvey AI announced a new contract review module...",
            url="https://harvey.ai/blog/contract-review",
            tier=SourceTier.TIER_2,
        )
        assert item.opinion_takeaway is None  # A tracks don't need this
        assert item.confidence_score == 1.0

    def test_track_c_item_with_takeaway(self):
        item = BriefingItem(
            item_id="c_001",
            track=BriefingTrack.C,
            heading="McKinsey: AI Workforce Transformation",
            summary="New research shows 60% of legal tasks automatable...",
            url="https://mckinsey.com/article",
            tier=SourceTier.TIER_2,
            opinion_takeaway="This signals a shift in how firms approach AI...",
            betterwiser_relevance="Directly relevant to BetterWiser's change management advisory...",
        )
        assert item.opinion_takeaway is not None


class TestGroundingReport:
    def test_passing_report(self):
        report = GroundingReport(
            total_claims=20,
            grounded_claims=19,
            pass_rate=0.95,
            below_threshold=False,
        )
        assert not report.below_threshold

    def test_failing_report(self):
        report = GroundingReport(
            total_claims=20,
            grounded_claims=15,
            pass_rate=0.75,
            below_threshold=True,
        )
        assert report.below_threshold


class TestDeliveryReceipt:
    def test_dry_run_receipt(self):
        receipt = DeliveryReceipt(
            run_id="test_run",
            track=BriefingTrack.C,
            delivered=False,
            dry_run=True,
            output_path="/path/to/track_C.html",
            recipients=["lynette@betterwiser.com"],
        )
        assert not receipt.delivered
        assert receipt.dry_run

    def test_sent_receipt(self):
        receipt = DeliveryReceipt(
            run_id="test_run",
            track=BriefingTrack.A,
            delivered=True,
            dry_run=False,
            recipients=["lynette@betterwiser.com"],
            delivered_at=datetime.now(tz=timezone.utc),
        )
        assert receipt.delivered
        assert not receipt.dry_run


class TestEnums:
    def test_briefing_track_values(self):
        assert BriefingTrack.A.value == "A"
        assert BriefingTrack.B.value == "B"
        assert BriefingTrack.C.value == "C"

    def test_source_tier_values(self):
        assert SourceTier.TIER_1.value == "tier_1"
        assert SourceTier.TIER_2.value == "tier_2"
        assert SourceTier.TIER_3.value == "tier_3"

    def test_briefing_track_from_string(self):
        t = BriefingTrack("C")
        assert t == BriefingTrack.C
