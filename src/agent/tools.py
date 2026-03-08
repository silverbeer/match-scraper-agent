"""PydanticAI tool functions for the match-scraper-agent."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import structlog
from pydantic_ai import RunContext

from agent.deps import AgentDeps

logger = structlog.get_logger()

# Serialize browser launches — one Chromium at a time
_scrape_semaphore = asyncio.Semaphore(1)

# MLS Next full names → missing-table DB names
TEAM_NAME_MAP: dict[str, str] = {
    "Intercontinental Football Academy of New England": "IFA",
}

# Academy league overrides (same MLS Next name, different DB team)
ACADEMY_TEAM_NAME_MAP: dict[str, str] = {
    "Intercontinental Football Academy of New England": "IFA Academy",
}


def _normalize_team_name(name: str, *, league: str = "") -> str:
    """Map MLS Next display names to missing-table canonical names."""
    if league == "Academy":
        return ACADEMY_TEAM_NAME_MAP.get(name, TEAM_NAME_MAP.get(name, name))
    return TEAM_NAME_MAP.get(name, name)


def get_today_info(ctx: RunContext[AgentDeps]) -> str:
    """Get today's date information to help decide what actions to take.

    Returns the current date, day of week, and week number. Use this at the
    start of your run to understand what day it is.
    """
    now = datetime.now(tz=UTC)
    logger.info("tool.get_today_info", date=now.strftime("%Y-%m-%d"), day=now.strftime("%A"))
    return (
        f"Date: {now.strftime('%Y-%m-%d')}\n"
        f"Day: {now.strftime('%A')}\n"
        f"Week: {now.isocalendar().week}\n"
        f"Time (UTC): {now.strftime('%H:%M')}"
    )


async def get_match_status(ctx: RunContext[AgentDeps]) -> str:
    """Check what match data MT already has for the current season.

    Queries the MT backend for match counts grouped by age group, league,
    and division. Use this to decide what needs scraping vs what's up to date.
    """
    import httpx

    settings = ctx.deps.settings
    season = _current_season()
    url = f"{settings.missing_table_api_url}/api/agent/match-summary"

    logger.info("tool.get_match_status", url=url, season=season)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                params={"season": season},
                headers={"Authorization": f"Bearer {settings.missing_table_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("tool.get_match_status.failed", error=str(exc))
        return "Could not reach MT backend. Falling back to full-season scrape."

    targets = data.get("targets", [])
    if not targets:
        return f"MT has no matches for {season}. All targets need full-season sync."

    lines = [f"MT Status ({season} season):\n"]
    for t in targets:
        status_parts = []
        for s in ("played", "scheduled", "tbd", "postponed", "cancelled"):
            count = t["by_status"].get(s, 0)
            if count:
                status_parts.append(f"{count} {s}")

        needs = t.get("needs_score", 0)
        if needs:
            status_parts.append(f"{needs} awaiting scores")

        summary = ", ".join(status_parts) if status_parts else "no matches"
        line = f"{t['age_group']} {t['league']} {t['division']}: {t['total']} matches ({summary})"

        details = []
        dr = t.get("date_range", {})
        if dr.get("earliest") and dr.get("latest"):
            details.append(f"Dates: {dr['earliest']} - {dr['latest']}")
        if t.get("last_played_date"):
            details.append(f"Last played: {t['last_played_date']}")
        if details:
            line += f"\n  {' | '.join(details)}"

        lines.append(line)

    logger.info("tool.get_match_status.done", target_count=len(targets))
    return "\n".join(lines)


# Season end date — enforced as a floor for end_date so the LLM can't
# accidentally use a shorter range than the full remaining season.
SEASON_END = date(2026, 6, 30)


async def scrape_matches(
    ctx: RunContext[AgentDeps],
    start_date: str,
    end_date: str,
    age_group: str | None = None,
    league: str | None = None,
    division: str | None = None,
    conference: str | None = None,
    club: str | None = None,
) -> str:
    """Scrape match data from the MLS Next website for a date range.

    Uses Playwright + CSS selectors to extract match data. No LLM tokens
    consumed — this is pure browser automation.

    Args:
        start_date: Start date (ISO 8601, e.g. "2026-02-18").
        end_date: End date (ISO 8601, e.g. "2026-05-10").
        age_group: Age group to scrape (e.g. "U14"). Defaults to agent config.
        league: League type ("Homegrown" or "Academy"). Defaults to agent config.
        division: Division filter for Homegrown (e.g. "Northeast"). Defaults to agent config.
        conference: Conference filter for Academy (e.g. "New England"). Optional.
        club: Club name filter (e.g. "Intercontinental Football Academy of New England").
            Filters results to only matches involving this club. Optional.
    """
    from src.scraper.config import ScrapingConfig
    from src.scraper.mls_scraper import MLSScraper

    settings = ctx.deps.settings
    parsed_start = date.fromisoformat(start_date)
    parsed_end = date.fromisoformat(end_date)

    # Guarantee the end date covers the full season regardless of what the LLM passes
    if parsed_end < SEASON_END:
        logger.info(
            "tool.scrape_matches.extend_end_date",
            requested=end_date,
            enforced=SEASON_END.isoformat(),
        )
        parsed_end = SEASON_END

    look_back = (parsed_end - parsed_start).days

    config = ScrapingConfig(
        age_group=age_group or settings.age_group,
        league=league or settings.league,
        division=division or settings.division,
        conference=conference or "",
        club=club or "",
        start_date=parsed_start,
        end_date=parsed_end,
        look_back_days=look_back,
        missing_table_api_url=settings.missing_table_api_url,
        missing_table_api_key=settings.missing_table_api_key or "unused",
    )

    logger.info(
        "tool.scrape_matches",
        start=start_date,
        end=end_date,
        age_group=config.age_group,
        league=config.league,
        division=config.division,
        conference=config.conference or None,
    )

    async with _scrape_semaphore:
        scraper = MLSScraper(config, headless=ctx.deps.headless)
        matches = await scraper.scrape_matches()

    # For MT backend: division field stores the conference name for Academy league
    # (MT has no separate conference field — "New England" is a division in Academy)
    mt_division = config.conference if config.conference else config.division

    # Accumulate matches for submit_matches to pick up
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

    # Apply team filter if set (e.g. --target u14-hg-ifa)
    team_filter = ctx.deps.team_filter
    if team_filter:
        before = len(built)
        built = [m for m in built if team_filter in (m["home_team"], m["away_team"])]
        logger.info(
            "tool.scrape_matches.team_filter",
            team=team_filter,
            before=before,
            after=len(built),
        )

    ctx.deps._scraped_matches += built

    if not matches:
        target = f"{config.age_group} {config.league}"
        if config.conference:
            target += f" {config.conference}"
        elif config.division:
            target += f" {config.division}"
        return f"No matches found for {target} ({start_date} to {end_date})."

    # Build a human-readable summary for the LLM
    lines = [f"Found {len(matches)} matches ({start_date} to {end_date}):"]
    for m in matches:
        score = f" ({m.home_score}-{m.away_score})" if m.has_score() else ""
        status = m.match_status
        lines.append(
            f"  {m.match_datetime.date()} | {m.home_team} vs {m.away_team}{score} [{status}]"
        )

    logger.info("tool.scrape_matches.done", matches_found=len(matches))
    return "\n".join(lines)


async def submit_matches(ctx: RunContext[AgentDeps]) -> str:
    """Submit scraped matches to the RabbitMQ queue for processing.

    Publishes all matches from the most recent scrape_matches call. Each match
    is validated against the MatchData schema before sending. This is a mutating
    operation — respects dry_run.

    Call this after scrape_matches if matches were found.
    """
    matches = ctx.deps._scraped_matches
    if not matches:
        return "No matches to submit. Run scrape_matches first."

    if ctx.deps.dry_run:
        logger.info("tool.submit_matches.dry_run", count=len(matches))
        return f"[DRY RUN] Would submit {len(matches)} matches to queue."

    submitted = 0
    errors = 0
    for match_dict in matches:
        try:
            ctx.deps.queue_client.submit_match(match_dict)
            submitted += 1
        except Exception as exc:
            errors += 1
            match_label = f"{match_dict['home_team']} vs {match_dict['away_team']}"
            ctx.deps._submission_errors.append({"match": match_label, "error": str(exc)})
            logger.warning(
                "tool.submit_matches.error",
                match=match_label,
                error=str(exc),
            )

    logger.info("tool.submit_matches.done", submitted=submitted, errors=errors)
    return f"Submitted {submitted} matches to queue ({errors} errors)."


def _current_season() -> str:
    """Return the current season string (e.g. '2025-26')."""
    today = date.today()
    # Season starts in August: Aug 2025 → "2025-26", Jan 2026 → "2025-26"
    if today.month >= 8:
        return f"{today.year}-{str(today.year + 1)[2:]}"
    return f"{today.year - 1}-{str(today.year)[2:]}"
