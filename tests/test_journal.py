"""Tests for the run journal (cross-run memory)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from utils.journal import (
    RunJournal,
    TargetEntry,
    build_journal,
    format_journal_log,
    read_journal,
    write_journal,
)

BUCKET = "missingtable-match-scraper"
KEY = "journal/latest.json"


def _match(
    home: str = "IFA",
    away: str = "Revolution",
    match_date: str = "2026-03-08",
    status: str = "completed",
    home_score: int | None = 2,
    away_score: int | None = 1,
    age_group: str = "U14",
    league: str = "Homegrown",
    division: str = "Northeast",
) -> dict:
    return {
        "home_team": home,
        "away_team": away,
        "match_date": match_date,
        "match_status": status,
        "home_score": home_score,
        "away_score": away_score,
        "age_group": age_group,
        "league": league,
        "division": division,
    }


def _fake_result(
    summary: str = "All done.",
    matches_found: int = 5,
    matches_submitted: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        summary=summary,
        matches_found=matches_found,
        matches_submitted=matches_submitted,
    )


def _s3_body(journal: RunJournal) -> MagicMock:
    """Return a mock S3 response body for a serialised journal."""
    body = MagicMock()
    body.read.return_value = journal.model_dump_json(indent=2).encode()
    return body


class TestRunJournalModel:
    def test_round_trip(self) -> None:
        journal = RunJournal(
            timestamp="2026-03-09T14:00:00+00:00",
            run_id="abc123",
            agent_summary="Test run",
            targets=[TargetEntry(target="U14 HG NE", matches_found=10, completed=8)],
            total_matches_found=10,
            total_matches_submitted=10,
        )
        raw = journal.model_dump_json()
        restored = RunJournal.model_validate_json(raw)
        assert restored.run_id == "abc123"
        assert restored.targets[0].completed == 8

    def test_defaults(self) -> None:
        journal = RunJournal(timestamp="2026-03-09T14:00:00+00:00", run_id="x")
        assert journal.target is None
        assert journal.dry_run is False
        assert journal.targets == []
        assert journal.missing_score_matches == []


class TestReadJournal:
    def test_reads_from_s3(self) -> None:
        journal = RunJournal(timestamp="2026-03-09T14:00:00+00:00", run_id="abc")
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": _s3_body(journal)}

        with patch("boto3.client", return_value=mock_s3):
            result = read_journal(BUCKET, KEY)

        assert result is not None
        assert result.run_id == "abc"
        mock_s3.get_object.assert_called_once_with(Bucket=BUCKET, Key=KEY)

    def test_returns_none_when_key_missing(self) -> None:
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject"
        )

        with patch("boto3.client", return_value=mock_s3):
            result = read_journal(BUCKET, KEY)

        assert result is None

    def test_returns_none_on_s3_error(self) -> None:
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}, "GetObject"
        )

        with patch("boto3.client", return_value=mock_s3):
            result = read_journal(BUCKET, KEY)

        assert result is None

    def test_returns_none_for_invalid_json(self) -> None:
        body = MagicMock()
        body.read.return_value = b"not json at all"
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": body}

        with patch("boto3.client", return_value=mock_s3):
            result = read_journal(BUCKET, KEY)

        assert result is None


class TestWriteJournal:
    def test_writes_to_s3(self) -> None:
        journal = RunJournal(timestamp="2026-03-09T14:00:00+00:00", run_id="xyz")
        mock_s3 = MagicMock()

        with patch("boto3.client", return_value=mock_s3):
            write_journal(BUCKET, KEY, journal)

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == BUCKET
        assert call_kwargs["Key"] == KEY
        assert call_kwargs["ContentType"] == "application/json"
        payload = json.loads(call_kwargs["Body"])
        assert payload["run_id"] == "xyz"

    def test_does_not_raise_on_s3_error(self) -> None:
        from botocore.exceptions import ClientError

        journal = RunJournal(timestamp="2026-03-09T14:00:00+00:00", run_id="xyz")
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}, "PutObject"
        )

        with patch("boto3.client", return_value=mock_s3):
            write_journal(BUCKET, KEY, journal)  # must not raise


class TestBuildJournal:
    def test_basic(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)  # Monday
        matches = [
            _match(match_date="2026-03-07", status="completed"),
            _match(home="NYCFC", away="Red Bulls", match_date="2026-03-08", status="scheduled"),
        ]
        journal = build_journal(
            run_id="abc123",
            result=_fake_result(),
            scraped_matches=matches,
            submission_errors=[],
            target="u14-hg",
            dry_run=False,
            now=now,
        )
        assert journal.run_id == "abc123"
        assert journal.target == "u14-hg"
        assert journal.total_matches_found == 5
        assert len(journal.targets) == 1  # both matches are U14 HG Northeast
        assert journal.targets[0].completed == 1
        assert journal.targets[0].missing_scores == 1

    def test_weekend_status(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)  # Monday
        matches = [
            _match(match_date="2026-03-07", status="completed"),
            _match(match_date="2026-03-08", status="scheduled"),
        ]
        journal = build_journal(
            run_id="x",
            result=_fake_result(),
            scraped_matches=matches,
            submission_errors=[],
            target=None,
            dry_run=False,
            now=now,
        )
        assert "1 awaiting" in journal.weekend_scores_status

    def test_missing_scores_list(self) -> None:
        now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)
        matches = [
            _match(match_date="2026-03-08", status="tbd", home_score=None, away_score=None),
        ]
        journal = build_journal(
            run_id="x",
            result=_fake_result(),
            scraped_matches=matches,
            submission_errors=[],
            target=None,
            dry_run=False,
            now=now,
        )
        assert len(journal.missing_score_matches) == 1
        assert "IFA vs Revolution" in journal.missing_score_matches[0]


class TestFormatJournalLog:
    def test_none_returns_empty(self) -> None:
        assert format_journal_log(None) == ""

    def test_basic_format(self) -> None:
        journal = RunJournal(
            timestamp="2026-03-09T14:00:00+00:00",
            run_id="abc123",
            agent_summary="Scraped 5 targets.",
            targets=[TargetEntry(target="U14 HG NE", matches_found=42, completed=38)],
            total_matches_found=42,
            total_matches_submitted=42,
            weekend_scores_status="all posted",
        )
        text = format_journal_log(journal)
        assert "Previous Run Journal" in text
        assert "abc123" in text
        assert "42 found" in text
        assert "38 scored" in text
        assert "all posted" in text
        assert "Scraped 5 targets" in text
        assert "End Journal" in text

    def test_includes_missing_scores(self) -> None:
        journal = RunJournal(
            timestamp="t",
            run_id="x",
            missing_score_matches=["2026-03-08 IFA vs Revolution (tbd)"],
        )
        text = format_journal_log(journal)
        assert "IFA vs Revolution" in text

    def test_caps_missing_scores_at_10(self) -> None:
        journal = RunJournal(
            timestamp="t",
            run_id="x",
            missing_score_matches=[f"match {i}" for i in range(15)],
        )
        text = format_journal_log(journal)
        assert "and 5 more" in text
