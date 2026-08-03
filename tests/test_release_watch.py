"""
Tests for the schedule release watcher (SB-538).

The watcher runs unattended for days at a time, so the behaviour that matters
is when it stays *quiet*. Most of these tests assert that nothing was sent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from src.models.schedule_release import DivisionRelease, ReleaseProbe, ReleaseState
from typer.testing import CliRunner

from cli.main import app
from utils.release_watch import ReleaseWatchState, read_state, write_state

BUCKET = "missingtable-match-scraper"
KEY = "release-watch/state.json"

runner = CliRunner()


def _result(age="U15", division="Northeast", state=ReleaseState.EMPTY, count=0, error=None):
    return DivisionRelease(
        age_group=age, division=division, state=state, match_count=count, error=error
    )


def _probe(results, season="2026-2027"):
    return ReleaseProbe(
        season=season,
        window_start=date(2026, 8, 1),
        window_end=date(2026, 12, 31),
        checked_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        results=results,
    )


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


class TestReleaseWatchState:
    def test_everything_is_new_on_a_blank_state(self):
        state = ReleaseWatchState()
        assert state.newly_live(["U15 Northeast"]) == ["U15 Northeast"]

    def test_remembered_targets_are_not_new(self):
        state = ReleaseWatchState(seen_live=["U15 Northeast"])
        assert state.newly_live(["U15 Northeast"]) == []

    def test_only_the_unseen_targets_are_new(self):
        state = ReleaseWatchState(seen_live=["U15 Northeast"])
        assert state.newly_live(["U15 Northeast", "U15 Florida"]) == ["U15 Florida"]

    def test_record_live_deduplicates(self):
        state = ReleaseWatchState(seen_live=["U15 Northeast"])
        state.record_live(["U15 Northeast", "U15 Florida"])
        assert state.seen_live == ["U15 Florida", "U15 Northeast"]

    def test_season_change_clears_memory(self):
        """Otherwise the watcher goes permanently quiet after its first season."""
        state = ReleaseWatchState(
            seen_live=["U15 Northeast"],
            last_season="2025-2026",
            consecutive_failures=2,
            failure_alert_sent=True,
        )
        state.reset_for_season("2026-2027")

        assert state.seen_live == []
        assert state.consecutive_failures == 0
        assert state.failure_alert_sent is False
        assert state.last_season == "2026-2027"

    def test_same_season_keeps_memory(self):
        state = ReleaseWatchState(seen_live=["U15 Northeast"], last_season="2026-2027")
        state.reset_for_season("2026-2027")
        assert state.seen_live == ["U15 Northeast"]

    def test_first_ever_run_records_the_season_without_clearing(self):
        state = ReleaseWatchState(seen_live=["U15 Northeast"])
        state.reset_for_season("2026-2027")
        assert state.seen_live == ["U15 Northeast"]
        assert state.last_season == "2026-2027"

    def test_touch_stamps_probe_time(self):
        state = ReleaseWatchState()
        state.touch(datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
        assert state.last_probe_at.startswith("2026-08-07T12:00")


# ---------------------------------------------------------------------------
# S3 round trip
# ---------------------------------------------------------------------------


class TestS3State:
    def test_round_trip(self):
        state = ReleaseWatchState(seen_live=["U15 Northeast"], last_season="2026-2027")
        captured = {}

        s3 = MagicMock()
        s3.put_object.side_effect = lambda **kw: captured.update(kw)

        with patch("boto3.client", return_value=s3):
            write_state(BUCKET, KEY, state)

        body = captured["Body"]
        s3_read = MagicMock()
        s3_read.get_object.return_value = {"Body": MagicMock(read=lambda: body)}

        with patch("boto3.client", return_value=s3_read):
            restored = read_state(BUCKET, KEY)

        assert restored.seen_live == ["U15 Northeast"]
        assert restored.last_season == "2026-2027"

    def test_missing_object_yields_blank_state(self):
        s3 = MagicMock()
        s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "nope"}}, "GetObject"
        )

        with patch("boto3.client", return_value=s3):
            assert read_state(BUCKET, KEY).seen_live == []

    def test_unreadable_state_yields_blank_rather_than_raising(self):
        """A watcher that crashes on bad state stops watching entirely."""
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"not json")}

        with patch("boto3.client", return_value=s3):
            assert read_state(BUCKET, KEY).seen_live == []

    def test_write_failure_never_raises(self):
        s3 = MagicMock()
        s3.put_object.side_effect = RuntimeError("S3 is down")

        with patch("boto3.client", return_value=s3):
            write_state(BUCKET, KEY, ReleaseWatchState())  # must not raise


# ---------------------------------------------------------------------------
# Local file round trip — the backend actually deployed on the cluster, which
# has no bucket and no AWS credentials.
# ---------------------------------------------------------------------------


class TestLocalFileState:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "release-watch.json")
        write_state("", KEY, ReleaseWatchState(seen_live=["U15 Northeast"]), path)

        restored = read_state("", KEY, path)
        assert restored.seen_live == ["U15 Northeast"]

    def test_missing_file_yields_blank_state(self, tmp_path):
        assert read_state("", KEY, str(tmp_path / "absent.json")).seen_live == []

    def test_unreadable_state_yields_blank_rather_than_raising(self, tmp_path):
        path = tmp_path / "release-watch.json"
        path.write_text("not json")
        assert read_state("", KEY, str(path)).seen_live == []

    def test_parent_directory_is_created(self, tmp_path):
        path = str(tmp_path / "nested" / "dir" / "release-watch.json")
        write_state("", KEY, ReleaseWatchState(seen_live=["U15 Florida"]), path)
        assert read_state("", KEY, path).seen_live == ["U15 Florida"]

    def test_write_failure_never_raises(self, tmp_path):
        """A directory where the file should be — write fails, watcher lives."""
        path = tmp_path / "release-watch.json"
        path.mkdir()
        write_state("", KEY, ReleaseWatchState(), str(path))  # must not raise

    def test_a_failed_write_leaves_the_previous_state_intact(self, tmp_path):
        """Truncated state parses as blank, which would re-announce."""
        path = tmp_path / "release-watch.json"
        write_state("", KEY, ReleaseWatchState(seen_live=["U15 Northeast"]), str(path))

        with patch("pathlib.Path.replace", side_effect=OSError("disk full")):
            write_state("", KEY, ReleaseWatchState(seen_live=["U15 Florida"]), str(path))

        assert read_state("", KEY, str(path)).seen_live == ["U15 Northeast"]

    def test_no_bucket_and_no_path_is_blank_not_a_crash(self):
        assert read_state("", KEY, "").seen_live == []
        write_state("", KEY, ReleaseWatchState(), "")  # must not raise

    def test_a_configured_bucket_still_wins(self, tmp_path):
        """One env var flips back to S3 — the local path is not consulted."""
        path = tmp_path / "release-watch.json"
        path.write_text(ReleaseWatchState(seen_live=["stale local"]).model_dump_json())

        s3 = MagicMock()
        s3.get_object.return_value = {
            "Body": MagicMock(
                read=lambda: ReleaseWatchState(seen_live=["from s3"]).model_dump_json()
            )
        }
        with patch("boto3.client", return_value=s3):
            assert read_state(BUCKET, KEY, str(path)).seen_live == ["from s3"]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@pytest.fixture
def sent():
    """Capture Telegram messages instead of sending them."""
    messages: list[str] = []
    with patch(
        "cli.main._send_telegram_message",
        side_effect=lambda settings, message, *, subject: messages.append(message),
    ):
        yield messages


def _run(probe, state, sent_bucket="bucket", extra_args=()):
    """Invoke watch-release against a canned probe and in-memory state."""
    saved: dict = {}

    with (
        patch(
            "src.scraper.release_detector.ScheduleReleaseDetector.probe",
            new=MagicMock(return_value=_async(probe)),
        ),
        patch("utils.release_watch.read_state", return_value=state),
        patch(
            "utils.release_watch.write_state",
            side_effect=lambda b, k, s, p="": saved.update({"state": s, "bucket": b, "path": p}),
        ),
        # AgentSettings reads AGENT_-prefixed env vars; the field itself is a
        # pydantic-settings descriptor and cannot be patched as an attribute.
        patch.dict("os.environ", {"AGENT_JOURNAL_S3_BUCKET": sent_bucket}),
    ):
        result = runner.invoke(app, ["watch-release", *extra_args])
    return result, saved.get("state")


def _async(value):
    """Wrap a value in an awaitable, since probe() is async."""

    async def _coro():
        return value

    return _coro()


class TestWatchReleaseCommand:
    def test_quiet_when_nothing_published(self, sent):
        result, state = _run(_probe([_result()]), ReleaseWatchState())

        assert result.exit_code == 0
        assert sent == [], "an unpublished schedule is not news"
        assert state.seen_live == []

    def test_announces_a_new_release_once(self, sent):
        probe = _probe([_result(state=ReleaseState.LIVE, count=25)])
        result, state = _run(probe, ReleaseWatchState())

        assert result.exit_code == 10
        assert len(sent) == 1
        assert "schedule published" in sent[0].lower()
        assert state.seen_live == ["U15 Northeast"]

    def test_does_not_re_announce_a_known_release(self, sent):
        probe = _probe([_result(state=ReleaseState.LIVE, count=25)])
        state_in = ReleaseWatchState(seen_live=["U15 Northeast"], last_season="2026-2027")

        result, _ = _run(probe, state_in)

        assert result.exit_code == 0
        assert sent == [], "the whole point of the S3 state"

    def test_announces_only_the_newly_live_division(self, sent):
        probe = _probe(
            [
                _result(state=ReleaseState.LIVE, count=25),
                _result(division="Florida", state=ReleaseState.LIVE, count=12),
            ]
        )
        state_in = ReleaseWatchState(seen_live=["U15 Northeast"], last_season="2026-2027")

        result, state = _run(probe, state_in)

        assert result.exit_code == 10
        assert "U15 Florida" in sent[0]
        assert "U15 Northeast" not in sent[0]
        assert state.seen_live == ["U15 Florida", "U15 Northeast"]

    def test_single_outage_is_silent(self, sent):
        """One failed run against someone else's server is noise."""
        probe = _probe([_result(state=ReleaseState.ERROR, error="timeout")])
        result, state = _run(probe, ReleaseWatchState())

        assert result.exit_code == 20
        assert sent == []
        assert state.consecutive_failures == 1

    def test_sustained_outage_alerts_at_the_threshold(self, sent):
        probe = _probe([_result(state=ReleaseState.ERROR, error="timeout")])
        state_in = ReleaseWatchState(consecutive_failures=2, last_season="2026-2027")

        result, state = _run(probe, state_in)

        assert result.exit_code == 20
        assert len(sent) == 1
        assert "failing" in sent[0].lower()
        assert state.failure_alert_sent is True

    def test_sustained_outage_alerts_only_once(self, sent):
        probe = _probe([_result(state=ReleaseState.ERROR, error="timeout")])
        state_in = ReleaseWatchState(
            consecutive_failures=5, failure_alert_sent=True, last_season="2026-2027"
        )

        result, _ = _run(probe, state_in)

        assert result.exit_code == 20
        assert sent == [], "already alerted for this streak"

    def test_state_survives_between_runs_on_the_local_backend(self, sent, tmp_path):
        """
        The deployed shape: no bucket, state on the PVC. Two runs against the
        same published probe must announce exactly once — this is the whole
        reason the CronJob mounts a volume.
        """
        probe_patch = patch(
            "src.scraper.release_detector.ScheduleReleaseDetector.probe",
            new=MagicMock(
                side_effect=lambda: _async(_probe([_result(state=ReleaseState.LIVE, count=25)]))
            ),
        )
        env = {
            "AGENT_JOURNAL_S3_BUCKET": "",
            "AGENT_RELEASE_WATCH_STATE_PATH": str(tmp_path / "release-watch.json"),
        }

        with probe_patch, patch.dict("os.environ", env):
            first = runner.invoke(app, ["watch-release"])
            second = runner.invoke(app, ["watch-release"])

        assert first.exit_code == 10
        assert second.exit_code == 0, "a remembered release is not news"
        assert len(sent) == 1

    def test_dry_run_writes_no_state(self, sent, tmp_path):
        """Otherwise a dry run would silence the real announcement."""
        state_file = tmp_path / "release-watch.json"
        probe_patch = patch(
            "src.scraper.release_detector.ScheduleReleaseDetector.probe",
            new=MagicMock(
                return_value=_async(_probe([_result(state=ReleaseState.LIVE, count=25)]))
            ),
        )
        env = {
            "AGENT_JOURNAL_S3_BUCKET": "",
            "AGENT_RELEASE_WATCH_STATE_PATH": str(state_file),
        }

        with probe_patch, patch.dict("os.environ", env):
            result = runner.invoke(app, ["watch-release", "--dry-run"])

        assert result.exit_code == 10
        assert sent == []
        assert not state_file.exists()

    def test_recovery_clears_the_failure_streak(self, sent):
        probe = _probe([_result()])
        state_in = ReleaseWatchState(
            consecutive_failures=4, failure_alert_sent=True, last_season="2026-2027"
        )

        result, state = _run(probe, state_in)

        assert result.exit_code == 0
        assert state.consecutive_failures == 0
        assert state.failure_alert_sent is False

    def test_partial_failure_is_not_an_outage(self, sent):
        probe = _probe(
            [_result(), _result(division="Florida", state=ReleaseState.ERROR, error="x")]
        )
        result, state = _run(probe, ReleaseWatchState())

        assert result.exit_code == 0
        assert sent == []
        assert state.consecutive_failures == 0

    def test_dry_run_sends_nothing_and_saves_nothing(self, sent):
        probe = _probe([_result(state=ReleaseState.LIVE, count=25)])
        result, state = _run(probe, ReleaseWatchState(), extra_args=["--dry-run"])

        assert sent == []
        assert state is None, "dry run must not persist"
        assert result.exit_code == 10, "still reports what it found"

    def test_unknown_division_exits_one(self):
        result = runner.invoke(app, ["watch-release", "-d", "Atlantis"])
        assert result.exit_code == 1
