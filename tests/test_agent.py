"""Tests for the pipeline engine, modifiers, and run_pipeline."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from agent.deps import RunContext
from agent.engine import (
    all_scored_skip,
    apply_modifiers,
    error_retry,
    execute_plan,
    run_pipeline,
)
from agent.planner import RunPlan, ScrapeAction, ScrapePlan
from agent.result import AgentResult
from utils.journal import RunJournal, TargetEntry


def _make_ctx(*, dry_run: bool = False) -> RunContext:
    """Create a RunContext with a mocked queue client."""
    queue_client = MagicMock()
    queue_client.submit_match.return_value = "task-id-123"
    return RunContext(
        queue_client=queue_client,
        missing_table_api_url="http://localhost:8000",
        missing_table_api_key="test-key",
        dry_run=dry_run,
    )


def _make_plan(
    action: ScrapeAction = ScrapeAction.FULL_SYNC,
    target_key: str = "u14-hg",
    reason: str = "test",
) -> ScrapePlan:
    """Create a ScrapePlan for testing."""
    return ScrapePlan(
        target_key=target_key,
        target_label="U14 Homegrown Northeast",
        action=action,
        start_date=date(2026, 3, 20),
        end_date=date(2026, 6, 30),
        reason=reason,
        scraper_params={
            "age_group": "U14",
            "league": "Homegrown",
            "division": "Northeast",
        },
    )


def _make_journal_entry(
    target: str = "U14 HG Northeast",
    matches_found: int = 10,
    missing_scores: int = 0,
    errors: int = 0,
) -> TargetEntry:
    return TargetEntry(
        target=target,
        matches_found=matches_found,
        missing_scores=missing_scores,
        errors=errors,
    )


class TestAllScoredSkip:
    def test_downgrades_score_sync_when_all_scored(self) -> None:
        sp = _make_plan(action=ScrapeAction.SCORE_SYNC)
        entry = _make_journal_entry(missing_scores=0, matches_found=10)
        result = all_scored_skip(sp, entry)
        assert result.action == ScrapeAction.SKIP
        assert "All scored last run" in result.reason

    def test_no_change_when_missing_scores(self) -> None:
        sp = _make_plan(action=ScrapeAction.SCORE_SYNC)
        entry = _make_journal_entry(missing_scores=3)
        result = all_scored_skip(sp, entry)
        assert result.action == ScrapeAction.SCORE_SYNC

    def test_no_change_for_full_sync(self) -> None:
        sp = _make_plan(action=ScrapeAction.FULL_SYNC)
        entry = _make_journal_entry(missing_scores=0)
        result = all_scored_skip(sp, entry)
        assert result.action == ScrapeAction.FULL_SYNC

    def test_no_change_when_no_entry(self) -> None:
        sp = _make_plan(action=ScrapeAction.SCORE_SYNC)
        result = all_scored_skip(sp, None)
        assert result.action == ScrapeAction.SCORE_SYNC


class TestErrorRetry:
    def test_upgrades_skip_to_full_sync_on_errors(self) -> None:
        sp = _make_plan(action=ScrapeAction.SKIP, reason="Up to date")
        entry = _make_journal_entry(errors=2)
        result = error_retry(sp, entry)
        assert result.action == ScrapeAction.FULL_SYNC
        assert "Retrying after 2 error(s)" in result.reason

    def test_no_change_when_no_errors(self) -> None:
        sp = _make_plan(action=ScrapeAction.SKIP)
        entry = _make_journal_entry(errors=0)
        result = error_retry(sp, entry)
        assert result.action == ScrapeAction.SKIP

    def test_no_change_for_non_skip(self) -> None:
        sp = _make_plan(action=ScrapeAction.FULL_SYNC)
        entry = _make_journal_entry(errors=2)
        result = error_retry(sp, entry)
        assert result.action == ScrapeAction.FULL_SYNC

    def test_no_change_when_no_entry(self) -> None:
        sp = _make_plan(action=ScrapeAction.SKIP)
        result = error_retry(sp, None)
        assert result.action == ScrapeAction.SKIP


class TestApplyModifiers:
    def test_returns_plan_unchanged_without_journal(self) -> None:
        plan = RunPlan(plans=[_make_plan()])
        result = apply_modifiers(plan, None)
        assert result.plans[0].action == ScrapeAction.FULL_SYNC

    def test_applies_all_scored_skip(self) -> None:
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.SCORE_SYNC)])
        journal = RunJournal(
            timestamp="2026-03-20T00:00:00Z",
            run_id="test-123",
            targets=[_make_journal_entry(missing_scores=0)],
        )
        result = apply_modifiers(plan, journal)
        assert result.plans[0].action == ScrapeAction.SKIP

    def test_applies_error_retry(self) -> None:
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.SKIP)])
        journal = RunJournal(
            timestamp="2026-03-20T00:00:00Z",
            run_id="test-123",
            targets=[_make_journal_entry(errors=1)],
        )
        result = apply_modifiers(plan, journal)
        assert result.plans[0].action == ScrapeAction.FULL_SYNC


class TestExecutePlan:
    def test_skip_action_produces_skip_entry(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.SKIP, reason="Up to date")])
        result = asyncio.run(execute_plan(plan, ctx))
        assert isinstance(result, AgentResult)
        assert len(result.actions) == 1
        assert result.actions[0].action == "skip"
        assert result.matches_found == 0

    def test_full_sync_scrapes_and_submits(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.FULL_SYNC)])

        mock_scrape = AsyncMock(return_value="Found 5 matches")
        mock_submit = AsyncMock(return_value="Submitted 5 matches to queue (0 errors).")

        def scrape_side_effect(ctx_arg, **kwargs):
            ctx_arg._scraped_matches = [
                {"home_team": f"T{i}", "away_team": f"T{i + 1}"} for i in range(5)
            ]
            return mock_scrape(ctx_arg, **kwargs)

        with (
            patch("agent.engine.scrape_matches", side_effect=scrape_side_effect),
            patch("agent.engine.submit_matches", mock_submit),
        ):
            result = asyncio.run(execute_plan(plan, ctx))

        assert result.matches_found == 5
        assert len(result.actions) == 2  # scrape + submit
        assert result.actions[0].action == "scrape"
        assert result.actions[1].action == "submit"

    def test_dry_run_flag_propagated(self) -> None:
        ctx = _make_ctx(dry_run=True)
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.FULL_SYNC)])

        mock_scrape = AsyncMock(return_value="Found 1 match")
        mock_submit = AsyncMock(return_value="[DRY RUN] Would submit 1 matches to queue.")

        def scrape_side_effect(ctx_arg, **kwargs):
            ctx_arg._scraped_matches = [{"home_team": "A", "away_team": "B"}]
            return mock_scrape(ctx_arg, **kwargs)

        with (
            patch("agent.engine.scrape_matches", side_effect=scrape_side_effect),
            patch("agent.engine.submit_matches", mock_submit),
        ):
            result = asyncio.run(execute_plan(plan, ctx))

        assert result.actions[0].dry_run is True


class TestRunPipeline:
    def test_end_to_end_with_skip(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.SKIP, reason="Up to date")])
        result = asyncio.run(run_pipeline(plan, ctx, journal=None))
        assert isinstance(result, AgentResult)
        assert result.matches_found == 0
        assert len(result.actions) == 1
        assert result.actions[0].action == "skip"

    def test_modifiers_applied_with_journal(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.SCORE_SYNC)])
        journal = RunJournal(
            timestamp="2026-03-20T00:00:00Z",
            run_id="test-123",
            targets=[_make_journal_entry(missing_scores=0)],
        )
        result = asyncio.run(run_pipeline(plan, ctx, journal))
        # Should have been downgraded to SKIP by all_scored_skip modifier
        assert result.actions[0].action == "skip"
        assert "All scored last run" in result.actions[0].detail
