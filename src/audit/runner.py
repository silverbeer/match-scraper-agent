"""One-team audit orchestrator — fetches next team, scrapes, compares, submits findings."""

from __future__ import annotations

import uuid
from datetime import date

import structlog

from agent.tools import TEAM_NAME_MAP, _current_season, _normalize_team_name
from audit.client import fetch_mt_matches, fetch_next_team, submit_audit_event
from audit.comparator import compare_matches
from audit.models import AuditEvent, AuditRunResult
from config.settings import AgentSettings

logger = structlog.get_logger()

# Reverse map: MT canonical name → MLS Next display name (used for scraper club= filter)
MT_NAME_TO_MLS_NAME: dict[str, str] = {v: k for k, v in TEAM_NAME_MAP.items()}


async def run_one_team_audit(
    settings: AgentSettings,
    dry_run: bool,
    season_start: date,
    season_end: date,
) -> AuditRunResult | None:
    """Audit one team: scrape mlssoccer.com, compare against MT, submit findings.

    Returns:
        AuditRunResult if a team was audited, None if all teams are up-to-date.
    """
    from src.scraper.config import ScrapingConfig
    from src.scraper.mls_scraper import MLSScraper

    season = _current_season()
    audit_run_id = uuid.uuid4().hex[:12]
    event_id = uuid.uuid4().hex[:12]

    # Step 1: Fetch next team to audit (least recently audited)
    next_team = await fetch_next_team(
        api_url=settings.missing_table_api_url,
        api_key=settings.missing_table_api_key or "",
        season=season,
        division="Northeast",
        league="Homegrown",
    )
    if next_team is None:
        logger.info("audit.runner.all_current")
        return None

    team = next_team.team
    age_group = next_team.age_group
    league = next_team.league
    division = next_team.division
    logger.info(
        "audit.runner.team_selected",
        team=team,
        age_group=age_group,
        audit_run_id=audit_run_id,
    )

    # Step 2: Build ScrapingConfig — map MT canonical name back to MLS Next display name
    mls_club_name = MT_NAME_TO_MLS_NAME.get(team, team)
    config = ScrapingConfig(
        age_group=age_group,
        league=league,
        division=division,
        conference="",
        club=mls_club_name,
        start_date=season_start,
        end_date=season_end,
        look_back_days=(season_end - season_start).days,
        missing_table_api_url=settings.missing_table_api_url,
        missing_table_api_key=settings.missing_table_api_key or "unused",
    )

    # Step 3: Scrape matches via Playwright
    scraper = MLSScraper(config, headless=settings.headless)
    raw_matches = await scraper.scrape_matches()
    logger.info("audit.runner.scraped_raw", count=len(raw_matches))

    # Step 4: Normalize into match dicts (same logic as tools.py / scrape command)
    mt_division = config.conference if config.conference else config.division
    scraped = [
        {
            "home_team": _normalize_team_name(m.home_team, league=league),
            "away_team": _normalize_team_name(m.away_team, league=league),
            "match_date": m.match_datetime.date().isoformat(),
            "match_time": (
                m.match_datetime.strftime("%H:%M")
                if m.match_datetime.hour or m.match_datetime.minute
                else None
            ),
            "season": season,
            "age_group": age_group,
            "match_type": "League",
            "division": mt_division,
            "league": league,
            "home_score": m.home_score if isinstance(m.home_score, int) else None,
            "away_score": m.away_score if isinstance(m.away_score, int) else None,
            "match_status": m.match_status,
            "external_match_id": m.match_id,
            "location": m.location,
            "source": "match-scraper-agent",
        }
        for m in raw_matches
    ]

    # Step 5: Fetch MT matches for this team
    mt_matches = await fetch_mt_matches(
        api_url=settings.missing_table_api_url,
        api_key=settings.missing_table_api_key or "",
        age_group=age_group,
        league=league,
        division=division,
        team=team,
        season=season,
    )
    logger.info("audit.runner.mt_matches", count=len(mt_matches))

    # Step 6: Compare scraped vs MT
    findings = compare_matches(scraped, mt_matches, team)
    logger.info("audit.runner.findings", count=len(findings), team=team, age_group=age_group)

    # Step 7: Submit audit event (even if clean — records last_audited_at)
    event = AuditEvent(
        event_id=event_id,
        audit_run_id=audit_run_id,
        team=team,
        age_group=age_group,
        league=league,
        division=division,
        season=season,
        findings=findings,
    )

    if dry_run:
        logger.info(
            "audit.runner.dry_run_skip_submit",
            event_id=event_id,
            findings=len(findings),
        )
    else:
        await submit_audit_event(
            api_url=settings.missing_table_api_url,
            api_key=settings.missing_table_api_key or "",
            event=event,
        )

    return AuditRunResult(
        team=team,
        age_group=age_group,
        scraped_count=len(scraped),
        mt_count=len(mt_matches),
        findings=findings,
        dry_run=dry_run,
    )
