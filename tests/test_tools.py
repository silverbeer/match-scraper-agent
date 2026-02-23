"""Tests for agent tool functions with mocked scraper and queue client."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic_ai import RunContext

from agent.deps import AgentDeps
from agent.tools import get_today_info, scrape_matches, submit_matches
from config.settings import AgentSettings


def _make_deps(
    *,
    dry_run: bool = False,
    mock_queue: MagicMock | None = None,
) -> AgentDeps:
    """Create AgentDeps with a mocked queue client."""
    queue = mock_queue or MagicMock()
    queue.submit_match.return_value = "task-id-123"
    settings = AgentSettings()
    return AgentDeps(queue_client=queue, settings=settings, dry_run=dry_run)


def _make_ctx(deps: AgentDeps) -> RunContext[AgentDeps]:
    """Create a minimal RunContext for testing tools outside PydanticAI."""
    return RunContext(
        deps=deps,
        model=None,  # type: ignore[arg-type]
        usage={},  # type: ignore[arg-type]
        prompt="test",
        run_step=0,
        retry=0,
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


class TestGetTodayInfo:
    def test_returns_date_info(self) -> None:
        deps = _make_deps()
        ctx = _make_ctx(deps)
        result = get_today_info(ctx)
        assert "Date:" in result
        assert "Day:" in result
        assert "Week:" in result

    def test_returns_time_utc(self) -> None:
        deps = _make_deps()
        ctx = _make_ctx(deps)
        result = get_today_info(ctx)
        assert "Time (UTC):" in result


class TestScrapeMatches:
    def test_returns_match_summary(self) -> None:
        deps = _make_deps()
        ctx = _make_ctx(deps)

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
        deps = _make_deps()
        ctx = _make_ctx(deps)

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

    def test_stores_matches_in_deps(self) -> None:
        deps = _make_deps()
        ctx = _make_ctx(deps)

        mock_scraper = MagicMock()
        mock_scraper.scrape_matches = AsyncMock(return_value=[_fake_match()])

        with (
            patch("src.scraper.mls_scraper.MLSScraper", return_value=mock_scraper),
            patch("src.scraper.config.ScrapingConfig"),
        ):
            asyncio.run(scrape_matches(ctx, start_date="2026-02-18", end_date="2026-02-25"))

        assert len(deps._scraped_matches) == 1
        assert deps._scraped_matches[0]["home_team"] == "Team A"
        assert deps._scraped_matches[0]["match_time"] == "18:00"
        assert deps._scraped_matches[0]["source"] == "match-scraper-agent"

    def test_scored_match_includes_scores(self) -> None:
        deps = _make_deps()
        ctx = _make_ctx(deps)

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
        deps = _make_deps(mock_queue=mock_queue)
        deps._scraped_matches = [
            {"home_team": "A", "away_team": "B", "match_date": "2026-02-20"},
        ]
        ctx = _make_ctx(deps)

        result = asyncio.run(submit_matches(ctx))
        assert "Submitted 1 matches" in result
        mock_queue.submit_match.assert_called_once()

    def test_dry_run_skips_submission(self) -> None:
        mock_queue = MagicMock()
        deps = _make_deps(dry_run=True, mock_queue=mock_queue)
        deps._scraped_matches = [
            {"home_team": "A", "away_team": "B", "match_date": "2026-02-20"},
        ]
        ctx = _make_ctx(deps)

        result = asyncio.run(submit_matches(ctx))
        assert "[DRY RUN]" in result
        mock_queue.submit_match.assert_not_called()

    def test_no_matches_returns_message(self) -> None:
        deps = _make_deps()
        ctx = _make_ctx(deps)

        result = asyncio.run(submit_matches(ctx))
        assert "No matches to submit" in result

    def test_handles_submission_errors(self) -> None:
        mock_queue = MagicMock()
        mock_queue.submit_match.side_effect = [
            "task-1",
            Exception("connection lost"),
            "task-3",
        ]
        deps = _make_deps(mock_queue=mock_queue)
        deps._scraped_matches = [
            {"home_team": "A", "away_team": "B"},
            {"home_team": "C", "away_team": "D"},
            {"home_team": "E", "away_team": "F"},
        ]
        ctx = _make_ctx(deps)

        result = asyncio.run(submit_matches(ctx))
        assert "Submitted 2 matches" in result
        assert "1 errors" in result
