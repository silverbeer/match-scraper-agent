"""Deterministic scrape planner — day-of-week aware strategy.

Schedule logic:
  Mon-Wed: Score sync (Fri→Mon window) if scores are missing, else skip.
  Thu-Fri: Schedule check for upcoming weekend (after 16 UTC), else skip.
  Sat-Sun: Score sync (Fri→Mon window) — always scrape, scores are posting.

All scrapes use tight Friday-to-Monday windows. No full-season scrapes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

# Hour threshold for Thu/Fri schedule checks (UTC)
_SCHEDULE_CHECK_HOUR = 16


class ScrapeAction(StrEnum):
    FULL_SYNC = "full_sync"
    SCORE_SYNC = "score_sync"
    SCHEDULE_CHECK = "schedule_check"
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
    is_weekly_sync: bool = False
    mt_api_status: str = ""  # "ok", "failed:<reason>", "empty"


def _weekend_window(today: date) -> tuple[date, date]:
    """Compute the Friday-to-Monday window for the relevant weekend.

    Mon-Wed: last weekend (previous Fri→Mon).
    Thu-Fri: upcoming weekend (next Fri→Mon).
    Sat-Sun: current weekend (this Fri→Mon).
    """
    wd = today.weekday()  # 0=Mon, 6=Sun

    if wd in (0, 1, 2):  # Mon-Wed → last weekend
        friday = today - timedelta(days=wd + 3)
        monday = today - timedelta(days=wd)
    elif wd in (3, 4):  # Thu-Fri → upcoming weekend
        friday = today + timedelta(days=4 - wd)
        monday = friday + timedelta(days=3)
    else:  # Sat-Sun → current weekend
        friday = today - timedelta(days=wd - 4)
        monday = friday + timedelta(days=3)

    return friday, monday


def _target_label(cfg: dict[str, str]) -> str:
    """Build a human-readable label from a scraper config dict."""
    ag = cfg.get("age_group", "?")
    league = cfg.get("league", "?")
    if cfg.get("conference"):
        return f"{ag} {league} {cfg['conference']}"
    return f"{ag} {league} {cfg.get('division', '?')}"


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


def fetch_mt_status(
    api_url: str,
    api_key: str,
    season: str,
) -> tuple[list[dict[str, Any]], str]:
    """Call MT match-summary API synchronously.

    Returns:
        (targets_list, status_string) where status is "ok", "failed:<reason>", or "empty".
    """
    import httpx

    url = f"{api_url}/api/agent/match-summary"
    logger.info("planner.fetch_mt_status", url=url, season=season)

    try:
        resp = httpx.get(
            url,
            params={"season": season},
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


def compute_scrape_plan(
    mt_targets: list[dict[str, Any]],
    target_configs: dict[str, dict[str, str]],
    now: datetime,
) -> RunPlan:
    """Compute a day-of-week aware scrape plan for all non-IFA targets.

    Args:
        mt_targets: Target dicts from the MT API response.
        target_configs: The _TARGET_SCRAPER_CONFIG dict from main.py.
        now: Current UTC datetime (used for day-of-week and hour).
    """
    today = now.date()
    weekday = now.weekday()  # 0=Mon, 6=Sun
    hour = now.hour
    friday, monday = _weekend_window(today)

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

        # Safety net: MT has no data for this division
        if mt_data is None or mt_data.get("total", 0) == 0:
            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.FULL_SYNC,
                    start_date=friday,
                    end_date=monday,
                    reason="No matches in MT — needs initial sync",
                    scraper_params=cfg,
                )
            )
            continue

        needs_score = mt_data.get("needs_score", 0)
        total = mt_data.get("total", 0)
        last_played = mt_data.get("last_played_date", "none")

        if weekday in (0, 1, 2):  # Mon-Wed: score focus
            if needs_score > 0:
                plans.append(
                    ScrapePlan(
                        target_key=target_key,
                        target_label=label,
                        action=ScrapeAction.SCORE_SYNC,
                        start_date=friday,
                        end_date=monday,
                        reason=f"{needs_score} match(es) awaiting scores",
                        scraper_params=cfg,
                    )
                )
            else:
                plans.append(
                    ScrapePlan(
                        target_key=target_key,
                        target_label=label,
                        action=ScrapeAction.SKIP,
                        reason=f"Up to date ({total} matches, last played {last_played})",
                        scraper_params=cfg,
                    )
                )

        elif weekday in (3, 4):  # Thu-Fri: schedule check
            if hour < _SCHEDULE_CHECK_HOUR:
                plans.append(
                    ScrapePlan(
                        target_key=target_key,
                        target_label=label,
                        action=ScrapeAction.SKIP,
                        reason=f"Schedule check runs after {_SCHEDULE_CHECK_HOUR}:00 UTC",
                        scraper_params=cfg,
                    )
                )
            else:
                plans.append(
                    ScrapePlan(
                        target_key=target_key,
                        target_label=label,
                        action=ScrapeAction.SCHEDULE_CHECK,
                        start_date=friday,
                        end_date=monday,
                        reason="Checking upcoming weekend schedule",
                        scraper_params=cfg,
                    )
                )

        else:  # Sat-Sun: score focus, always scrape
            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.SCORE_SYNC,
                    start_date=friday,
                    end_date=monday,
                    reason=f"Weekend score sync ({needs_score} awaiting scores)",
                    scraper_params=cfg,
                )
            )

    mt_status = "ok" if mt_targets else ("empty" if mt_targets is not None else "failed")
    return RunPlan(plans=plans, mt_api_status=mt_status)


def format_plan_prompt(plan: RunPlan) -> str:
    """Format the scrape plan as explicit LLM instructions."""
    lines = [
        "## Scrape Plan",
        "",
        "Follow this plan exactly. Do NOT call get_match_status() — "
        "the plan is pre-computed from MT data.",
        "",
    ]

    step = 0
    for p in plan.plans:
        step += 1
        if p.action == ScrapeAction.SKIP:
            lines.append(f"{step}. **{p.target_label}: SKIP**")
            lines.append(f"   Reason: {p.reason}")
            lines.append("")
            continue

        action_labels = {
            ScrapeAction.FULL_SYNC: "FULL SYNC",
            ScrapeAction.SCORE_SYNC: "SCORE SYNC",
            ScrapeAction.SCHEDULE_CHECK: "SCHEDULE CHECK",
        }
        action_label = action_labels.get(p.action, p.action.value.upper())
        lines.append(f"{step}. **{p.target_label}: {action_label}**")
        lines.append(f"   Reason: {p.reason}")

        # Build exact scrape_matches call params
        params = [
            f'start_date="{p.start_date.isoformat()}"' if p.start_date else "",
            f'end_date="{p.end_date.isoformat()}"' if p.end_date else "",
        ]
        cfg = p.scraper_params
        if cfg.get("age_group"):
            params.append(f'age_group="{cfg["age_group"]}"')
        if cfg.get("league"):
            params.append(f'league="{cfg["league"]}"')
        if cfg.get("division"):
            params.append(f'division="{cfg["division"]}"')
        if cfg.get("conference"):
            params.append(f'conference="{cfg["conference"]}"')

        param_str = ", ".join(p for p in params if p)
        lines.append(f"   Call: scrape_matches({param_str})")
        lines.append("   Then call: submit_matches()")
        lines.append("")

    skipped = sum(1 for p in plan.plans if p.action == ScrapeAction.SKIP)
    active = len(plan.plans) - skipped
    lines.append(f"Summary: {active} targets to scrape, {skipped} skipped.")

    return "\n".join(lines)
