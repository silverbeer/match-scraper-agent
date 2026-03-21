"""Pipeline-based rules engine — replaces PydanticAI agent loop."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import structlog

from agent.deps import RunContext
from agent.planner import RunPlan, ScrapeAction, ScrapePlan
from agent.result import AgentAction, AgentResult
from agent.tools import SEASON_END, scrape_matches, submit_matches
from utils.journal import RunJournal, TargetEntry

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Modifier rules
# ---------------------------------------------------------------------------


def all_scored_skip(sp: ScrapePlan, entry: TargetEntry | None) -> ScrapePlan:
    """If target had 0 missing scores last run and planner says SCORE_SYNC, skip it."""
    if entry and sp.action == ScrapeAction.SCORE_SYNC and entry.missing_scores == 0:
        return sp.model_copy(
            update={
                "action": ScrapeAction.SKIP,
                "reason": f"All scored last run ({entry.matches_found} matches) — skipping",
            }
        )
    return sp


def error_retry(sp: ScrapePlan, entry: TargetEntry | None) -> ScrapePlan:
    """If target had errors last run, force full sync."""
    if entry and entry.errors > 0 and sp.action == ScrapeAction.SKIP:
        return sp.model_copy(
            update={
                "action": ScrapeAction.FULL_SYNC,
                "reason": f"Retrying after {entry.errors} error(s) last run",
                "start_date": date.today(),
                "end_date": SEASON_END,
            }
        )
    return sp


# Modifier registry — add new rules here
_MODIFIERS: list[Callable[[ScrapePlan, TargetEntry | None], ScrapePlan]] = [
    all_scored_skip,
    error_retry,
]


# ---------------------------------------------------------------------------
# Plan modification
# ---------------------------------------------------------------------------


def _journal_key(sp: ScrapePlan) -> str:
    """Build a journal lookup key from a ScrapePlan.

    Must match the target labels produced by journal._target_label().
    e.g. "U14 HG Northeast" or "U14 Academy New England"
    """
    cfg = sp.scraper_params
    ag = cfg.get("age_group", "?")
    league = cfg.get("league", "?")
    league_short = "HG" if league == "Homegrown" else "Academy"
    div = cfg.get("conference") or cfg.get("division", "?")
    return f"{ag} {league_short} {div}"


def apply_modifiers(plan: RunPlan, journal: RunJournal | None) -> RunPlan:
    """Apply journal-based modifier rules to the base plan."""
    if journal is None:
        return plan
    journal_lookup = {entry.target: entry for entry in journal.targets}
    modified = []
    for sp in plan.plans:
        entry = journal_lookup.get(_journal_key(sp))
        for modifier in _MODIFIERS:
            sp = modifier(sp, entry)
        modified.append(sp)
    return plan.model_copy(update={"plans": modified})


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def execute_plan(plan: RunPlan, ctx: RunContext) -> AgentResult:
    """Execute all non-SKIP targets sequentially."""
    actions: list[AgentAction] = []
    total_found = 0
    total_submitted = 0

    for rule in plan.plans:
        if rule.action == ScrapeAction.SKIP:
            actions.append(
                AgentAction(action="skip", detail=f"{rule.target_label}: {rule.reason}")
            )
            logger.info(
                "engine.skip",
                target=rule.target_label,
                reason=rule.reason,
            )
            continue

        # Reset per-target state
        ctx._scraped_matches = []

        cfg = rule.scraper_params
        scrape_result = await scrape_matches(
            ctx,
            start_date=rule.start_date.isoformat() if rule.start_date else "",
            end_date=rule.end_date.isoformat() if rule.end_date else "",
            age_group=cfg.get("age_group"),
            league=cfg.get("league"),
            division=cfg.get("division"),
            conference=cfg.get("conference"),
        )

        found = len(ctx._scraped_matches)
        total_found += found
        actions.append(
            AgentAction(
                action="scrape",
                detail=f"{rule.target_label}: {scrape_result}",
                dry_run=ctx.dry_run,
            )
        )

        if found > 0:
            submit_result = await submit_matches(ctx)
            submitted = found - len([e for e in ctx._submission_errors if not ctx.dry_run])
            if ctx.dry_run:
                submitted = 0
            total_submitted += submitted
            actions.append(
                AgentAction(
                    action="submit",
                    detail=f"{rule.target_label}: {submit_result}",
                    dry_run=ctx.dry_run,
                )
            )

    return AgentResult(
        summary=_build_summary(total_found, total_submitted, plan),
        actions=actions,
        matches_found=total_found,
        matches_submitted=total_submitted,
    )


def _build_summary(found: int, submitted: int, plan: RunPlan) -> str:
    """Build a human-readable run summary."""
    active = sum(1 for p in plan.plans if p.action != ScrapeAction.SKIP)
    skipped = sum(1 for p in plan.plans if p.action == ScrapeAction.SKIP)
    parts = [f"{active} targets scraped, {skipped} skipped"]
    parts.append(f"{found} matches found, {submitted} submitted")
    if plan.is_weekly_sync:
        parts.append("weekly full sync")
    return " — ".join(parts)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


async def run_pipeline(
    plan: RunPlan,
    ctx: RunContext,
    journal: RunJournal | None,
) -> AgentResult:
    """Execute the full pipeline: modify → execute → return result."""
    modified_plan = apply_modifiers(plan, journal)

    modified_count = sum(
        1
        for orig, mod in zip(plan.plans, modified_plan.plans, strict=True)
        if orig.action != mod.action
    )
    if modified_count:
        logger.info("engine.modifiers_applied", changes=modified_count)

    return await execute_plan(modified_plan, ctx)
