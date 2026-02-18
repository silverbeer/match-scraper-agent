"""PydanticAI tool functions for the match-scraper-agent."""

from __future__ import annotations

from datetime import UTC, date, datetime

import structlog
from pydantic_ai import RunContext

from agent.deps import AgentDeps

logger = structlog.get_logger()


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


async def scrape_matches(
    ctx: RunContext[AgentDeps],
    start_date: str,
    end_date: str,
    age_group: str | None = None,
    league: str | None = None,
    division: str | None = None,
    conference: str | None = None,
) -> str:
    """Scrape match data from the MLS Next website for a date range.

    Uses Playwright + CSS selectors to extract match data. No LLM tokens
    consumed — this is pure browser automation.

    Args:
        start_date: Start date (ISO 8601, e.g. "2026-02-18").
        end_date: End date (ISO 8601, e.g. "2026-02-25").
        age_group: Age group to scrape (e.g. "U14"). Defaults to agent config.
        league: League type ("Homegrown" or "Academy"). Defaults to agent config.
        division: Division filter for Homegrown (e.g. "Northeast"). Defaults to agent config.
        conference: Conference filter for Academy (e.g. "New England"). Optional.
    """
    from src.scraper.config import ScrapingConfig
    from src.scraper.mls_scraper import MLSScraper

    settings = ctx.deps.settings
    parsed_start = date.fromisoformat(start_date)
    parsed_end = date.fromisoformat(end_date)
    look_back = (parsed_end - parsed_start).days

    config = ScrapingConfig(
        age_group=age_group or settings.age_group,
        league=league or settings.league,
        division=division or settings.division,
        conference=conference or "",
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

    scraper = MLSScraper(config, headless=True)
    matches = await scraper.scrape_matches()

    # For MT backend: division field stores the conference name for Academy league
    # (MT has no separate conference field — "New England" is a division in Academy)
    mt_division = config.conference if config.conference else config.division

    # Accumulate matches for submit_matches to pick up
    ctx.deps._scraped_matches += [
        {
            "home_team": m.home_team,
            "away_team": m.away_team,
            "match_date": m.match_datetime.date().isoformat(),
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
            logger.warning(
                "tool.submit_matches.error",
                match=f"{match_dict['home_team']} vs {match_dict['away_team']}",
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
