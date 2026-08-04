"""
Run journal — lightweight cross-run memory for the agent.

Two backends, chosen by whether an S3 bucket is configured:

* ``AGENT_JOURNAL_S3_BUCKET`` set → S3.
* otherwise → a JSON file at ``AGENT_JOURNAL_PATH``, which on the cluster
  points into the ``agent-state`` PVC the CronJob already mounts. The live
  cluster has no bucket and no AWS credentials, so this is the deployed path;
  setting the bucket env var switches back to S3 with no code change.

Same arrangement as ``utils.release_watch``. Reads and writes log and carry on
rather than raising: a run that dies because its journal store hiccuped is
worse than one that plans without memory of the last run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class TargetEntry(BaseModel):
    """Per-target stats from a single run."""

    target: str
    matches_found: int = 0
    matches_submitted: int = 0
    completed: int = 0
    missing_scores: int = 0
    errors: int = 0
    action_taken: str = ""


class RunJournal(BaseModel):
    """Snapshot of the last agent run, persisted as JSON between runs."""

    timestamp: str  # ISO 8601 UTC
    run_id: str
    target: str | None = None
    dry_run: bool = False
    agent_summary: str = ""
    targets: list[TargetEntry] = []
    total_matches_found: int = 0
    total_matches_submitted: int = 0
    weekend_scores_status: str = ""
    missing_score_matches: list[str] = []


def read_journal(bucket: str, key: str, local_path: str = "") -> RunJournal | None:
    """
    Read the last run journal from S3, or from ``local_path`` when no bucket
    is set. Returns None if missing or invalid.
    """
    if not bucket:
        return _read_local(local_path)

    import boto3
    from botocore.exceptions import ClientError

    try:
        s3 = boto3.client("s3")
        response = s3.get_object(Bucket=bucket, Key=key)
        return RunJournal.model_validate_json(response["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchKey":
            logger.debug("journal.read_skipped", bucket=bucket, key=key, reason="not found")
        else:
            logger.debug("journal.read_skipped", bucket=bucket, key=key, reason=str(exc))
        return None
    except Exception as exc:
        logger.debug("journal.read_skipped", bucket=bucket, key=key, reason=str(exc))
        return None


def write_journal(bucket: str, key: str, journal: RunJournal, local_path: str = "") -> None:
    """
    Write the run journal to S3, or to ``local_path`` when no bucket is set.
    Never raises.
    """
    if not bucket:
        _write_local(local_path, journal)
        return

    import boto3

    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=journal.model_dump_json(indent=2),
            ContentType="application/json",
        )
        logger.info("journal.written", bucket=bucket, key=key)
    except Exception as exc:
        logger.warning("journal.write_failed", bucket=bucket, key=key, error=str(exc))


def _read_local(path: str) -> RunJournal | None:
    """Read the journal from a JSON file. None if absent or unreadable."""
    if not path:
        logger.warning(
            "journal.no_store",
            detail="no S3 bucket and no journal path — this run has no memory of the last",
        )
        return None

    try:
        return RunJournal.model_validate_json(Path(path).read_text())
    except FileNotFoundError:
        logger.debug("journal.read_skipped", path=path, reason="not found")
        return None
    except Exception as exc:
        logger.debug("journal.read_skipped", path=path, reason=str(exc))
        return None


def _write_local(path: str, journal: RunJournal) -> None:
    """
    Write the journal to a JSON file. Never raises.

    Writes to a sibling temp file and renames, so a pod killed mid-write leaves
    the previous journal intact rather than a truncated file that reads as no
    memory at all.
    """
    if not path:
        return

    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_text(journal.model_dump_json(indent=2))
        tmp.replace(target)
        logger.info("journal.written", path=path)
    except Exception as exc:
        logger.warning("journal.write_failed", path=path, error=str(exc))


def build_journal(
    *,
    run_id: str,
    result: Any,
    scraped_matches: list[dict[str, Any]],
    submission_errors: list[dict[str, str]],
    target: str | None,
    dry_run: bool,
    now: datetime | None = None,
) -> RunJournal:
    """Build a RunJournal from post-run data."""
    now = now or datetime.now(tz=UTC)

    # Per-target breakdown
    targets_map: dict[str, TargetEntry] = {}
    for m in scraped_matches:
        label = _target_label(m)
        entry = targets_map.setdefault(label, TargetEntry(target=label))
        entry.matches_found += 1
        if m.get("match_status") == "completed":
            entry.completed += 1
        elif _is_past(m.get("match_date", ""), now) and m.get("match_status") != "completed":
            entry.missing_scores += 1

    for err in submission_errors:
        label = err.get("target", "unknown")
        entry = targets_map.setdefault(label, TargetEntry(target=label))
        entry.errors += 1

    # Submitted counts come from the result totals, distribute proportionally
    # but for simplicity just set found counts and total submitted
    for entry in targets_map.values():
        entry.matches_submitted = entry.matches_found  # approximate per-target

    # Weekend scores status
    weekend_status = _weekend_status(now, scraped_matches)

    # Missing score match labels
    missing = [
        f"{m.get('match_date', '?')} {m.get('home_team', '?')} vs {m.get('away_team', '?')}"
        f" ({m.get('match_status', '?')})"
        for m in scraped_matches
        if _is_past(m.get("match_date", ""), now) and m.get("match_status") != "completed"
    ]

    return RunJournal(
        timestamp=now.isoformat(),
        run_id=run_id,
        target=target,
        dry_run=dry_run,
        agent_summary=result.summary,
        targets=list(targets_map.values()),
        total_matches_found=result.matches_found,
        total_matches_submitted=result.matches_submitted,
        weekend_scores_status=weekend_status,
        missing_score_matches=missing,
    )


def format_journal_log(journal: RunJournal | None) -> str:
    """Format a journal for structured log output."""
    if journal is None:
        return ""

    lines = [
        "--- Previous Run Journal ---",
        f"Last run: {journal.timestamp} (run_id: {journal.run_id})",
    ]
    if journal.target:
        lines.append(f"Target filter: {journal.target}")
    if journal.dry_run:
        lines.append("Mode: DRY RUN")

    lines.append(
        f"Totals: {journal.total_matches_found} found, {journal.total_matches_submitted} submitted"
    )

    for t in journal.targets:
        parts = [f"{t.matches_found} found"]
        if t.completed:
            parts.append(f"{t.completed} scored")
        if t.missing_scores:
            parts.append(f"{t.missing_scores} missing scores")
        if t.errors:
            parts.append(f"{t.errors} errors")
        lines.append(f"  {t.target}: {', '.join(parts)}")

    if journal.weekend_scores_status:
        lines.append(f"Weekend scores: {journal.weekend_scores_status}")

    if journal.missing_score_matches:
        lines.append("Missing scores:")
        for m in journal.missing_score_matches[:10]:  # cap to avoid bloat
            lines.append(f"  - {m}")
        if len(journal.missing_score_matches) > 10:
            lines.append(f"  ... and {len(journal.missing_score_matches) - 10} more")

    if journal.agent_summary:
        lines.append(f'Agent said: "{journal.agent_summary}"')

    lines.append("--- End Journal ---")
    return "\n".join(lines)


def _target_label(match: dict[str, Any]) -> str:
    """Derive a human-readable target label from a match dict."""
    ag = match.get("age_group", "?")
    league = match.get("league", "?")
    div = match.get("division", "?")
    league_short = "HG" if league == "Homegrown" else "Academy"
    return f"{ag} {league_short} {div}"


def _is_past(match_date: str, now: datetime) -> bool:
    """Check if a match date is in the past."""
    try:
        md = datetime.strptime(match_date, "%Y-%m-%d").date()
        return md < now.date()
    except (ValueError, TypeError):
        return False


def _weekend_status(now: datetime, matches: list[dict[str, Any]]) -> str:
    """Summarize weekend score status."""
    today = now.date()
    day = today.weekday()
    days_since_sunday = (day + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    last_saturday = last_sunday - timedelta(days=1)

    weekend = [m for m in matches if _match_date(m) in (last_saturday, last_sunday)]
    if not weekend:
        return "no weekend matches"

    scored = sum(1 for m in weekend if m.get("match_status") == "completed")
    unscored = len(weekend) - scored
    if unscored == 0:
        return "all posted"
    return f"{unscored} awaiting scores"


def _match_date(match: dict[str, Any]) -> Any:
    """Parse match_date to a date object, or return None."""
    try:
        return datetime.strptime(match.get("match_date", ""), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
