"""
Cross-run state for the schedule release watcher.

CronJob pods are ephemeral, so "have we already announced this?" cannot live
in the container filesystem — a fresh pod would re-announce on every run,
which is exactly the alert spam the watcher exists to avoid.

Two backends, chosen by whether an S3 bucket is configured:

* ``AGENT_JOURNAL_S3_BUCKET`` set → S3, alongside the run journal.
* otherwise → a JSON file at ``AGENT_RELEASE_WATCH_STATE_PATH``, which on the
  cluster points into the ``agent-state`` PVC. The live cluster has no bucket
  and no AWS credentials, so this is the deployed path; setting the bucket env
  var switches back to S3 with no code change.

Mirrors ``utils.journal``: lazy boto3 import, and reads/writes that log and
carry on rather than raising. A watcher that dies because its store hiccuped
is worse than one that sends a duplicate message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()


class ReleaseWatchState(BaseModel):
    """What the watcher remembers between runs."""

    seen_live: list[str] = Field(
        default_factory=list,
        description="Target labels ('U15 Northeast') already announced as published",
    )
    consecutive_failures: int = Field(
        default=0,
        ge=0,
        description="Runs in a row where every target failed to probe",
    )
    failure_alert_sent: bool = Field(
        default=False,
        description="Whether the current failure streak has already been alerted",
    )
    last_probe_at: str = Field(
        default="", description="ISO 8601 UTC timestamp of the last completed probe"
    )
    last_season: str = Field(default="", description="Season the remembered targets belong to")

    def newly_live(self, live_labels: list[str]) -> list[str]:
        """Return the labels that are live now and were not live before."""
        known = set(self.seen_live)
        return [label for label in live_labels if label not in known]

    def record_live(self, labels: list[str]) -> None:
        """Remember these targets as announced, keeping the list stable-sorted."""
        self.seen_live = sorted(set(self.seen_live) | set(labels))

    def reset_for_season(self, season: str) -> None:
        """
        Drop remembered targets when the season changes.

        Without this, last season's announcements would suppress this season's
        — the watcher would go permanently quiet after its first success.
        """
        if self.last_season and self.last_season != season:
            logger.info(
                "release_watch.season_changed",
                previous=self.last_season,
                current=season,
            )
            self.seen_live = []
            self.consecutive_failures = 0
            self.failure_alert_sent = False
        self.last_season = season

    def touch(self, now: datetime | None = None) -> None:
        """Stamp the probe time."""
        self.last_probe_at = (now or datetime.now(tz=UTC)).isoformat()


def read_state(bucket: str, key: str, local_path: str = "") -> ReleaseWatchState:
    """
    Read watch state from S3, or from ``local_path`` when no bucket is set.

    Returns a blank state when the object is missing or unreadable — the
    watcher should start announcing again rather than refuse to run.
    """
    if not bucket:
        return _read_local(local_path)

    import boto3
    from botocore.exceptions import ClientError

    try:
        s3 = boto3.client("s3")
        response = s3.get_object(Bucket=bucket, Key=key)
        return ReleaseWatchState.model_validate_json(response["Body"].read())
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        reason = "not found" if code == "NoSuchKey" else str(exc)
        logger.debug("release_watch.read_skipped", bucket=bucket, key=key, reason=reason)
        return ReleaseWatchState()
    except Exception as exc:
        logger.debug("release_watch.read_skipped", bucket=bucket, key=key, reason=str(exc))
        return ReleaseWatchState()


def write_state(bucket: str, key: str, state: ReleaseWatchState, local_path: str = "") -> None:
    """Write watch state to S3, or to ``local_path`` when no bucket is set. Never raises."""
    if not bucket:
        _write_local(local_path, state)
        return

    import boto3

    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=state.model_dump_json(indent=2),
            ContentType="application/json",
        )
        logger.info("release_watch.written", bucket=bucket, key=key)
    except Exception as exc:
        logger.warning("release_watch.write_failed", bucket=bucket, key=key, error=str(exc))


def _read_local(path: str) -> ReleaseWatchState:
    """Read watch state from a JSON file, blank if absent or unreadable."""
    if not path:
        logger.warning(
            "release_watch.no_state_store",
            detail="no S3 bucket and no state path — every run will re-announce",
        )
        return ReleaseWatchState()

    try:
        return ReleaseWatchState.model_validate_json(Path(path).read_text())
    except FileNotFoundError:
        logger.debug("release_watch.read_skipped", path=path, reason="not found")
        return ReleaseWatchState()
    except Exception as exc:
        logger.debug("release_watch.read_skipped", path=path, reason=str(exc))
        return ReleaseWatchState()


def _write_local(path: str, state: ReleaseWatchState) -> None:
    """
    Write watch state to a JSON file. Never raises.

    Writes to a sibling temp file and renames, so a pod killed mid-write leaves
    the previous state intact rather than a truncated file that parses as blank
    and re-announces.
    """
    if not path:
        return

    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.replace(target)
        logger.info("release_watch.written", path=path)
    except Exception as exc:
        logger.warning("release_watch.write_failed", path=path, error=str(exc))
