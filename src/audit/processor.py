"""Audit event processor — resubmits corrections to RabbitMQ and marks events done."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from agent.tools import _current_season
from audit.client import cancel_match, fetch_pending_events, mark_event_processed
from audit.models import ProcessResult

if TYPE_CHECKING:
    from config.settings import AgentSettings

logger = structlog.get_logger()

# Finding types that trigger re-submission to RabbitMQ (scraped_match required)
_RESUBMIT_TYPES = frozenset(
    {"missing_in_mt", "score_mismatch", "status_mismatch", "time_mismatch"}
)

# extra_in_mt findings older than this are cancelled in MT; newer ones are left alone
# (matches get rescheduled and mlssoccer.com can lag — give it a full week)
_CANCEL_THRESHOLD_DAYS = 7


async def process_pending_events(
    settings: AgentSettings,
    queue_client: Any,
    dry_run: bool,
) -> ProcessResult:
    """Fetch pending audit events and process each finding.

    - missing_in_mt / score_mismatch / status_mismatch / time_mismatch:
        submit scraped_match to RabbitMQ for MT upsert.
    - extra_in_mt (> 7 days old, no score): cancel in MT automatically.
    - extra_in_mt (< 7 days old): skip — may be a reschedule in progress.

    Returns:
        ProcessResult with counts of actions taken.
    """
    season = _current_season()
    events = await fetch_pending_events(
        api_url=settings.missing_table_api_url,
        api_key=settings.missing_table_api_key or "",
        season=season,
    )

    events_processed = 0
    matches_resubmitted = 0
    extra_in_mt_cancelled = 0
    extra_in_mt_skipped = 0
    errors = 0
    cancel_threshold = date.today() - timedelta(days=_CANCEL_THRESHOLD_DAYS)

    for event in events:
        try:
            for finding in event.findings:
                if finding.finding_type in _RESUBMIT_TYPES:
                    if finding.scraped_match:
                        if not dry_run:
                            queue_client.submit_match(finding.scraped_match)
                        matches_resubmitted += 1
                        logger.info(
                            "audit.processor.resubmit",
                            finding_type=finding.finding_type,
                            match=f"{finding.home_team} vs {finding.away_team}",
                            match_date=finding.match_date,
                            dry_run=dry_run,
                        )
                    else:
                        logger.warning(
                            "audit.processor.no_scraped_match",
                            finding_type=finding.finding_type,
                            match=f"{finding.home_team} vs {finding.away_team}",
                        )
                elif finding.finding_type == "extra_in_mt":
                    try:
                        match_date = date.fromisoformat(finding.match_date)
                    except ValueError:
                        logger.warning(
                            "audit.processor.extra_in_mt.bad_date",
                            match_date=finding.match_date,
                        )
                        extra_in_mt_skipped += 1
                        continue

                    if match_date < cancel_threshold:
                        if not dry_run:
                            await cancel_match(
                                api_url=settings.missing_table_api_url,
                                api_key=settings.missing_table_api_key or "",
                                home_team=finding.home_team,
                                away_team=finding.away_team,
                                match_date=finding.match_date,
                                age_group=event.age_group,
                                league=event.league,
                                division=event.division,
                                season=event.season,
                            )
                        extra_in_mt_cancelled += 1
                        logger.info(
                            "audit.processor.extra_in_mt.cancelled",
                            home_team=finding.home_team,
                            away_team=finding.away_team,
                            match_date=finding.match_date,
                            dry_run=dry_run,
                        )
                    else:
                        extra_in_mt_skipped += 1
                        logger.info(
                            "audit.processor.extra_in_mt.skipped",
                            home_team=finding.home_team,
                            away_team=finding.away_team,
                            match_date=finding.match_date,
                            reason="within_7_day_window",
                        )

            if not dry_run:
                await mark_event_processed(
                    api_url=settings.missing_table_api_url,
                    api_key=settings.missing_table_api_key or "",
                    event_id=event.event_id,
                )
            events_processed += 1

        except Exception as exc:
            errors += 1
            logger.error(
                "audit.processor.event_error",
                event_id=event.event_id,
                team=event.team,
                error=str(exc),
            )

    logger.info(
        "audit.processor.done",
        events_processed=events_processed,
        matches_resubmitted=matches_resubmitted,
        extra_in_mt_cancelled=extra_in_mt_cancelled,
        extra_in_mt_skipped=extra_in_mt_skipped,
        errors=errors,
    )

    return ProcessResult(
        events_processed=events_processed,
        matches_resubmitted=matches_resubmitted,
        extra_in_mt_cancelled=extra_in_mt_cancelled,
        extra_in_mt_skipped=extra_in_mt_skipped,
        errors=errors,
    )
