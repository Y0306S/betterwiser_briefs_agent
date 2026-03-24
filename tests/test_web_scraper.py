"""
Tests for the tiered web scraper.
Mocks httpx to avoid real network calls.
"""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from src.schemas import SourceTier
from src.gatherers.web_scraper import (
    _try_jina,
    _extract_title,
    scrape_url,
    scrape_urls,
)


class TestExtractTitle:
    def test_path_based_title(self):
        title = _extract_title("https://harvey.ai/blog/contract-review-2026")
        assert "Contract Review 2026" in title or "contract" in title.lower()

    def test_domain_only(self):
        title = _extract_title("https://harvey.ai/")
        assert "harvey" in title.lower()

    def test_invalid_url(self):
        title = _extract_title("not-a-url")
        assert title == "not-a-url"


class TestJinaScraper:
    @pytest.mark.asyncio
    async def test_successful_jina_scrape(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "content": "Harvey AI launched a new contract review feature. " * 20,
                "title": "Harvey Product Update",
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await _try_jina("https://harvey.ai/blog/update")

        assert result is not None
        assert result.scraper_used == "jina"
        assert result.word_count >= 100

    @pytest.mark.asyncio
    async def test_jina_returns_none_on_low_word_count(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"content": "Short.", "title": "Short"}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await _try_jina("https://example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_jina_returns_none_on_exception(self):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=Exception("Connection error"))

            result = await _try_jina("https://example.com")

        assert result is None


class TestScrapeUrl:
    @pytest.mark.asyncio
    async def test_falls_back_to_error_source_on_all_failures(self):
        """When all tiers fail, return ScrapedSource with error field."""
        with (
            patch("src.gatherers.web_scraper._try_jina", new=AsyncMock(return_value=None)),
            patch("src.gatherers.web_scraper._try_spider", new=AsyncMock(return_value=None)),
            patch("src.gatherers.web_scraper._try_crawl4ai", new=AsyncMock(return_value=None)),
        ):
            result = await scrape_url("https://example.com")

        assert result.error is not None
        assert result.scraper_used == "none"
        assert result.word_count == 0

    @pytest.mark.asyncio
    async def test_uses_jina_result_when_sufficient(self):
        from src.schemas import ScrapedSource, SourceTier
        from datetime import datetime

        good_result = ScrapedSource(
            url="https://harvey.ai",
            title="Harvey Update",
            content="A" * 500,  # 500 chars, ~100 words
            tier=SourceTier.TIER_3,
            scraper_used="jina",
            word_count=100,
        )

        with (
            patch("src.gatherers.web_scraper._try_jina", new=AsyncMock(return_value=good_result)),
        ):
            result = await scrape_url("https://harvey.ai")

        assert result.scraper_used == "jina"
        assert not result.error


class TestScrapeUrls:
    @pytest.mark.asyncio
    async def test_empty_url_list(self):
        results = await scrape_urls([])
        assert results == []

    @pytest.mark.asyncio
    async def test_handles_exceptions_per_url(self):
        """scrape_urls should not raise even if individual scrapes fail."""
        from src.schemas import ScrapedSource, SourceTier

        error_result = ScrapedSource(
            url="https://example.com",
            title="",
            content="",
            tier=SourceTier.TIER_3,
            scraper_used="none",
            error="All scrapers failed",
        )

        with patch("src.gatherers.web_scraper.scrape_url", new=AsyncMock(return_value=error_result)):
            results = await scrape_urls(["https://example.com", "https://example2.com"])

        assert len(results) == 2
        for r in results:
            assert r.error is not None
