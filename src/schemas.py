"""
BetterWiser Legal-Tech AI Briefing Agent — Pydantic v2 Data Contracts

CRITICAL: Every data transfer between pipeline phases MUST use these models.
Never pass raw dicts, untyped JSON strings, or ad-hoc data structures between
phases. This is the #1 cause of pipeline failures in agentic workflows.

Validate on write, validate on read.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class BriefingTrack(str, Enum):
    """The three briefing tracks produced each month."""
    A = "A"
    B = "B"
    C = "C"


class SourceTier(str, Enum):
    """Authority tier for ranking sources by credibility."""
    TIER_1 = "tier_1"    # Government portals, official regulators
    TIER_2 = "tier_2"    # Reputable publications (Reuters, FT, McKinsey)
    TIER_3 = "tier_3"    # Everything else (blogs, aggregators, etc.)


# ---------------------------------------------------------------------------
# Phase 1: Run Context
# ---------------------------------------------------------------------------

class RunContext(BaseModel):
    """Immutable context object generated at pipeline start (Phase 1)."""
    run_id: str                          # e.g. "2026-03_run_20260324T150000"
    month: str                           # "YYYY-MM"
    tracks: list[BriefingTrack]
    dry_run: bool = True                 # default: don't send real emails
    send: bool = False                   # explicit --send flag required to email
    resume: bool = False
    runs_dir: str = "runs"
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @field_validator("month")
    @classmethod
    def validate_month(cls, v: str) -> str:
        import re
        if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", v):
            raise ValueError(
                f"month must be YYYY-MM format with month 01-12, got: {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# Phase 2: Gathered Data
# ---------------------------------------------------------------------------

class ScrapedSource(BaseModel):
    """A web page scraped via Jina → Spider → Crawl4AI tiered fallback."""
    url: str
    title: str
    content: str                         # cleaned markdown/text
    tier: SourceTier
    scraper_used: str                    # "jina" | "spider" | "crawl4ai" | "none"
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    word_count: int = 0
    error: Optional[str] = None          # populated if scraper partially failed

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://, got: {v}")
        return v


class AttachmentContent(BaseModel):
    """Parsed content from an email attachment."""
    filename: str
    content_type: str                    # MIME type
    extracted_text: str
    page_count: Optional[int] = None
    extraction_method: str              # "pymupdf" | "python-docx" | "openpyxl" | "pillow" | "none"
    error: Optional[str] = None


class EmailSource(BaseModel):
    """A single email read from the agent's Microsoft 365 inbox."""
    message_id: str
    subject: str
    sender: str
    received_at: datetime
    body_text: str                       # plain text / markdown extracted from HTML
    body_html: Optional[str] = None
    has_attachments: bool = False
    attachments: list[AttachmentContent] = Field(default_factory=list)
    extracted_links: list[str] = Field(default_factory=list)


class DiscoveredArticle(BaseModel):
    """An article discovered via Claude web search or Tavily research."""
    url: str
    title: str
    snippet: str                         # 2-3 sentence summary
    source_name: str
    published_date: Optional[str] = None
    track: BriefingTrack
    discovery_wave: Optional[int] = None  # 1-6 for Track C waves; None for A/B
    tier: SourceTier = SourceTier.TIER_3
    discovered_via: str = "claude_web_search"  # "claude_web_search" | "tavily" | "email_link"

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://, got: {v}")
        return v


class GatheringStats(BaseModel):
    """Telemetry collected during Phase 2 gathering."""
    emails_read: int = 0
    attachments_parsed: int = 0
    urls_scraped: int = 0
    scrape_failures: int = 0
    articles_discovered: int = 0
    tavily_calls: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = Field(default_factory=list)
    skipped_reasons: list[str] = Field(default_factory=list)


class GatheredData(BaseModel):
    """All intelligence gathered in Phase 2. Fed into Phase 3 synthesis."""
    run_context: RunContext
    scraped_sources: list[ScrapedSource] = Field(default_factory=list)
    email_sources: list[EmailSource] = Field(default_factory=list)
    discovered_articles: list[DiscoveredArticle] = Field(default_factory=list)
    historical_context: Optional[str] = None  # summary from previous months
    stats: GatheringStats = Field(default_factory=GatheringStats)


