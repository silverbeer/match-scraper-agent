"""Tests for tool functions with mocked scraper and queue client."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from agent.deps import RunContext
from agent.tools import scrape_matches, submit_matches


def _make_ctx(
    *,
    dry_run: bool = False,
    mock_queue: MagicMock | None = None,
) -> RunContext:
    """Create a RunContext with a mocked queue client."""
    queue = mock_queue or MagicMock()
    queue.submit_match.return_value = "task-id-123"
    return RunContext(
        queue_client=queue,
        missing_table_api_url="http://localhost:8000",
        missing_table_api_key="test-key",
        dry_run=dry_run,
    )


def _fake_match(
    *,
    match_id: str = "m-1",
    home: str = "Team A",
    away: str = "Team B",
    home_score: int | None = None,
    away_score: int | None = None,
) -> MagicMock:
    """Create a mock Match object mimicking src.scraper.models.Match."""
    m = MagicMock()
    m.match_id = match_id
    m.home_team = home
    m.away_team = away
    m.home_score = home_score
    m.away_score = away_score
    m.match_datetime = datetime(2026, 2, 20, 18, 0, tzinfo=UTC)
    m.location = "Stadium"
    m.competition = "League"
    m.match_status = "scheduled" if home_score is None else "completed"
    m.has_score.return_value = home_score is not None and away_score is not None
    return m


class TestScrapeMatches:
    def test_returns_match_summary(self) -> None:
        ctx = _make_ctx()

        mock_scraper = MagicMock()
        mock_scraper.scrape_matches = AsyncMock(
            return_value=[
                _fake_match(),
                _fake_match(match_id="m-2", home="Team C", away="Team D"),
            ]
        )

        with (
            patch("src.scraper.mls_scraper.MLSScraper", return_value=mock_scraper),
            patch("src.scraper.config.ScrapingConfig"),
        ):
            result = asyncio.run(
                scrape_matches(ctx, start_date="2026-02-18", end_date="2026-02-25")
            )

        assert "Found 2 matches" in result
        assert "Team A vs Team B" in result
        assert "Team C vs Team D" in result

    def test_no_matches_returns_message(self) -> None:
        ctx = _make_ctx()

        mock_scraper = MagicMock()
        mock_scraper.scrape_matches = AsyncMock(return_value=[])

        with (
            patch("src.scraper.mls_scraper.MLSScraper", return_value=mock_scraper),
            patch("src.scraper.config.ScrapingConfig"),
        ):
            result = asyncio.run(
                scrape_matches(ctx, start_date="2026-02-18", end_date="2026-02-25")
            )

        assert "No matches found" in result

    def test_stores_matches_in_ctx(self) -> None:
        ctx = _make_ctx()

        mock_scraper = MagicMock()
        mock_scraper.scrape_matches = AsyncMock(return_value=[_fake_match()])

        with (
            patch("src.scraper.mls_scraper.MLSScraper", return_value=mock_scraper),
            patch("src.scraper.config.ScrapingConfig"),
        ):
            asyncio.run(scrape_matches(ctx, start_date="2026-02-18", end_date="2026-02-25"))

        assert len(ctx._scraped_matches) == 1
        assert ctx._scraped_matches[0]["home_team"] == "Team A"
        assert ctx._scraped_matches[0]["match_time"] == "18:00"
        assert ctx._scraped_matches[0]["source"] == "match-scraper-agent"

    def test_scored_match_includes_scores(self) -> None:
        ctx = _make_ctx()

        mock_scraper = MagicMock()
        mock_scraper.scrape_matches = AsyncMock(
            return_value=[_fake_match(home_score=2, away_score=1)]
        )

        with (
            patch("src.scraper.mls_scraper.MLSScraper", return_value=mock_scraper),
            patch("src.scraper.config.ScrapingConfig"),
        ):
            result = asyncio.run(
                scrape_matches(ctx, start_date="2026-02-18", end_date="2026-02-25")
            )

        assert "(2-1)" in result


class TestSubmitMatches:
    def test_submits_scraped_matches(self) -> None:
        mock_queue = MagicMock()
        mock_queue.submit_match.return_value = "task-123"
        ctx = _make_ctx(mock_queue=mock_queue)
        ctx._scraped_matches = [
            {"home_team": "A", "away_team": "B", "match_date": "2026-02-20"},
        ]

        result = asyncio.run(submit_matches(ctx))
        assert "Submitted 1 matches" in result
        mock_queue.submit_match.assert_called_once()

    def test_dry_run_skips_submission(self) -> None:
        mock_queue = MagicMock()
        ctx = _make_ctx(dry_run=True, mock_queue=mock_queue)
        ctx._scraped_matches = [
            {"home_team": "A", "away_team": "B", "match_date": "2026-02-20"},
        ]

        result = asyncio.run(submit_matches(ctx))
        assert "[DRY RUN]" in result
        mock_queue.submit_match.assert_not_called()

    def test_no_matches_returns_message(self) -> None:
        ctx = _make_ctx()

        result = asyncio.run(submit_matches(ctx))
        assert "No matches to submit" in result

    def test_handles_submission_errors(self) -> None:
        mock_queue = MagicMock()
        mock_queue.submit_match.side_effect = [
            "task-1",
            Exception("connection lost"),
            "task-3",
        ]
        ctx = _make_ctx(mock_queue=mock_queue)
        ctx._scraped_matches = [
            {"home_team": "A", "away_team": "B"},
            {"home_team": "C", "away_team": "D"},
            {"home_team": "E", "away_team": "F"},
        ]

        result = asyncio.run(submit_matches(ctx))
        assert "Submitted 2 matches" in result
        assert "1 errors" in result


class TestClampScrapeRange:
    """
    Guards on the window handed to the scraper (SB-546).

    MLS Next serves only the current season; an earlier start makes the
    calendar widget fail rather than return less, and an inverted range makes
    ScrapingConfig reject look_back_days outright.
    """

    def test_start_is_raised_to_the_season_start(self):
        from agent.tools import SEASON_START, clamp_scrape_range

        start, _ = clamp_scrape_range(
            SEASON_START - timedelta(days=40), SEASON_START + timedelta(days=30)
        )

        assert start == SEASON_START

    def test_start_inside_the_season_is_untouched(self):
        from agent.tools import SEASON_START, clamp_scrape_range

        wanted = SEASON_START + timedelta(days=60)
        start, end = clamp_scrape_range(wanted, wanted + timedelta(days=7))

        assert start == wanted
        assert end == wanted + timedelta(days=7)

    def test_inverted_range_is_never_returned(self):
        """
        The production failure: end 33 days before start produced
        look_back_days=-33 and killed every run.
        """
        from agent.tools import SEASON_START, clamp_scrape_range

        start, end = clamp_scrape_range(
            SEASON_START + timedelta(days=1), SEASON_START - timedelta(days=32)
        )

        assert end >= start
        assert (end - start).days >= 0

    def test_look_back_days_is_never_negative(self):
        """Whatever we feed it, the derived look_back must satisfy ScrapingConfig."""
        from agent.tools import SEASON_START, clamp_scrape_range

        for start_offset in (-400, -60, -1, 0, 1, 200):
            for end_offset in (-400, -33, 0, 5, 300):
                start, end = clamp_scrape_range(
                    SEASON_START + timedelta(days=start_offset),
                    SEASON_START + timedelta(days=end_offset),
                )
                assert start >= SEASON_START
                assert (end - start).days >= 0

    def test_season_end_is_after_season_start(self):
        from agent.tools import SEASON_END, SEASON_START

        assert SEASON_START < SEASON_END
