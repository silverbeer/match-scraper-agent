"""Tests for the run summary report builder."""

from __future__ import annotations

from datetime import UTC, datetime

from utils.report import (
    _agent_awareness,
    _format_delta,
    _is_last_weekend,
    _next_scheduled_run,
    build_report,
)


def _match(
    home: str = "IFA",
    away: str = "Revolution",
    match_date: str = "2026-03-08",
    status: str = "completed",
    home_score: int | None = 2,
    away_score: int | None = 1,
) -> dict:
    return {
        "home_team": home,
        "away_team": away,
        "match_date": match_date,
        "match_status": status,
        "home_score": home_score,
        "away_score": away_score,
    }


class TestBuildReport:
    def test_basic_report_contains_header(self) -> None:
        now = datetime(2026, 3, 8, 14, 2, tzinfo=UTC)  # Saturday
        report = build_report(
            result_summary="Completed run",
            actions=[{"action": "scrape", "detail": "Scraped U14 HG NE", "dry_run": False}],
            matches_found=5,
            matches_submitted=5,
            scraped_matches=[_match()],
            submission_errors=[],
            env="prod",
            target="u14-hg",
            dry_run=False,
            now=now,
        )
        assert "Match Scraper Report" in report
        assert "prod" in report
        assert "u14\\-hg" in report

    def test_dry_run_shown_in_header(self) -> None:
        now = datetime(2026, 3, 8, 14, 0, tzinfo=UTC)
        report = build_report(
            result_summary="",
            actions=[],
            matches_found=0,
            matches_submitted=0,
            scraped_matches=[],
            submission_errors=[],
            env="local",
            target=None,
            dry_run=True,
            now=now,
        )
        assert "DRY RUN" in report

    def test_errors_shown(self) -> None:
        now = datetime(2026, 3, 8, 14, 0, tzinfo=UTC)
        report = build_report(
            result_summary="",
            actions=[],
            matches_found=2,
            matches_submitted=1,
            scraped_matches=[_match()],
            submission_errors=[{"match": "IFA vs Rev", "error": "connection refused"}],
            env="prod",
            target=None,
            dry_run=False,
            now=now,
        )
        assert "Submission Errors" in report
        assert "connection refused" in report

    def test_next_run_shown(self) -> None:
        now = datetime(2026, 3, 8, 14, 2, tzinfo=UTC)
        report = build_report(
            result_summary="",
            actions=[],
            matches_found=0,
            matches_submitted=0,
            scraped_matches=[],
            submission_errors=[],
            env="prod",
            target=None,
            dry_run=False,
            now=now,
        )
        assert "Next run" in report
        assert "20:00 UTC" in report

    def test_today_missing_scores_shown(self) -> None:
        now = datetime(2026, 3, 8, 14, 0, tzinfo=UTC)  # Saturday
        matches = [
            _match(status="completed"),
            _match(home="NYCFC", away="Red Bulls", status="scheduled", home_score=None, away_score=None),
        ]
        report = build_report(
            result_summary="",
            actions=[],
            matches_found=2,
            matches_submitted=2,
            scraped_matches=matches,
            submission_errors=[],
            env="prod",
            target=None,
            dry_run=False,
            now=now,
        )
        assert "Awaiting Scores" in report
        assert "NYCFC" in report


class TestAgentAwareness:
    def test_saturday(self) -> None:
        now = datetime(2026, 3, 7, 14, 0, tzinfo=UTC)  # Saturday
        result = _agent_awareness(now, [])
        assert "Active match day" in result

    def test_sunday(self) -> None:
        now = datetime(2026, 3, 8, 14, 0, tzinfo=UTC)  # Sunday
        result = _agent_awareness(now, [])
        assert "Active match day" in result

    def test_wednesday_all_scored(self) -> None:
        now = datetime(2026, 3, 11, 14, 0, tzinfo=UTC)  # Wednesday
        matches = [
            _match(match_date="2026-03-07", status="completed"),
            _match(match_date="2026-03-08", status="completed"),
        ]
        result = _agent_awareness(now, matches)
        assert "all weekend scores are posted" in result

    def test_monday_unscored(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)  # Monday
        matches = [
            _match(match_date="2026-03-07", status="scheduled"),
        ]
        result = _agent_awareness(now, matches)
        assert "still awaiting scores" in result

    def test_thursday(self) -> None:
        now = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)  # Thursday
        result = _agent_awareness(now, [])
        assert "routine schedule sync" in result


class TestIsLastWeekend:
    def test_last_saturday(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)  # Monday
        assert _is_last_weekend("2026-03-07", now) is True  # Saturday

    def test_last_sunday(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)  # Monday
        assert _is_last_weekend("2026-03-08", now) is True  # Sunday

    def test_older_weekend(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)  # Monday
        assert _is_last_weekend("2026-02-28", now) is False  # Previous Sat

    def test_weekday(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)
        assert _is_last_weekend("2026-03-06", now) is False  # Friday


class TestNextScheduledRun:
    def test_mid_morning(self) -> None:
        now = datetime(2026, 3, 8, 10, 30, tzinfo=UTC)
        next_run, delta = _next_scheduled_run(now)
        assert next_run.hour == 14

    def test_after_last_slot(self) -> None:
        now = datetime(2026, 3, 8, 21, 0, tzinfo=UTC)
        next_run, delta = _next_scheduled_run(now)
        assert next_run.hour == 2
        assert next_run.day == 9

    def test_before_first_slot(self) -> None:
        now = datetime(2026, 3, 8, 1, 0, tzinfo=UTC)
        next_run, _ = _next_scheduled_run(now)
        assert next_run.hour == 2


class TestFormatDelta:
    def test_hours_and_minutes(self) -> None:
        from datetime import timedelta

        assert _format_delta(timedelta(hours=5, minutes=58)) == "5h 58m"

    def test_minutes_only(self) -> None:
        from datetime import timedelta

        assert _format_delta(timedelta(minutes=45)) == "45m"
