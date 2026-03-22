"""Typer CLI for match-scraper-agent."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Any

import structlog
import typer

if TYPE_CHECKING:
    from config.settings import AgentSettings

app = typer.Typer(name="match-scraper-agent", no_args_is_help=True)
logger = structlog.get_logger()


# Target → scraper config (age_group, league, division, conference, club)
_TARGET_SCRAPER_CONFIG: dict[str, dict[str, str]] = {
    "u14-hg": {
        "age_group": "U14",
        "league": "Homegrown",
        "division": "Northeast",
    },
    "u14-hg-ifa": {
        "age_group": "U14",
        "league": "Homegrown",
        "division": "Northeast",
    },
    "u13-hg": {
        "age_group": "U13",
        "league": "Homegrown",
        "division": "Northeast",
    },
    "u13-hg-ifa": {
        "age_group": "U13",
        "league": "Homegrown",
        "division": "Northeast",
    },
    "u14-academy": {
        "age_group": "U14",
        "league": "Academy",
        "conference": "New England",
    },
    "u14-academy-ifa": {
        "age_group": "U14",
        "league": "Academy",
        "conference": "New England",
    },
    "u14-hg-florida": {
        "age_group": "U14",
        "league": "Homegrown",
        "division": "Florida",
    },
    "u13-hg-florida": {
        "age_group": "U13",
        "league": "Homegrown",
        "division": "Florida",
    },
    "u15-hg": {
        "age_group": "U15",
        "league": "Homegrown",
        "division": "Northeast",
    },
    "u15-hg-ifa": {
        "age_group": "U15",
        "league": "Homegrown",
        "division": "Northeast",
    },
    "u16-hg": {
        "age_group": "U16",
        "league": "Homegrown",
        "division": "Northeast",
    },
    "u16-hg-ifa": {
        "age_group": "U16",
        "league": "Homegrown",
        "division": "Northeast",
    },
}

# Targets that include a team filter — value is the DB team name used for filtering
_TARGET_TEAM_FILTER: dict[str, str] = {
    "u14-hg-ifa": "IFA",
    "u13-hg-ifa": "IFA",
    "u15-hg-ifa": "IFA",
    "u16-hg-ifa": "IFA",
    "u14-academy-ifa": "IFA Academy",
}


def _queue_client_kwargs(settings: AgentSettings) -> dict[str, str]:
    """Build kwargs for MatchQueueClient based on settings."""
    kwargs: dict[str, str] = {"broker_url": settings.rabbitmq_url}
    if settings.queue_name:
        kwargs["queue_name"] = settings.queue_name
    else:
        kwargs["exchange_name"] = settings.exchange_name
    return kwargs


def _send_telegram_report(
    settings: AgentSettings,
    result: Any,
    ctx: Any,
    env: str,
    target: str | None,
) -> None:
    """Build and send the run summary report to Telegram."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("telegram.skipped", reason="not configured")
        return

    from telegram_notify import TelegramClient

    from utils.report import build_report

    report = build_report(
        result_summary=result.summary,
        actions=[a.model_dump() for a in result.actions],
        matches_found=result.matches_found,
        matches_submitted=result.matches_submitted,
        scraped_matches=ctx._scraped_matches,
        submission_errors=ctx._submission_errors,
        env=env,
        target=target,
        dry_run=ctx.dry_run,
        mt_status=ctx._mt_status,
        scrape_plan=ctx._scrape_plan,
    )

    try:
        client = TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        client.send(report)
        logger.info("telegram.sent", chat_id=settings.telegram_chat_id)
    except Exception as exc:
        logger.warning("telegram.failed", error=str(exc))


