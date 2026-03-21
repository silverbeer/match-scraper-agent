"""Telegram MarkdownV2 report builders for audit commands."""

from __future__ import annotations

from telegram_notify import escape

from audit.models import AuditRunResult, ProcessResult


def build_audit_report(result: AuditRunResult, env: str) -> str:
    """Build a MarkdownV2 audit report for Telegram.

    Only called when findings exist — clean runs are silent.
    """
    lines: list[str] = []
    lines.append("*Audit Report*")

    team_label = escape(f"{result.team} {result.age_group}")
    env_label = escape(env)
    dry_suffix = escape(" · DRY RUN") if result.dry_run else ""
    lines.append(f"_{team_label} · {env_label}{dry_suffix}_")
    lines.append("")

    lines.append(
        f"*Scraped:* {escape(str(result.scraped_count))} · "
        f"*MT:* {escape(str(result.mt_count))} · "
        f"*Findings:* {escape(str(len(result.findings)))}"
    )
    lines.append("")

    # Group findings by type
    by_type: dict[str, list] = {}
    for f in result.findings:
        by_type.setdefault(f.finding_type, []).append(f)

    for ftype, flist in sorted(by_type.items()):
        type_label = escape(ftype.replace("_", " ").title())
        count_label = escape(str(len(flist)))
        lines.append(f"*{type_label} \\({count_label}\\):*")
        for f in flist:
            home = escape(f.home_team)
            away = escape(f.away_team)
            md = escape(f.match_date)
            detail = ""
            if f.field:
                scraped_val = escape(f.scraped_value or "?")
                mt_val = escape(f.mt_value or "?")
                field_label = escape(f.field)
                detail = f" — {field_label}: {scraped_val} vs {mt_val}"
            lines.append(f"  • {md} {home} vs {away}{detail}")
        lines.append("")

    return "\n".join(lines)


def build_processor_report(result: ProcessResult, env: str) -> str:
    """Build a MarkdownV2 processor report for Telegram.

    Only called when matches were resubmitted or extra_in_mt were acted on.
    """
    lines: list[str] = []
    lines.append("*Audit Processor Report*")
    lines.append(f"_{escape(env)}_")
    lines.append("")

    parts = [
        f"*Corrected:* {escape(str(result.matches_resubmitted))}",
        f"*Cancelled:* {escape(str(result.extra_in_mt_cancelled))}",
    ]
    if result.extra_in_mt_skipped:
        parts.append(f"*Pending \\(< 7d\\):* {escape(str(result.extra_in_mt_skipped))}")
    lines.append(" · ".join(parts))

    if result.errors:
        lines.append(f"*Errors:* {escape(str(result.errors))}")

    return "\n".join(lines)
