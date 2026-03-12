"""Run summary report builder for Telegram notifications."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram_notify import escape

# K3s CronJob schedule hours (UTC)
_CRON_HOURS = [2, 8, 14, 20]

# Display timezone for reports
_DISPLAY_TZ = ZoneInfo("America/New_York")


def build_report(
    *,
    result_summary: str,
    actions: list[dict[str, Any]],
    matches_found: int,
    matches_submitted: int,
    scraped_matches: list[dict[str, Any]],
    submission_errors: list[dict[str, str]],
    env: str,
    target: str | None,
    dry_run: bool,
    mt_status: str = "",
    now: datetime | None = None,
) -> str:
    """Build a MarkdownV2-formatted run summary report.

    Args:
        result_summary: Agent's summary string.
        actions: List of AgentAction dicts (action, detail, dry_run).
        matches_found: Total matches found by scraper.
        matches_submitted: Total matches submitted to queue.
        scraped_matches: Raw match dicts from AgentDeps._scraped_matches.
        submission_errors: Error dicts from AgentDeps._submission_errors.
        env: Environment name (local, prod).
        target: Target filter name or None.
        dry_run: Whether this was a dry run.
        now: Override current time (for testing). Defaults to UTC now.

    Returns:
        MarkdownV2-formatted report string ready for Telegram.
    """
    now = now or datetime.now(tz=UTC)
    lines: list[str] = []

    # --- Header ---
    lines.append("*Match Scraper Report*")
    local = now.astimezone(_DISPLAY_TZ)
    tz_abbr = local.strftime("%Z")  # EDT or EST
    ts = escape(local.strftime(f"%Y-%m-%d %H:%M {tz_abbr}"))
    target_label = escape(target) if target else "all targets"
    header_parts = [ts, escape(env)]
    if target:
        header_parts.append(target_label)
    if dry_run:
        header_parts.append("DRY RUN")
    lines.append(f"_{' · '.join(header_parts)}_")
    lines.append("")

    # --- Agent Awareness ---
    awareness = _agent_awareness(now, scraped_matches)
    lines.append(awareness)

    # --- MT Status ---
    if mt_status.startswith("failed:"):
        reason = escape(mt_status.removeprefix("failed:"))
        lines.append(escape("⚠️ MT status check FAILED — full-season scrape fallback"))
        lines.append(f"  _{reason}_")
    elif mt_status == "ok":
        lines.append(escape("📡 MT status: OK — smart scrape"))
    else:
        lines.append(escape("📡 MT status: not checked (targeted run)"))
    lines.append("")

    # --- Actions Taken ---
    for action in actions:
        prefix = escape("[DRY RUN] ") if action.get("dry_run") else ""
        icon = _action_icon(action.get("action", ""))
        detail = escape(action.get("detail", ""))
        lines.append(f"{icon} {prefix}{detail}")

    if not actions:
        lines.append(escape("No actions taken."))
    lines.append("")

    # --- Match Summary ---
    completed = sum(1 for m in scraped_matches if m.get("match_status") == "completed")
    scheduled = sum(1 for m in scraped_matches if m.get("match_status") == "scheduled")
    tbd = sum(1 for m in scraped_matches if m.get("match_status") == "tbd")
    error_count = len(submission_errors)

    summary_parts = [
        escape(f"{matches_found} found"),
        escape(f"{matches_submitted} submitted"),
    ]
    if error_count:
        summary_parts.append(escape(f"{error_count} errors"))
    lines.append(f"*Matches:* {' · '.join(summary_parts)}")

    status_parts = []
    if completed:
        status_parts.append(escape(f"{completed} completed"))
    if scheduled:
        status_parts.append(escape(f"{scheduled} scheduled"))
    if tbd:
        status_parts.append(escape(f"{tbd} tbd"))
    if status_parts:
        lines.append(f"  {' · '.join(status_parts)}")
    lines.append("")

    # --- Submission Errors ---
    if submission_errors:
        lines.append(f"*Submission Errors \\({escape(str(error_count))}\\):*")
        for err in submission_errors:
            match = escape(err.get("match", "unknown"))
            error = escape(err.get("error", "unknown"))
            lines.append(f"  • {match} — {error}")
        lines.append("")

    # --- Weekend Scores ---
    scored_lines = _weekend_scores_section(now, scraped_matches)
    if scored_lines:
        lines.extend(scored_lines)
        lines.append("")

    # --- Missing Scores ---
    missing_lines = _missing_scores_section(now, scraped_matches)
    if missing_lines:
        lines.extend(missing_lines)
        lines.append("")

    # --- Next Run ---
    next_run, delta = _next_scheduled_run(now)
    next_local = next_run.astimezone(_DISPLAY_TZ)
    next_tz = next_local.strftime("%Z")
    next_str = escape(next_local.strftime(f"%H:%M {next_tz}"))
    delta_str = _format_delta(delta)
    lines.append(f"*Next run:* {next_str} \\(in {escape(delta_str)}\\)")

    return "\n".join(lines)


def _agent_awareness(now: datetime, matches: list[dict[str, Any]]) -> str:
    """Generate context-aware message based on day of week and match data."""
    day = now.weekday()  # 0=Mon, 5=Sat, 6=Sun

    if day in (5, 6):  # Sat/Sun
        return escape("🧠 Active match day — scores may still be posting")

    if day in (0, 1, 2):  # Mon/Tue/Wed
        # Check for unscored weekend matches
        weekend_unscored = [
            m
            for m in matches
            if _is_last_weekend(m.get("match_date", ""), now)
            and m.get("match_status") != "completed"
        ]
        if weekend_unscored:
            n = len(weekend_unscored)
            return escape(f"🧠 Mid-week — {n} weekend match(es) still awaiting scores")
        # Check if there were any weekend matches at all
        weekend_all = [m for m in matches if _is_last_weekend(m.get("match_date", ""), now)]
        if weekend_all:
            return escape("🧠 Mid-week — all weekend scores are posted ✓")
        return escape("🧠 Mid-week — no recent weekend matches found")

    # Thu/Fri
    return escape("🧠 No recent match activity — routine schedule sync")


def _is_last_weekend(match_date: str, now: datetime) -> bool:
    """Check if a match date falls on the most recent Saturday or Sunday."""
    try:
        md = datetime.strptime(match_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False

    today = now.date()
    day = today.weekday()  # 0=Mon

    # Find last Saturday and Sunday
    days_since_sunday = (day + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    last_saturday = last_sunday - timedelta(days=1)

    return md in (last_saturday, last_sunday)


def _weekend_scores_section(now: datetime, matches: list[dict[str, Any]]) -> list[str]:
    """Show completed weekend match scores (Mon-Wed only)."""
    day = now.weekday()
    if day not in (0, 1, 2):  # Only Mon/Tue/Wed
        return []

    scored = [
        m
        for m in matches
        if _is_last_weekend(m.get("match_date", ""), now) and m.get("match_status") == "completed"
    ]
    if not scored:
        return []

    lines: list[str] = []
    n = len(scored)
    lines.append(f"*Weekend Scores \\({escape(str(n))}\\):*")
    for m in sorted(scored, key=lambda x: x.get("match_date", "")):
        home = escape(m.get("home_team", "?"))
        away = escape(m.get("away_team", "?"))
        hs = m.get("home_score", "?")
        aws = m.get("away_score", "?")
        md = escape(m.get("match_date", "?"))
        lines.append(f"  • {md} {home} {hs}\\-{aws} {away}")
    return lines


def _missing_scores_section(now: datetime, matches: list[dict[str, Any]]) -> list[str]:
    """Build the missing scores section if there are unscored matches to highlight."""
    today_str = now.strftime("%Y-%m-%d")
    day = now.weekday()
    lines: list[str] = []

    # Today's unscored matches (Sat/Sun primarily, but show any day)
    today_unscored = [
        m
        for m in matches
        if m.get("match_date") == today_str and m.get("match_status") != "completed"
    ]
    if today_unscored:
        n = len(today_unscored)
        lines.append(f"*Today's Matches Awaiting Scores \\({escape(str(n))}\\):*")
        for m in today_unscored:
            home = escape(m.get("home_team", "?"))
            away = escape(m.get("away_team", "?"))
            status = escape(m.get("match_status", "?"))
            lines.append(f"  • {home} vs {away} — {status}")

    # Mon-Wed: also show unscored weekend matches
    if day in (0, 1, 2):
        weekend_unscored = [
            m
            for m in matches
            if _is_last_weekend(m.get("match_date", ""), now)
            and m.get("match_status") != "completed"
        ]
        if weekend_unscored:
            n = len(weekend_unscored)
            if lines:
                lines.append("")
            lines.append(f"*Weekend Matches Awaiting Scores \\({escape(str(n))}\\):*")
            for m in weekend_unscored:
                home = escape(m.get("home_team", "?"))
                away = escape(m.get("away_team", "?"))
                status = escape(m.get("match_status", "?"))
                md = escape(m.get("match_date", "?"))
                lines.append(f"  • {md} {home} vs {away} — {status}")

    return lines


def _next_scheduled_run(now: datetime) -> tuple[datetime, timedelta]:
    """Compute the next scheduled run time from the cron schedule."""
    for hour in _CRON_HOURS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate, candidate - now

    # Wrap to first slot tomorrow
    tomorrow = now + timedelta(days=1)
    candidate = tomorrow.replace(hour=_CRON_HOURS[0], minute=0, second=0, microsecond=0)
    return candidate, candidate - now


def _format_delta(delta: timedelta) -> str:
    """Format a timedelta as 'Xh Ym'."""
    total_minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _action_icon(action: str) -> str:
    """Map action type to an emoji icon."""
    return {"scrape": "✅", "submit": "✅", "skip": "⏭️"}.get(action, "•")