@app.command()
def run(
    env: Annotated[str, typer.Option("--env", help="Environment name (local, prod)")] = "local",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip mutating operations")] = False,
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Output JSON log lines")] = False,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Scrape only this target (u14-hg, u13-hg, u14-academy)"),
    ] = None,
) -> None:
    """Run the match-scraper pipeline engine."""
    import asyncio

    from src.celery.queue_client import MatchQueueClient

    from agent.deps import RunContext
    from config.settings import AgentSettings, env_file_path
    from utils.logger import configure_logging

    settings = AgentSettings(_env_file=env_file_path(env))
    if dry_run:
        settings.dry_run = True

    configure_logging(json_output=json_logs or settings.json_logs, log_level=settings.log_level)

    run_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=run_id, env=env)

    logger.info(
        "engine.starting",
        dry_run=settings.dry_run,
    )

    try:
        queue_client = MatchQueueClient(**_queue_client_kwargs(settings))

        if target and target not in _TARGET_SCRAPER_CONFIG:
            valid = ", ".join(sorted(_TARGET_SCRAPER_CONFIG))
            typer.echo(f"Unknown target '{target}'. Valid targets: {valid}", err=True)
            raise typer.Exit(code=1)

        team_filter = _TARGET_TEAM_FILTER.get(target or "", "")
        ctx = RunContext(
            queue_client=queue_client,
            missing_table_api_url=settings.missing_table_api_url,
            missing_table_api_key=settings.missing_table_api_key or "",
            dry_run=settings.dry_run,
            headless=settings.headless,
            team_filter=team_filter,
            age_group=settings.age_group,
            league=settings.league,
            division=settings.division,
        )

        if target:
            # Targeted run — build a single-entry RunPlan
            from datetime import date

            from agent.planner import RunPlan, ScrapeAction, ScrapePlan
            from agent.tools import SEASON_END

            target_cfg = _TARGET_SCRAPER_CONFIG[target]
            label = _target_label(target_cfg)
            plan = RunPlan(
                plans=[
                    ScrapePlan(
                        target_key=target,
                        target_label=label,
                        action=ScrapeAction.FULL_SYNC,
                        start_date=date.today(),
                        end_date=SEASON_END,
                        reason="Targeted run",
                        scraper_params=target_cfg,
                    )
                ],
            )
            ctx._scrape_plan = plan
            logger.info("engine.target_run", target=target, team_filter=team_filter or None)
            journal = None
        else:
            # Full run — compute scrape plan from MT data
            from datetime import UTC, datetime

            from agent.planner import (
                compute_scrape_plan,
                fetch_mt_status,
                is_weekly_sync_run,
            )
            from agent.tools import SEASON_END, _current_season

            now_utc = datetime.now(tz=UTC)
            mt_targets, mt_status_str = fetch_mt_status(
                api_url=settings.missing_table_api_url,
                api_key=settings.missing_table_api_key or "",
                season=_current_season(),
            )
            ctx._mt_status = mt_status_str

            if mt_status_str.startswith("failed:"):
                logger.error(
                    "engine.mt_api_failed",
                    mt_status=mt_status_str,
                    reason="Cannot plan without MT data — halting run",
                )
                raise typer.Exit(code=1)

            weekly = is_weekly_sync_run(now_utc)
            plan = compute_scrape_plan(
                mt_targets=mt_targets,
                target_configs=_TARGET_SCRAPER_CONFIG,
                today=now_utc.date(),
                season_end=SEASON_END,
                is_weekly=weekly,
            )
            ctx._scrape_plan = plan

            logger.info(
                "planner.computed",
                weekly=weekly,
                mt_status=mt_status_str,
                full_sync=sum(1 for p in plan.plans if p.action.value == "full_sync"),
                score_sync=sum(1 for p in plan.plans if p.action.value == "score_sync"),
                skip=sum(1 for p in plan.plans if p.action.value == "skip"),
            )

            # Read previous journal for modifier rules
            journal = None
            if settings.journal_path:
                from pathlib import Path

                from utils.journal import read_journal

                journal = read_journal(Path(settings.journal_path))
                if journal:
                    logger.info("journal.loaded", run_id=journal.run_id)

        # Run the pipeline engine
        from agent.engine import run_pipeline

        result = asyncio.run(run_pipeline(plan, ctx, journal))

    except typer.Exit:
        raise
    except Exception as exc:
        logger.error("engine.failed", error=str(exc), exc_info=exc)
        raise typer.Exit(code=1) from None

    logger.info(
        "engine.completed",
        summary=result.summary,
        actions=len(result.actions),
        matches_found=result.matches_found,
        matches_submitted=result.matches_submitted,
    )

    if json_logs or settings.json_logs:
        print(result.model_dump_json(indent=2))
    else:
        typer.echo(f"\n{result.summary}")
        for action in result.actions:
            prefix = "[DRY RUN] " if action.dry_run else ""
            typer.echo(f"  {prefix}{action.action}: {action.detail}")

    # Write run journal for next run's context
    if settings.journal_path:
        from pathlib import Path

        from utils.journal import build_journal, write_journal

        journal_out = build_journal(
            run_id=run_id,
            result=result,
            scraped_matches=ctx._scraped_matches,
            submission_errors=ctx._submission_errors,
            target=target,
            dry_run=settings.dry_run,
        )
        write_journal(Path(settings.journal_path), journal_out)

    # Send Telegram summary report
    _send_telegram_report(
        settings=settings,
        result=result,
        ctx=ctx,
        env=env,
        target=target,
    )

    structlog.contextvars.unbind_contextvars("run_id", "env")


