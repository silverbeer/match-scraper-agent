"""Tests for the run journal (cross-run memory)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from utils.journal import (
    RunJournal,
    TargetEntry,
    build_journal,
    format_journal_log,
    read_journal,
    write_journal,
)


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
    def test_reads_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "journal.json"
        journal = RunJournal(timestamp="2026-03-09T14:00:00+00:00", run_id="abc")
        p.write_text(journal.model_dump_json())
        result = read_journal(p)
        assert result is not None
        assert result.run_id == "abc"

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        result = read_journal(tmp_path / "nope.json")
        assert result is None

    def test_returns_none_for_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json at all")
        result = read_journal(p)
        assert result is None


class TestWriteJournal:
    def test_writes_and_reads_back(self, tmp_path: Path) -> None:
        p = tmp_path / "journal.json"
        journal = RunJournal(timestamp="2026-03-09T14:00:00+00:00", run_id="xyz")
        write_journal(p, journal)
        restored = read_journal(p)
        assert restored is not None
        assert restored.run_id == "xyz"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "nested" / "deep" / "journal.json"
        journal = RunJournal(timestamp="2026-03-09T14:00:00+00:00", run_id="nested")
        write_journal(p, journal)
        assert p.exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        p = tmp_path / "journal.json"
        write_journal(p, RunJournal(timestamp="t1", run_id="first"))
        write_journal(p, RunJournal(timestamp="t2", run_id="second"))
        result = read_journal(p)
        assert result is not None
        assert result.run_id == "second"


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
