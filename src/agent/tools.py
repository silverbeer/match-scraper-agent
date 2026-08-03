"""Tool functions for the match-scraper-agent pipeline engine."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import structlog
from src.scraper.modular11 import (
    current_season_year,
    fall_segment_window,
    season_label,
    season_window,
)

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
#
# Derived, not pinned. The previous hardcoded date(2026, 6, 30) silently
# became a date in the *past* when the 2026-2027 season opened, which made
# this floor produce an end_date before the start_date. Deriving it from the
# current season means the rollover is a no-op instead of an outage.
SEASON_END = season_window(current_season_year())[1]

# Season start date — the earliest date MLS Next will serve. The schedule
# calendar refuses to navigate to months before the current season and fails
# with "Failed to navigate to start date month", so asking for anything
# earlier is not a smaller result, it is a broken scrape.
SEASON_START = season_window(current_season_year())[0]

# The latest date a scrape may end on.
#
# The MLS Next date picker shows two month panels side by side, and the
# season's final month (July 2027) is the last one it will render — there is
# no August 2027 to pair it with, so July can only ever be the *right* panel.
# Our calendar code navigates the start month into the *left* panel, so any
# range ending in that final month fails with "Failed to navigate to start
# date month". Measured 2026-08-03: a range ending 15 Jun 2027 works, one
# ending 15 Jul 2027 fails, and even a two-week range inside July 2027 fails.
#
# So we stop at the end of the month *before* the season's final month.
#
# This costs no data: matches run September-November in the fall segment and
# March-June in the spring, so nothing is ever scheduled in the season's first
# or last month.
SCRAPE_END_CAP = SEASON_END.replace(day=1) - timedelta(days=1)


def current_segment_window(today: date | None = None) -> tuple[date, date]:
    """
    Return the window for the segment ``today`` falls in.

    The season splits in two, and scraping a segment at a time keeps the
    request inside what the date picker can express:

    * Fall:   1 August  - 31 December
    * Spring: 1 January - 1 July

    The returned end is capped at :data:`SCRAPE_END_CAP`, so the spring
    segment stops short of the season's unreachable final month rather than
    reproducing the full_sync failure every January.
    """
    today = today or date.today()
    season_year = current_season_year(today)
    fall_start, fall_end = fall_segment_window(season_year)

    if today <= fall_end:
        return fall_start, min(fall_end, SCRAPE_END_CAP)

    spring_start = date(season_year + 1, 1, 1)
    spring_end = date(season_year + 1, 7, 1)
    return spring_start, min(spring_end, SCRAPE_END_CAP)


def clamp_scrape_range(start: date, end: date) -> tuple[date, date]:
    """
    Confine a scrape window to dates MLS Next will actually serve.

    Two guards, both learned the hard way:

    * ``start`` is raised to the season start. The weekend-scores window looks
      back to the Friday before last weekend, which in early August points at
      the previous season — a range the site rejects outright.
    * ``end`` is lowered to :data:`SCRAPE_END_CAP`, keeping the range clear of
      the season's final month, which the date picker cannot use as the left
      panel.
    * ``end`` is never allowed before ``start``. The season window closes on
      15 July while the season year rolls over on 1 August, leaving the second
      half of July a dead zone where an unguarded ``end`` sits in the past.
      That inversion produced ``look_back_days=-33`` and failed every
      production run.

    Returns the corrected ``(start, end)``.
    """
    clamped_start = max(start, SEASON_START)
    clamped_end = max(min(end, SCRAPE_END_CAP), clamped_start)
    return clamped_start, clamped_end


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

    requested_start = date.fromisoformat(start_date)
    requested_end = date.fromisoformat(end_date)

    # Last line of defence: every scrape goes through here, whoever planned it.
    parsed_start, parsed_end = clamp_scrape_range(requested_start, requested_end)
    if (parsed_start, parsed_end) != (requested_start, requested_end):
        logger.info(
            "tool.scrape_matches.range_clamped",
            requested_start=requested_start.isoformat(),
            requested_end=requested_end.isoformat(),
            start=parsed_start.isoformat(),
            end=parsed_end.isoformat(),
            season_start=SEASON_START.isoformat(),
        )

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
        start=parsed_start.isoformat(),
        end=parsed_end.isoformat(),
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
        return f"No matches found for {target} ({parsed_start} to {parsed_end})."

    # Build a human-readable summary
    lines = [f"Found {len(matches)} matches ({parsed_start} to {parsed_end}):"]
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
    """
    Return the current season string (e.g. '2026-2027').

    Delegates to match-scraper rather than reimplementing the August cutoff.
    Two copies of this rule is how the agent and the scraper ended up posting
    different season formats ('2025-2026' vs '2024-25') for the same match.
    """
    return season_label(current_season_year())