def _target_label(cfg: dict[str, str]) -> str:
    """Build a human-readable label from a scraper config dict."""
    ag = cfg.get("age_group", "?")
    league = cfg.get("league", "?")
    if cfg.get("conference"):
        return f"{ag} {league} {cfg['conference']}"
    return f"{ag} {league} {cfg.get('division', '?')}"


@app.command()
def check(
    env: Annotated[str, typer.Option("--env", help="Environment name (local, prod)")] = "local",
) -> None:
    """Check RabbitMQ connectivity."""
    from config.settings import AgentSettings, env_file_path
    from utils.logger import configure_logging

    configure_logging(json_output=False)

    settings = AgentSettings(_env_file=env_file_path(env))

    typer.echo(f"environment: {env}")

    # Check RabbitMQ
    typer.echo(f"rabbitmq: checking {settings.rabbitmq_url}")
    try:
        from src.celery.queue_client import MatchQueueClient

        client = MatchQueueClient(**_queue_client_kwargs(settings))
        if client.check_connection():
            typer.echo("  status: connected")
        else:
            typer.echo("  status: UNREACHABLE")
    except Exception as exc:
        typer.echo(f"  status: ERROR ({exc})")


@app.command()
def scrape(
    target: Annotated[
        str,
        typer.Option("--target", help="Scrape target (u14-hg, u14-hg-ifa, u13-hg, etc.)"),
    ],
    env: Annotated[str, typer.Option("--env", help="Environment name (local, prod)")] = "local",
    json_output: Annotated[
        bool, typer.Option("--json", help="Output raw match dicts as JSON")
    ] = False,
    from_date: Annotated[
        str | None,
        typer.Option("--from", help="Start date (YYYY-MM-DD). Defaults to today."),
    ] = None,
    to_date: Annotated[
        str | None,
        typer.Option("--to", help="End date (YYYY-MM-DD). Defaults to season end (2026-06-30)."),
    ] = None,
    submit: Annotated[
        bool,
        typer.Option("--submit", help="Submit scraped matches to RabbitMQ queue"),
    ] = False,
    club: Annotated[
        str | None,
        typer.Option("--club", help="Filter to a specific club name"),
    ] = None,
) -> None:
    """Scrape matches directly — no pipeline, just browser automation."""
    import asyncio
    from datetime import date

    from src.scraper.config import ScrapingConfig
    from src.scraper.mls_scraper import MLSScraper

    from agent.tools import (
        SEASON_END,
        _current_season,
        _normalize_team_name,
    )
    from config.settings import AgentSettings, env_file_path
    from utils.logger import configure_logging

    configure_logging(json_output=False)

    if target not in _TARGET_SCRAPER_CONFIG:
        valid = ", ".join(sorted(_TARGET_SCRAPER_CONFIG))
        typer.echo(f"Unknown target '{target}'. Valid targets: {valid}", err=True)
        raise typer.Exit(code=1)

    settings = AgentSettings(_env_file=env_file_path(env))
    target_cfg = _TARGET_SCRAPER_CONFIG[target]
    team_filter = _TARGET_TEAM_FILTER.get(target, "")

    try:
        start = date.fromisoformat(from_date) if from_date else date.today()
        end = date.fromisoformat(to_date) if to_date else SEASON_END
    except ValueError as exc:
        typer.echo(f"Invalid date format: {exc}. Use YYYY-MM-DD.", err=True)
        raise typer.Exit(code=1) from None

    config = ScrapingConfig(
        age_group=target_cfg.get("age_group", settings.age_group),
        league=target_cfg.get("league", settings.league),
        division=target_cfg.get("division", settings.division),
        conference=target_cfg.get("conference", ""),
        club=club or "",
        start_date=start,
        end_date=end,
        look_back_days=(end - start).days,
        missing_table_api_url=settings.missing_table_api_url,
        missing_table_api_key=settings.missing_table_api_key or "unused",
    )

    label = f"{config.age_group} {config.league}"
    if config.conference:
        label += f" {config.conference}"
    elif config.division:
        label += f" {config.division}"
    typer.echo(f"Scraping {label} ({start} to {end})...")

    scraper = MLSScraper(config, headless=True)
    matches = asyncio.run(scraper.scrape_matches())

    if not matches:
        typer.echo("No matches found.")
        raise typer.Exit(code=0)

    # Build match dicts (same logic as the engine tool)
    mt_division = config.conference if config.conference else config.division
    built = [
        {
            "home_team": _normalize_team_name(m.home_team, league=config.league),
            "away_team": _normalize_team_name(m.away_team, league=config.league),
            "match_date": m.match_datetime.date().isoformat(),
            "match_time": m.match_datetime.strftime("%H:%M")
            if m.match_datetime.hour or m.match_datetime.minute
            else None,
            "season": _current_season(),
            "age_group": config.age_group,
            "match_type": "League",
            "division": mt_division,
            "league": config.league,
            "home_score": m.home_score if isinstance(m.home_score, int) else None,
            "away_score": m.away_score if isinstance(m.away_score, int) else None,
            "match_status": m.match_status,
            "external_match_id": m.match_id,
            "location": m.location,
            "source": "match-scraper-agent",
        }
        for m in matches
    ]

    # Apply team filter
    if team_filter:
        built = [m for m in built if team_filter in (m["home_team"], m["away_team"])]

    if json_output:
        import json

        print(json.dumps(built, indent=2))
    else:
        typer.echo(f"\nFound {len(matches)} matches ({len(built)} after filtering):\n")
        for m in built:
            has_score = m["home_score"] is not None
            score = f" ({m['home_score']}-{m['away_score']})" if has_score else ""
            typer.echo(
                f"  {m['match_date']} | {m['home_team']} vs {m['away_team']}"
                f"{score} [{m['match_status']}]"
            )

    if submit:
        from src.celery.queue_client import MatchQueueClient

        queue_client = MatchQueueClient(**_queue_client_kwargs(settings))
        submitted = 0
        errors = 0
        for match_dict in built:
            try:
                queue_client.submit_match(match_dict)
                submitted += 1
            except Exception as exc:
                errors += 1
                logger.warning(
                    "scrape.submit_error",
                    match=f"{match_dict['home_team']} vs {match_dict['away_team']}",
                    error=str(exc),
                )
        typer.echo(f"\nSubmitted {submitted} matches to queue ({errors} errors).")


