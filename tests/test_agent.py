"""Tests for the pipeline engine, modifiers, and run_pipeline."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from agent.deps import RunContext
from agent.engine import (
    _filter_live_scored_protection,
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


class TestFilterLiveScoredProtection:
    def test_unscored_match_is_protected(self) -> None:
        matches = [
            {"home_team": "IFA", "away_team": "Team B", "home_score": None, "away_score": None,
             "match_status": "scheduled"},
        ]
        to_submit, protected = _filter_live_scored_protection(matches)
        assert to_submit == []
        assert len(protected) == 1
        assert protected[0]["home_team"] == "IFA"

    def test_scored_match_is_submitted(self) -> None:
        matches = [
            {"home_team": "IFA", "away_team": "Team B", "home_score": 2, "away_score": 1,
             "match_status": "completed"},
        ]
        to_submit, protected = _filter_live_scored_protection(matches)
        assert len(to_submit) == 1
        assert protected == []

    def test_mixed_matches_split_correctly(self) -> None:
        matches = [
            {"home_team": "A", "away_team": "B", "home_score": 2, "away_score": 0,
             "match_status": "completed"},
            {"home_team": "C", "away_team": "D", "home_score": None, "away_score": None,
             "match_status": "scheduled"},
            {"home_team": "E", "away_team": "F", "home_score": 1, "away_score": 1,
             "match_status": "completed"},
        ]
        to_submit, protected = _filter_live_scored_protection(matches)
        assert len(to_submit) == 2
        assert len(protected) == 1
        assert protected[0]["home_team"] == "C"

    def test_zero_score_match_is_submitted(self) -> None:
        """A 0-0 draw has home_score=0, which is not None — should be submitted."""
        matches = [
            {"home_team": "A", "away_team": "B", "home_score": 0, "away_score": 0,
             "match_status": "completed"},
        ]
        to_submit, protected = _filter_live_scored_protection(matches)
        assert len(to_submit) == 1
        assert protected == []

    def test_empty_input_returns_empty_lists(self) -> None:
        to_submit, protected = _filter_live_scored_protection([])
        assert to_submit == []
        assert protected == []


class TestScoreSyncProtection:
    """Integration tests: SCORE_SYNC withholds unscored matches, FULL_SYNC does not."""

    def test_score_sync_protects_unscored_matches(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.SCORE_SYNC)])

        mock_scrape = AsyncMock(return_value="Found 2 matches")
        mock_submit = AsyncMock(return_value="Submitted 1 matches to queue (0 errors).")

        def scrape_side_effect(ctx_arg, **kwargs):
            ctx_arg._scraped_matches = [
                {"home_team": "IFA", "away_team": "B", "home_score": None, "away_score": None,
                 "match_status": "scheduled"},
                {"home_team": "C", "away_team": "D", "home_score": 2, "away_score": 1,
                 "match_status": "completed"},
            ]
            return mock_scrape(ctx_arg, **kwargs)

        with (
            patch("agent.engine.scrape_matches", side_effect=scrape_side_effect),
            patch("agent.engine.submit_matches", mock_submit),
        ):
            result = asyncio.run(execute_plan(plan, ctx))

        # Only the scored match should be submitted
        assert len(ctx._scraped_matches) == 1
        assert ctx._scraped_matches[0]["home_team"] == "C"
        # The unscored match should be protected
        assert len(ctx._protected_matches) == 1
        assert ctx._protected_matches[0]["home_team"] == "IFA"
        assert result.matches_found == 2  # total_found counts before filter

    def test_score_sync_all_unscored_skips_submit(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.SCORE_SYNC)])

        mock_scrape = AsyncMock(return_value="Found 1 match")
        mock_submit = AsyncMock(return_value="Submitted 0 matches")

        def scrape_side_effect(ctx_arg, **kwargs):
            ctx_arg._scraped_matches = [
                {"home_team": "IFA", "away_team": "B", "home_score": None, "away_score": None,
                 "match_status": "scheduled"},
            ]
            return mock_scrape(ctx_arg, **kwargs)

        with (
            patch("agent.engine.scrape_matches", side_effect=scrape_side_effect),
            patch("agent.engine.submit_matches", mock_submit) as mock_sub,
        ):
            asyncio.run(execute_plan(plan, ctx))

        # submit_matches should NOT be called when nothing passes the filter
        mock_sub.assert_not_called()
        assert len(ctx._protected_matches) == 1

    def test_full_sync_does_not_filter_unscored_matches(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(plans=[_make_plan(action=ScrapeAction.FULL_SYNC)])

        mock_scrape = AsyncMock(return_value="Found 1 match")
        mock_submit = AsyncMock(return_value="Submitted 1 matches to queue (0 errors).")

        def scrape_side_effect(ctx_arg, **kwargs):
            ctx_arg._scraped_matches = [
                {"home_team": "A", "away_team": "B", "home_score": None, "away_score": None,
                 "match_status": "scheduled"},
            ]
            return mock_scrape(ctx_arg, **kwargs)

        with (
            patch("agent.engine.scrape_matches", side_effect=scrape_side_effect),
            patch("agent.engine.submit_matches", mock_submit),
        ):
            asyncio.run(execute_plan(plan, ctx))

        # FULL_SYNC: unscored matches still submitted, nothing protected
        assert len(ctx._protected_matches) == 0
        mock_submit.assert_called_once()

    def test_protected_matches_accumulate_across_targets(self) -> None:
        ctx = _make_ctx()
        plan = RunPlan(
            plans=[
                _make_plan(action=ScrapeAction.SCORE_SYNC, target_key="u14-hg"),
                ScrapePlan(
                    target_key="u15-hg",
                    target_label="U15 Homegrown Northeast",
                    action=ScrapeAction.SCORE_SYNC,
                    start_date=date(2026, 3, 20),
                    end_date=date(2026, 6, 30),
                    reason="test",
                    scraper_params={
                        "age_group": "U15",
                        "league": "Homegrown",
                        "division": "Northeast",
                    },
                ),
            ]
        )

        call_count = 0

        async def scrape_side_effect(ctx_arg, **kwargs):
            nonlocal call_count
            call_count += 1
            ctx_arg._scraped_matches = [
                {"home_team": f"Team{call_count}A", "away_team": f"Team{call_count}B",
                 "home_score": None, "away_score": None, "match_status": "scheduled"},
            ]
            return f"Found 1 match (call {call_count})"

        mock_submit = AsyncMock(return_value="Submitted 0 matches")

        with (
            patch("agent.engine.scrape_matches", side_effect=scrape_side_effect),
            patch("agent.engine.submit_matches", mock_submit),
        ):
            asyncio.run(execute_plan(plan, ctx))

        # Both targets contributed a protected match
        assert len(ctx._protected_matches) == 2


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
