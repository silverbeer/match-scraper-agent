"""Deterministic scrape planner — replaces LLM decision-making."""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

# Kickoff-sync lookahead: check matches within this many days for missing kick-off times
_KICKOFF_LOOKAHEAD_DAYS = 14


class ScrapeAction(StrEnum):
    FULL_SYNC = "full_sync"
    SCORE_SYNC = "score_sync"
    KICKOFF_SYNC = "kickoff_sync"
    SKIP = "skip"


class ScrapePlan(BaseModel):
    """Scrape plan for a single target."""

    target_key: str  # e.g. "u14-hg"
    target_label: str  # e.g. "U14 Homegrown Northeast"
    action: ScrapeAction
    start_date: date | None = None
    end_date: date | None = None
    reason: str = ""
    scraper_params: dict[str, str] = {}


class RunPlan(BaseModel):
    """Full scrape plan for all targets in a single run."""

    plans: list[ScrapePlan] = []
    mt_api_status: str = ""  # "ok", "failed:<reason>", "empty"


def _target_label(cfg: dict[str, str]) -> str:
    """Build a human-readable label from a scraper config dict."""
    ag = cfg.get("age_group", "?")
    league = cfg.get("league", "?")
    if cfg.get("conference"):
        return f"{ag} {league} {cfg['conference']}"
    return f"{ag} {league} {cfg.get('division', '?')}"


def _match_weekend_window(today: date) -> tuple[date, date]:
    """Return a window covering last weekend through this coming Monday.

    Covers two weekends:
    - Last weekend (scores may still be posting)
    - This weekend (check for schedule changes)

    The window starts on the Friday *before* last weekend and ends on the
    Monday *after* this weekend.

    Examples (all dates 2026):
        Friday  Mar 20 -> Mar 13 to Mar 23  (last Fri through this Mon)
        Monday  Mar 16 -> Mar 13 to Mar 23
        Wednesday Mar 18 -> Mar 13 to Mar 23

    Returns:
        (start_friday, end_monday) covering both weekends.
    """
    weekday = today.weekday()  # 0=Mon … 6=Sun
    # Find this week's Friday (upcoming or today)
    days_until_fri = (4 - weekday) % 7
    this_friday = today + timedelta(days=days_until_fri)
    # Last Friday is 7 days before this Friday
    last_friday = this_friday - timedelta(days=7)
    # Monday after this weekend
    this_monday = this_friday + timedelta(days=3)
    return last_friday, this_monday


def _mt_key(age_group: str, league: str, division: str) -> tuple[str, str, str]:
    """Normalize MT response fields into a lookup key."""
    return (age_group, league, division)


def _cfg_key(cfg: dict[str, str]) -> tuple[str, str, str]:
    """Normalize scraper config fields into a lookup key.

    For Academy targets, MT stores the conference in the division field.
    """
    if cfg.get("conference"):
        return (cfg["age_group"], cfg["league"], cfg["conference"])
    return (cfg["age_group"], cfg["league"], cfg.get("division", ""))


def _last_weekend(today: date) -> tuple[date, date]:
    """Return (saturday, sunday) of the most recent past weekend.

    On Sat/Sun returns the current weekend. On Mon-Fri returns last weekend.
    """
    weekday = today.weekday()  # 0=Mon … 6=Sun
    if weekday == 6:  # Sunday
        return today - timedelta(days=1), today
    if weekday == 5:  # Saturday
        return today, today + timedelta(days=1)
    # Mon-Fri: go back to last Saturday
    days_since_sat = weekday + 2
    sat = today - timedelta(days=days_since_sat)
    return sat, sat + timedelta(days=1)