def _send_telegram_audit_report(
    settings: AgentSettings,
    result: Any,
    env: str,
) -> None:
    """Send audit findings report to Telegram. Silently skips if not configured."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("telegram.skipped", reason="not configured")
        return

    from telegram_notify import TelegramClient

    from audit.report import build_audit_report

    report = build_audit_report(result=result, env=env)
    try:
        client = TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        client.send(report)
        logger.info("telegram.sent", chat_id=settings.telegram_chat_id)
    except Exception as exc:
        logger.warning("telegram.failed", error=str(exc))


def _send_telegram_processor_report(
    settings: AgentSettings,
    result: Any,
    env: str,
) -> None:
    """Send processor summary report to Telegram. Silently skips if not configured."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("telegram.skipped", reason="not configured")
        return

    from telegram_notify import TelegramClient

    from audit.report import build_processor_report

    report = build_processor_report(result=result, env=env)
    try:
        client = TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        client.send(report)
        logger.info("telegram.sent", chat_id=settings.telegram_chat_id)
    except Exception as exc:
        logger.warning("telegram.failed", error=str(exc))


@app.command()
def audit(
    env: Annotated[str, typer.Option("--env", help="Environment name (local, prod)")] = "local",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip mutating operations")] = False,
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Output JSON log lines")] = False,
    team: Annotated[str | None, typer.Option("--team", help="Audit a specific team (skips rotation). Requires --age-group.")] = None,
    age_group: Annotated[str | None, typer.Option("--age-group", help="Age group for --team override (e.g. U14)")] = None,
) -> None:
    """Audit one team against mlssoccer.com source of truth."""
    import asyncio
    from datetime import date

    from audit.runner import run_one_team_audit
    from config.settings import AgentSettings, env_file_path
    from utils.logger import configure_logging

    settings = AgentSettings(_env_file=env_file_path(env))
    configure_logging(json_output=json_logs or settings.json_logs, log_level=settings.log_level)

    run_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=run_id, env=env)

    try:
        season_start = date.fromisoformat(settings.audit_season_start)
    except ValueError:
        typer.echo(f"Invalid AGENT_AUDIT_SEASON_START: {settings.audit_season_start}", err=True)
        raise typer.Exit(code=1) from None

    from agent.tools import SEASON_END

    if bool(team) != bool(age_group):
        typer.echo("--team and --age-group must be used together", err=True)
        raise typer.Exit(code=1)

    try:
        result = asyncio.run(
            run_one_team_audit(
                settings=settings,
                dry_run=dry_run,
                season_start=season_start,
                season_end=SEASON_END,
                team_override=team,
                age_group_override=age_group,
            )
        )
    except Exception as exc:
        logger.error("audit.failed", error=str(exc), exc_info=exc)
        raise typer.Exit(code=1) from None

    if result is None:
        logger.info("audit.all_teams_current")
        typer.echo("All teams are up-to-date. Nothing to audit.")
    else:
        logger.info(
            "audit.completed",
            team=result.team,
            age_group=result.age_group,
            scraped=result.scraped_count,
            mt=result.mt_count,
            findings=len(result.findings),
        )
        typer.echo(
            f"Audited {result.team} {result.age_group}: "
            f"{result.scraped_count} scraped, {result.mt_count} in MT, "
            f"{len(result.findings)} findings"
        )
        if result.findings:
            _send_telegram_audit_report(settings=settings, result=result, env=env)

    structlog.contextvars.unbind_contextvars("run_id", "env")


