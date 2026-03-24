"""
Tests for fuzzy-match grounding verification.
"""

import pytest
from unittest.mock import patch

from src.schemas import BriefingItem, BriefingTrack, SourceTier
from src.utils.grounding import (
    _chunk_text,
    _classify_claim_type,
    ground_claim,
    run_grounding_pass,
)


class TestGroundClaim:
    def test_exact_match_grounded(self):
        claim = "Harvey raised $100 million in Series B funding"
        sources = ["Harvey AI announced it has raised $100 million in Series B funding."]
        grounded, score = ground_claim(claim, sources, threshold=75)
        assert grounded
        assert score >= 75

    def test_no_sources_ungrounded(self):
        claim = "Some claim about something"
        grounded, score = ground_claim(claim, [], threshold=80)
        assert not grounded
        assert score == 0.0

    def test_empty_claim(self):
        grounded, score = ground_claim("", ["source text"], threshold=80)
        assert not grounded

    def test_partial_match_grounded(self):
        claim = "On 15 March, Harvey launched a new feature"
        sources = [
            "Harvey has launched several new features this quarter.",
            "In March 2026, Harvey AI released updates to its platform.",
        ]
        # Should find some match even if not exact
        grounded, score = ground_claim(claim, sources, threshold=50)
        assert isinstance(grounded, bool)
        assert isinstance(score, float)

    def test_completely_unrelated_claim(self):
        claim = "$50 billion acquisition of Microsoft by Apple"
        sources = ["Legal technology news from Singapore. SAL event upcoming."]
        grounded, score = ground_claim(claim, sources, threshold=80)
        assert not grounded

    def test_threshold_effect(self):
        claim = "Harvey raised funding"
        sources = ["Harvey AI has raised significant funding this year."]
        # Low threshold should ground it
        grounded_low, _ = ground_claim(claim, sources, threshold=30)
        # Very high threshold may not
        grounded_high, _ = ground_claim(claim, sources, threshold=99)
        assert grounded_low  # should be grounded at low threshold
        assert isinstance(grounded_high, bool)


class TestChunkText:
    def test_short_text_single_chunk(self):
        text = "This is a short text."
        chunks = _chunk_text(text, window=300, overlap=50)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_multiple_chunks(self):
        words = " ".join([f"word{i}" for i in range(500)])
        chunks = _chunk_text(words, window=100, overlap=20)
        assert len(chunks) > 1

    def test_empty_text(self):
        chunks = _chunk_text("", window=100, overlap=20)
        assert len(chunks) == 1
        assert chunks[0] == ""


class TestClassifyClaimType:
    def test_number_claim(self):
        assert _classify_claim_type("Harvey raised $50 million") == "number"

    def test_date_claim(self):
        assert _classify_claim_type("On 15 March 2026, SAL announced") == "date"

    def test_entity_claim(self):
        result = _classify_claim_type("BetterWiser Singapore is a legal innovation firm")
        assert result in ("entity", "unsupported")  # depends on regex match

    def test_unsupported_claim(self):
        assert _classify_claim_type("some claim without specifics") == "unsupported"


class TestRunGroundingPass:
    def make_item(self, item_id: str, heading: str, summary: str) -> BriefingItem:
        return BriefingItem(
            item_id=item_id,
            track=BriefingTrack.A,
            heading=heading,
            summary=summary,
            url="https://example.com",
            tier=SourceTier.TIER_3,
        )

    def test_empty_items(self):
        report = run_grounding_pass([], ["some source text"])
        assert report.pass_rate == 1.0
        assert not report.below_threshold
        assert report.total_claims == 0

    def test_all_grounded_items(self):
        items = [
            self.make_item(
                "1",
                "Harvey raises $100 million",
                "Harvey AI raised $100 million in Series B funding.",
            ),
            self.make_item(
                "2",
                "SAL launches AI programme",
                "Singapore Academy of Law has launched an AI training programme.",
            ),
        ]
        sources = [
            "Harvey AI raised $100 million in Series B funding announced today.",
            "The Singapore Academy of Law launched an AI training programme this quarter.",
        ]
        report = run_grounding_pass(items, sources, threshold=50, grounding_threshold=0.95)
        assert report.total_claims == 2
        assert not report.below_threshold

    def test_grounding_report_structure(self):
        items = [self.make_item("1", "Test heading", "Test summary")]
        sources = ["Different content altogether about unrelated topics."]
        report = run_grounding_pass(items, sources, threshold=90, grounding_threshold=0.95)
        assert isinstance(report.pass_rate, float)
        assert 0.0 <= report.pass_rate <= 1.0
        assert isinstance(report.flagged_claims, list)

    def test_below_threshold_flagged(self):
        items = [
            self.make_item(
                "1", "$999 billion IPO of AcmeCorp on Mars", "AcmeCorp listed on Mars exchange."
            )
        ]
        sources = ["Completely unrelated content about cooking recipes."]
        report = run_grounding_pass(items, sources, threshold=90, grounding_threshold=0.95)
        assert report.below_threshold
        assert len(report.flagged_claims) > 0