# ---------------------------------------------------------------------------
# Phase 3: Synthesis Models
# ---------------------------------------------------------------------------

class EventCluster(BaseModel):
    """A group of sources describing the same event (used in Pass 0 dedup)."""
    cluster_id: str
    theme: str                           # 1-sentence description of the event
    member_urls: list[str]
    representative_snippet: str
    is_new_entrant: bool = False         # Track A: first time this entity appears
    trend_annotation: Optional[str] = None  # e.g. "Third consecutive month"
    duplicate_count: int = 0


class BriefingItem(BaseModel):
    """A single entry in the final briefing (bullet point or paragraph)."""
    item_id: str
    track: BriefingTrack
    date_str: Optional[str] = None      # e.g. "On 23 March 2026"
    heading: str
    summary: str                         # 1-2 sentences
    url: str
    tier: SourceTier = SourceTier.TIER_3
    citation_id: Optional[str] = None   # from Citations API
    confidence_score: float = 1.0       # 0.0-1.0 from fact-check pass
    # Track C specific fields
    opinion_takeaway: Optional[str] = None
    betterwiser_relevance: Optional[str] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://, got: {v}")
        return v


class ThemeGroup(BaseModel):
    """A themed section within Track C thought leadership briefing."""
    theme_name: str                      # e.g. "AI Workforce Transformation"
    theme_description: str
    items: list[BriefingItem]
    betterwiser_relevance: str           # section-level BW relevance note


class SynthesisResult(BaseModel):
    """Output of the 6-pass synthesis pipeline for one track."""
    run_id: str
    track: BriefingTrack
    raw_html: str                        # Claude's raw HTML output from Pass 2
    items: list[BriefingItem] = Field(default_factory=list)
    theme_groups: list[ThemeGroup] = Field(default_factory=list)  # Track C only
    thinking_summary: Optional[str] = None  # extended thinking block
    hot_vendor_suggestion: Optional[str] = None  # Track A only
    pass_completed: list[int] = Field(default_factory=list)  # [0,1,2,3,35,4]


# ---------------------------------------------------------------------------
# Phase 3.5 + 4: Grounding & Validation
# ---------------------------------------------------------------------------

class FlaggedClaim(BaseModel):
    """A factual claim that failed grounding verification."""
    claim_text: str
    item_id: str
    reason: str                          # why it failed
    claim_type: str = "unsupported"     # "number" | "date" | "entity" | "unsupported"
    nearest_source_match: Optional[str] = None
    resolution: Optional[str] = None    # "corrected" | "removed" | "kept_with_caveat"
    suggested_fix: Optional[str] = None


class GroundingReport(BaseModel):
    """Result of the programmatic fuzzy-match grounding pass."""
    total_claims: int
    grounded_claims: int
    pass_rate: float                     # grounded / total
    below_threshold: bool = False        # True if pass_rate < config threshold
    flagged_claims: list[FlaggedClaim] = Field(default_factory=list)
    citation_coverage_rate: float = 0.0  # % of claims with citations attached


class LinkCheckResult(BaseModel):
    """Result of HTTP HEAD request on a single URL."""
    url: str
    status_code: Optional[int] = None
    reachable: bool = True
    redirect_url: Optional[str] = None
    wayback_fallback: Optional[str] = None  # Wayback Machine URL if dead
    error: Optional[str] = None


class ValidatedBriefing(BaseModel):
    """Final output after all 6 passes: ready (or held) for delivery."""
    synthesis: SynthesisResult
    grounding_report: GroundingReport
    link_results: list[LinkCheckResult] = Field(default_factory=list)
    final_html: str                      # Outlook-compatible HTML
    subject_line: str = ""
    held_for_review: bool = False        # True if grounding < threshold
    ready_to_send: bool = True


# ---------------------------------------------------------------------------
# Phase 5: Delivery
# ---------------------------------------------------------------------------

class DeliveryReceipt(BaseModel):
    """Confirmation of email send or dry-run save."""
    run_id: str
    track: BriefingTrack
    delivered: bool
    dry_run: bool
    output_path: Optional[str] = None   # local HTML file path (dry-run or archive)
    email_message_id: Optional[str] = None  # MS Graph message ID
    recipients: list[str] = Field(default_factory=list)
    delivered_at: Optional[datetime] = None
    error: Optional[str] = None
    held_for_review: bool = False
