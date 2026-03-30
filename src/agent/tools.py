"""Tool functions for the match-scraper-agent pipeline engine."""

from __future__ import annotations

import asyncio
from datetime import date

import structlog

from agent.deps import RunContext

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


# Season end date — enforced as a floor for end_date so we can't
# accidentally use a shorter range than the full remaining season.
SEASON_END = date(2026, 6, 30)


async def scrape_matches(
    ctx: RunContext,
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
    """
    from src.scraper.config import ScrapingConfig
    from src.scraper.mls_scraper import MLSScraper

    parsed_start = date.fromisoformat(start_date)
    parsed_end = date.fromisoformat(end_date)

    look_back = (parsed_end - parsed_start).days

    config = ScrapingConfig(
        age_group=age_group or ctx.age_group,
        league=league or ctx.league,
        division=division or ctx.division,
        conference=conference or "",
        club=club or "",
        start_date=parsed_start,
        end_date=parsed_end,
        look_back_days=look_back,
        missing_table_api_url=ctx.missing_table_api_url,
        missing_table_api_key=ctx.missing_table_api_key or "unused",
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
        scraper = MLSScraper(config, headless=ctx.headless)
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
            # Omit match_time when unknown — prevents overwriting an existing
            # MT kick-off time with null (MLS Next drops time on completed matches)
            **(
                {"match_time": m.match_datetime.strftime("%H:%M")}
                if m.match_datetime.hour or m.match_datetime.minute
                else {}
            ),
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
    team_filter = ctx.team_filter
    if team_filter:
        before = len(built)
        built = [m for m in built if team_filter in (m["home_team"], m["away_team"])]
        logger.info(
            "tool.scrape_matches.team_filter",
            team=team_filter,
            before=before,
            after=len(built),
        )

    ctx._scraped_matches += built

    if not matches:
        target = f"{config.age_group} {config.league}"
        if config.conference:
            target += f" {config.conference}"
        elif config.division:
            target += f" {config.division}"
        return f"No matches found for {target} ({start_date} to {end_date})."

    # Build a human-readable summary
    lines = [f"Found {len(matches)} matches ({start_date} to {end_date}):"]
    for m in matches:
        score = f" ({m.home_score}-{m.away_score})" if m.has_score() else ""
        status = m.match_status
        lines.append(
            f"  {m.match_datetime.date()} | {m.home_team} vs {m.away_team}{score} [{status}]"
        )

    logger.info("tool.scrape_matches.done", matches_found=len(matches))
    return "\n".join(lines)


async def submit_matches(ctx: RunContext) -> str:
    """Submit scraped matches to the RabbitMQ queue for processing.

    Publishes all matches from the most recent scrape_matches call. Each match
    is validated against the MatchData schema before sending. This is a mutating
    operation — respects dry_run.

    Call this after scrape_matches if matches were found.
    """
    matches = ctx._scraped_matches
    if not matches:
        return "No matches to submit. Run scrape_matches first."

    if ctx.dry_run:
        logger.info("tool.submit_matches.dry_run", count=len(matches))
        return f"[DRY RUN] Would submit {len(matches)} matches to queue."

    submitted = 0
    errors = 0
    for match_dict in matches:
        try:
            ctx.queue_client.submit_match(match_dict)
            submitted += 1
        except Exception as exc:
            errors += 1
            match_label = f"{match_dict['home_team']} vs {match_dict['away_team']}"
            ctx._submission_errors.append({"match": match_label, "error": str(exc)})
            logger.warning(
                "tool.submit_matches.error",
                match=match_label,
                error=str(exc),
            )

    logger.info("tool.submit_matches.done", submitted=submitted, errors=errors)
    return f"Submitted {submitted} matches to queue ({errors} errors)."


def _current_season() -> str:
    """Return the current season string (e.g. '2025-2026')."""
    today = date.today()
    # Season starts in August: Aug 2025 → "2025-2026", Jan 2026 → "2025-2026"
    if today.month >= 8:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"