def fetch_mt_status(
    api_url: str,
    api_key: str,
    season: str,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Call MT match-summary API synchronously.

    Passes last weekend's date range as score_from/score_to so that
    needs_score only reflects matches from last weekend.

    Returns:
        (targets_list, status_string) where status is "ok", "failed:<reason>", or "empty".
    """
    import httpx

    if today is None:
        today = date.today()

    sat, sun = _last_weekend(today)
    url = f"{api_url}/api/agent/match-summary"
    logger.info("planner.fetch_mt_status", url=url, season=season, score_from=sat, score_to=sun)

    try:
        resp = httpx.get(
            url,
            params={
                "season": season,
                "score_from": sat.isoformat(),
                "score_to": sun.isoformat(),
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("planner.fetch_mt_status.failed", error=str(exc))
        return [], f"failed:{exc}"

    targets = data.get("targets", [])
    if not targets:
        return [], "empty"

    logger.info("planner.fetch_mt_status.ok", target_count=len(targets))
    return targets, "ok"


def _clamp(start: date, end: date, season_start: date) -> tuple[date, date]:
    """
    Confine a planned window to dates MLS Next will serve.

    Mirrors ``tools.clamp_scrape_range``, applied here as well so that plans,
    logs and Telegram reports quote the dates actually scraped rather than the
    ones we would have liked.
    """
    clamped_start = max(start, season_start)
    return clamped_start, max(end, clamped_start)


def compute_scrape_plan(
    mt_targets: list[dict[str, Any]],
    target_configs: dict[str, dict[str, str]],
    today: date,
    season_end: date,
    season_start: date,
) -> RunPlan:
    """Compute a deterministic scrape plan for all non-IFA targets.

    Args:
        mt_targets: Target dicts from the MT API response.
        target_configs: The _TARGET_SCRAPER_CONFIG dict from main.py.
        today: Today's date.
        season_end: Season end date (SEASON_END constant).
        season_start: Earliest date MLS Next serves (SEASON_START constant).
            Windows are clamped to it — the score-sync window looks back to the
            Friday before last weekend, which in early August lands in the
            previous season and the site rejects outright. Required rather than
            defaulted, so this planner stays a pure function of its arguments
            instead of quietly depending on the wall clock.
    """
    # Build lookup: (age_group, league, division) → MT target data
    mt_lookup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for t in mt_targets:
        key = _mt_key(t["age_group"], t["league"], t["division"])
        mt_lookup[key] = t

    # Only plan for division-level targets (skip IFA-specific ones)
    plan_targets = {k: v for k, v in target_configs.items() if not k.endswith("-ifa")}

    plans: list[ScrapePlan] = []
    for target_key, cfg in plan_targets.items():
        label = _target_label(cfg)
        lookup_key = _cfg_key(cfg)
        mt_data = mt_lookup.get(lookup_key)

        if mt_data is None or mt_data.get("total", 0) == 0:
            full_start, full_end = _clamp(today, season_end, season_start)
            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.FULL_SYNC,
                    start_date=full_start,
                    end_date=full_end,
                    reason="No matches in MT — needs initial sync",
                    scraper_params=cfg,
                )
            )
            continue

        needs_score = mt_data.get("needs_score", 0)
        needs_kickoff = mt_data.get("needs_kickoff", 0)

        if needs_score > 0:
            fri, mon = _clamp(*_match_weekend_window(today), season_start)

            reason_parts = [f"{needs_score} match(es) awaiting scores"]
            if needs_kickoff > 0:
                reason_parts.append(f"{needs_kickoff} missing kick-off time(s)")

            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.SCORE_SYNC,
                    start_date=fri,
                    end_date=mon,
                    reason=", ".join(reason_parts),
                    scraper_params=cfg,
                )
            )
            continue

        if needs_kickoff > 0:
            ko_start, ko_end = _clamp(
                today, today + timedelta(days=_KICKOFF_LOOKAHEAD_DAYS), season_start
            )
            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.KICKOFF_SYNC,
                    start_date=ko_start,
                    end_date=ko_end,
                    reason=f"{needs_kickoff} match(es) missing kick-off time",
                    scraper_params=cfg,
                )
            )
            continue

        # Fully up to date
        total = mt_data.get("total", 0)
        last_played = mt_data.get("last_played_date", "none")
        plans.append(
            ScrapePlan(
                target_key=target_key,
                target_label=label,
                action=ScrapeAction.SKIP,
                reason=f"Up to date ({total} matches, last played {last_played})",
                scraper_params=cfg,
            )
        )

    mt_status = "ok" if mt_targets else ("empty" if mt_targets is not None else "failed")
    return RunPlan(plans=plans, mt_api_status=mt_status)
