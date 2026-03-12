"""Deterministic scrape planner — replaces LLM decision-making."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

# Score-sync lookback: scrape this many days before the earliest unscored match
_SCORE_SYNC_LOOKBACK_DAYS = 3


class ScrapeAction(StrEnum):
    FULL_SYNC = "full_sync"
    SCORE_SYNC = "score_sync"
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


def is_weekly_sync_run(now: datetime) -> bool:
    """Monday 02:00-03:00 UTC = weekly full sync window."""
    return now.weekday() == 0 and now.hour in (2, 3)


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
    today: date,
    season_end: date,
    is_weekly: bool,
) -> RunPlan:
    """Compute a deterministic scrape plan for all non-IFA targets.

    Args:
        mt_targets: Target dicts from the MT API response.
        target_configs: The _TARGET_SCRAPER_CONFIG dict from main.py.
        today: Today's date.
        season_end: Season end date (SEASON_END constant).
        is_weekly: True for the Monday 02:00 UTC weekly full-sync run.
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

        if is_weekly:
            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.FULL_SYNC,
                    start_date=today,
                    end_date=season_end,
                    reason="Weekly full sync (Monday)",
                    scraper_params=cfg,
                )
            )
            continue

        if mt_data is None or mt_data.get("total", 0) == 0:
            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.FULL_SYNC,
                    start_date=today,
                    end_date=season_end,
                    reason="No matches in MT — needs initial sync",
                    scraper_params=cfg,
                )
            )
            continue

        needs_score = mt_data.get("needs_score", 0)
        if needs_score > 0:
            last_played = mt_data.get("last_played_date")
            if last_played:
                try:
                    lp = date.fromisoformat(last_played)
                    start = lp - timedelta(days=_SCORE_SYNC_LOOKBACK_DAYS)
                except ValueError:
                    start = today
            else:
                start = today

            plans.append(
                ScrapePlan(
                    target_key=target_key,
                    target_label=label,
                    action=ScrapeAction.SCORE_SYNC,
                    start_date=start,
                    end_date=season_end,
                    reason=f"{needs_score} match(es) awaiting scores",
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
    return RunPlan(plans=plans, is_weekly_sync=is_weekly, mt_api_status=mt_status)


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

        action_label = "FULL SYNC" if p.action == ScrapeAction.FULL_SYNC else "SCORE SYNC"
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
