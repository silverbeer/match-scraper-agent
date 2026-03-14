"""Tests for the deterministic scrape planner."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from agent.planner import (
    _KICKOFF_LOOKAHEAD_DAYS,
    _match_weekend_window,
    RunPlan,
    ScrapeAction,
    compute_scrape_plan,
    format_plan_prompt,
    is_weekly_sync_run,
)

SEASON_END = date(2026, 6, 30)

# Minimal target configs (mirrors _TARGET_SCRAPER_CONFIG without IFA entries)
SAMPLE_CONFIGS = {
    "u14-hg": {"age_group": "U14", "league": "Homegrown", "division": "Northeast"},
    "u13-hg": {"age_group": "U13", "league": "Homegrown", "division": "Northeast"},
    "u14-academy": {"age_group": "U14", "league": "Academy", "conference": "New England"},
    # IFA targets should be filtered out by the planner
    "u14-hg-ifa": {"age_group": "U14", "league": "Homegrown", "division": "Northeast"},
}


def _mt_target(
    age_group: str,
    league: str,
    division: str,
    total: int = 100,
    needs_score: int = 0,
    needs_kickoff: int = 0,
    last_played_date: str | None = None,
) -> dict:
    return {
        "age_group": age_group,
        "league": league,
        "division": division,
        "total": total,
        "needs_score": needs_score,
        "needs_kickoff": needs_kickoff,
        "by_status": {"scheduled": total},
        "date_range": {"earliest": "2026-03-01", "latest": "2026-06-28"},
        "last_played_date": last_played_date,
    }


class TestIsWeeklySyncRun:
    def test_monday_0200_utc(self):
        # Monday March 9 2026 02:00 UTC
        dt = datetime(2026, 3, 9, 2, 0, tzinfo=UTC)
        assert is_weekly_sync_run(dt) is True

    def test_monday_0300_utc(self):
        dt = datetime(2026, 3, 9, 3, 0, tzinfo=UTC)
        assert is_weekly_sync_run(dt) is True

    def test_monday_0800_utc(self):
        dt = datetime(2026, 3, 9, 8, 0, tzinfo=UTC)
        assert is_weekly_sync_run(dt) is False

    def test_tuesday_0200_utc(self):
        dt = datetime(2026, 3, 10, 2, 0, tzinfo=UTC)
        assert is_weekly_sync_run(dt) is False

    def test_thursday_1400_utc(self):
        dt = datetime(2026, 3, 12, 14, 0, tzinfo=UTC)
        assert is_weekly_sync_run(dt) is False


class TestMatchWeekendWindow:
    def test_on_saturday(self):
        # Saturday Mar 7 → window is Fri Mar 6 – Mon Mar 9
        fri, mon = _match_weekend_window(date(2026, 3, 7))
        assert fri == date(2026, 3, 6)
        assert mon == date(2026, 3, 9)

    def test_on_monday(self):
        # Monday Mar 9 → window is Fri Mar 6 – Mon Mar 9
        fri, mon = _match_weekend_window(date(2026, 3, 9))
        assert fri == date(2026, 3, 6)
        assert mon == date(2026, 3, 9)

    def test_on_wednesday(self):
        # Wednesday Mar 11 → window is Fri Mar 6 – Mon Mar 9
        fri, mon = _match_weekend_window(date(2026, 3, 11))
        assert fri == date(2026, 3, 6)
        assert mon == date(2026, 3, 9)

    def test_on_friday(self):
        # Friday Mar 13 → window is Fri Mar 13 – Mon Mar 16
        fri, mon = _match_weekend_window(date(2026, 3, 13))
        assert fri == date(2026, 3, 13)
        assert mon == date(2026, 3, 16)

    def test_on_thursday(self):
        # Thursday Mar 12 → window is Fri Mar 6 – Mon Mar 9
        fri, mon = _match_weekend_window(date(2026, 3, 12))
        assert fri == date(2026, 3, 6)
        assert mon == date(2026, 3, 9)


class TestComputeScrapePlan:
    def test_all_targets_up_to_date_skips(self):
        mt_targets = [
            _mt_target("U14", "Homegrown", "Northeast", total=105, last_played_date="2026-03-08"),
            _mt_target("U13", "Homegrown", "Northeast", total=100, last_played_date="2026-03-08"),
            _mt_target("U14", "Academy", "New England", total=99, last_played_date="2026-03-07"),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        assert len(plan.plans) == 3  # excludes u14-hg-ifa
        for p in plan.plans:
            assert p.action == ScrapeAction.SKIP
            assert "Up to date" in p.reason

    def test_needs_score_triggers_score_sync(self):
        mt_targets = [
            _mt_target(
                "U14",
                "Homegrown",
                "Northeast",
                total=105,
                needs_score=3,
                last_played_date="2026-03-08",
            ),
            _mt_target("U13", "Homegrown", "Northeast", total=100, last_played_date="2026-03-08"),
            _mt_target("U14", "Academy", "New England", total=99),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.SCORE_SYNC
        assert u14.start_date == date(2026, 3, 6)  # Friday of match weekend
        assert u14.end_date == date(2026, 3, 9)  # Monday after match weekend
        assert "3 match(es) awaiting scores" in u14.reason

    def test_missing_from_mt_triggers_full_sync(self):
        # Only U14 HG exists in MT, U13 and Academy are missing
        mt_targets = [
            _mt_target("U14", "Homegrown", "Northeast", total=105, last_played_date="2026-03-08"),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        u13 = next(p for p in plan.plans if p.target_key == "u13-hg")
        assert u13.action == ScrapeAction.FULL_SYNC
        assert "No matches in MT" in u13.reason

        academy = next(p for p in plan.plans if p.target_key == "u14-academy")
        assert academy.action == ScrapeAction.FULL_SYNC

    def test_zero_total_triggers_full_sync(self):
        mt_targets = [
            _mt_target("U14", "Homegrown", "Northeast", total=0),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.FULL_SYNC

    def test_weekly_sync_overrides_all(self):
        mt_targets = [
            _mt_target("U14", "Homegrown", "Northeast", total=105, last_played_date="2026-03-08"),
            _mt_target("U13", "Homegrown", "Northeast", total=100, last_played_date="2026-03-08"),
            _mt_target("U14", "Academy", "New England", total=99, last_played_date="2026-03-07"),
        ]
        plan = compute_scrape_plan(mt_targets, SAMPLE_CONFIGS, date(2026, 3, 9), SEASON_END, True)

        assert plan.is_weekly_sync is True
        for p in plan.plans:
            assert p.action == ScrapeAction.FULL_SYNC
            assert "Weekly" in p.reason

    def test_empty_mt_response_full_sync_all(self):
        plan = compute_scrape_plan([], SAMPLE_CONFIGS, date(2026, 3, 12), SEASON_END, False)

        for p in plan.plans:
            assert p.action == ScrapeAction.FULL_SYNC

    def test_ifa_targets_excluded(self):
        plan = compute_scrape_plan([], SAMPLE_CONFIGS, date(2026, 3, 12), SEASON_END, False)
        keys = [p.target_key for p in plan.plans]
        assert "u14-hg-ifa" not in keys

    def test_academy_conference_mapping(self):
        """Academy targets use conference in config but division in MT response."""
        mt_targets = [
            _mt_target("U14", "Academy", "New England", total=99, last_played_date="2026-03-07"),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        academy = next(p for p in plan.plans if p.target_key == "u14-academy")
        assert academy.action == ScrapeAction.SKIP

    def test_needs_kickoff_triggers_kickoff_sync(self):
        mt_targets = [
            _mt_target(
                "U14",
                "Homegrown",
                "Northeast",
                total=105,
                needs_score=0,
                needs_kickoff=3,
                last_played_date="2026-03-08",
            ),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.KICKOFF_SYNC
        assert "3 match(es) missing kick-off time" in u14.reason

    def test_kickoff_sync_date_range(self):
        today = date(2026, 3, 12)
        mt_targets = [
            _mt_target(
                "U14",
                "Homegrown",
                "Northeast",
                total=105,
                needs_kickoff=2,
                last_played_date="2026-03-08",
            ),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            today,
            SEASON_END,
            False,
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.start_date == today
        assert u14.end_date == today + timedelta(days=_KICKOFF_LOOKAHEAD_DAYS)

    def test_needs_score_and_kickoff_merges(self):
        mt_targets = [
            _mt_target(
                "U14",
                "Homegrown",
                "Northeast",
                total=105,
                needs_score=3,
                needs_kickoff=2,
                last_played_date="2026-03-08",
            ),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.SCORE_SYNC
        assert "awaiting scores" in u14.reason
        assert "missing kick-off" in u14.reason

    def test_needs_kickoff_zero_skips(self):
        mt_targets = [
            _mt_target(
                "U14",
                "Homegrown",
                "Northeast",
                total=105,
                needs_score=0,
                needs_kickoff=0,
                last_played_date="2026-03-08",
            ),
        ]
        plan = compute_scrape_plan(
            mt_targets,
            SAMPLE_CONFIGS,
            date(2026, 3, 12),
            SEASON_END,
            False,
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.SKIP


class TestFormatPlanPrompt:
    def test_contains_scrape_params(self):
        plan = RunPlan(
            plans=[
                {
                    "target_key": "u14-hg",
                    "target_label": "U14 Homegrown Northeast",
                    "action": ScrapeAction.FULL_SYNC,
                    "start_date": date(2026, 3, 12),
                    "end_date": date(2026, 6, 30),
                    "reason": "No matches in MT",
                    "scraper_params": {
                        "age_group": "U14",
                        "league": "Homegrown",
                        "division": "Northeast",
                    },
                },
            ]
        )
        prompt = format_plan_prompt(plan)
        assert 'age_group="U14"' in prompt
        assert 'division="Northeast"' in prompt
        assert "scrape_matches(" in prompt
        assert "submit_matches()" in prompt

    def test_skip_shows_reason(self):
        plan = RunPlan(
            plans=[
                {
                    "target_key": "u14-hg",
                    "target_label": "U14 Homegrown Northeast",
                    "action": ScrapeAction.SKIP,
                    "reason": "Up to date (105 matches)",
                    "scraper_params": {},
                },
            ]
        )
        prompt = format_plan_prompt(plan)
        assert "SKIP" in prompt
        assert "Up to date" in prompt
        assert "scrape_matches" not in prompt

    def test_do_not_call_get_match_status(self):
        plan = RunPlan(plans=[])
        prompt = format_plan_prompt(plan)
        assert "Do NOT call get_match_status()" in prompt

    def test_kickoff_sync_format(self):
        plan = RunPlan(
            plans=[
                {
                    "target_key": "u14-hg",
                    "target_label": "U14 Homegrown Northeast",
                    "action": ScrapeAction.KICKOFF_SYNC,
                    "start_date": date(2026, 3, 12),
                    "end_date": date(2026, 3, 26),
                    "reason": "3 match(es) missing kick-off time",
                    "scraper_params": {
                        "age_group": "U14",
                        "league": "Homegrown",
                        "division": "Northeast",
                    },
                },
            ]
        )
        prompt = format_plan_prompt(plan)
        assert "KICKOFF SYNC" in prompt
        assert "scrape_matches(" in prompt