@app.command(name="audit-process")
def audit_process(
    env: Annotated[str, typer.Option("--env", help="Environment name (local, prod)")] = "local",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip mutating operations")] = False,
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Output JSON log lines")] = False,
) -> None:
    """Process pending audit findings — resubmit corrections to RabbitMQ."""
    import asyncio

    from src.celery.queue_client import MatchQueueClient

    from audit.processor import process_pending_events
    from config.settings import AgentSettings, env_file_path
    from utils.logger import configure_logging

    settings = AgentSettings(_env_file=env_file_path(env))
    configure_logging(json_output=json_logs or settings.json_logs, log_level=settings.log_level)

    run_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=run_id, env=env)

    queue_client = MatchQueueClient(**_queue_client_kwargs(settings))

    try:
        result = asyncio.run(
            process_pending_events(
                settings=settings,
                queue_client=queue_client,
                dry_run=dry_run,
            )
        )
    except Exception as exc:
        logger.error("audit_process.failed", error=str(exc), exc_info=exc)
        raise typer.Exit(code=1) from None

    logger.info(
        "audit_process.completed",
        events_processed=result.events_processed,
        matches_resubmitted=result.matches_resubmitted,
        extra_in_mt_cancelled=result.extra_in_mt_cancelled,
        extra_in_mt_skipped=result.extra_in_mt_skipped,
        errors=result.errors,
    )

    if result.matches_resubmitted or result.extra_in_mt_cancelled:
        _send_telegram_processor_report(settings=settings, result=result, env=env)

    structlog.contextvars.unbind_contextvars("run_id", "env")
