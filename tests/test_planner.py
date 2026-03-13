"""Tests for the day-of-week aware scrape planner."""

from __future__ import annotations

from datetime import UTC, date, datetime

from agent.planner import (
    RunPlan,
    ScrapeAction,
    ScrapePlan,
    _weekend_window,
    compute_scrape_plan,
    format_plan_prompt,
)

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
    last_played_date: str | None = None,
) -> dict:
    return {
        "age_group": age_group,
        "league": league,
        "division": division,
        "total": total,
        "needs_score": needs_score,
        "by_status": {"scheduled": total},
        "date_range": {"earliest": "2026-03-01", "latest": "2026-06-28"},
        "last_played_date": last_played_date,
    }


class TestWeekendWindow:
    def test_monday(self):
        fri, mon = _weekend_window(date(2026, 3, 9))  # Monday
        assert fri == date(2026, 3, 6)  # Last Friday
        assert mon == date(2026, 3, 9)  # Today (Monday)

    def test_tuesday(self):
        fri, mon = _weekend_window(date(2026, 3, 10))  # Tuesday
        assert fri == date(2026, 3, 6)
        assert mon == date(2026, 3, 9)

    def test_wednesday(self):
        fri, mon = _weekend_window(date(2026, 3, 11))  # Wednesday
        assert fri == date(2026, 3, 6)
        assert mon == date(2026, 3, 9)

    def test_thursday(self):
        fri, mon = _weekend_window(date(2026, 3, 12))  # Thursday
        assert fri == date(2026, 3, 13)  # Upcoming Friday
        assert mon == date(2026, 3, 16)  # Upcoming Monday

    def test_friday(self):
        fri, mon = _weekend_window(date(2026, 3, 13))  # Friday
        assert fri == date(2026, 3, 13)  # Today
        assert mon == date(2026, 3, 16)

    def test_saturday(self):
        fri, mon = _weekend_window(date(2026, 3, 14))  # Saturday
        assert fri == date(2026, 3, 13)  # Yesterday (Friday)
        assert mon == date(2026, 3, 16)

    def test_sunday(self):
        fri, mon = _weekend_window(date(2026, 3, 15))  # Sunday
        assert fri == date(2026, 3, 13)
        assert mon == date(2026, 3, 16)


class TestComputeScrapePlan:
    """Test day-of-week scrape planning logic."""

    def _all_scored_mt(self):
        return [
            _mt_target("U14", "Homegrown", "Northeast", total=105, last_played_date="2026-03-08"),
            _mt_target("U13", "Homegrown", "Northeast", total=100, last_played_date="2026-03-08"),
            _mt_target("U14", "Academy", "New England", total=99, last_played_date="2026-03-07"),
        ]

    def _some_unscored_mt(self):
        return [
            _mt_target(
                "U14",
                "Homegrown",
                "Northeast",
                total=105,
                needs_score=3,
                last_played_date="2026-03-08",
            ),
            _mt_target("U13", "Homegrown", "Northeast", total=100, last_played_date="2026-03-08"),
            _mt_target("U14", "Academy", "New England", total=99, last_played_date="2026-03-07"),
        ]

    # --- Mon-Wed: score focus ---

    def test_monday_all_scored_skips(self):
        now = datetime(2026, 3, 9, 8, 0, tzinfo=UTC)  # Monday 08:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)

        assert len(plan.plans) == 3  # excludes IFA
        for p in plan.plans:
            assert p.action == ScrapeAction.SKIP
            assert "Up to date" in p.reason

    def test_tuesday_needs_score_triggers_score_sync(self):
        now = datetime(2026, 3, 10, 14, 0, tzinfo=UTC)  # Tuesday 14:00
        plan = compute_scrape_plan(self._some_unscored_mt(), SAMPLE_CONFIGS, now)

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.SCORE_SYNC
        assert u14.start_date == date(2026, 3, 6)  # Friday
        assert u14.end_date == date(2026, 3, 9)  # Monday
        assert "3 match(es) awaiting scores" in u14.reason

        # Others are up to date → skip
        u13 = next(p for p in plan.plans if p.target_key == "u13-hg")
        assert u13.action == ScrapeAction.SKIP

    def test_wednesday_all_scored_skips(self):
        now = datetime(2026, 3, 11, 2, 0, tzinfo=UTC)  # Wednesday 02:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.SKIP

    # --- Thu-Fri: schedule check ---

    def test_thursday_before_16_utc_skips(self):
        now = datetime(2026, 3, 12, 8, 0, tzinfo=UTC)  # Thursday 08:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.SKIP
            assert "16" in p.reason

    def test_thursday_after_16_utc_schedule_check(self):
        now = datetime(2026, 3, 12, 20, 0, tzinfo=UTC)  # Thursday 20:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.SCHEDULE_CHECK
            assert p.start_date == date(2026, 3, 13)  # Upcoming Friday
            assert p.end_date == date(2026, 3, 16)  # Upcoming Monday

    def test_friday_at_16_utc_schedule_check(self):
        now = datetime(2026, 3, 13, 16, 0, tzinfo=UTC)  # Friday 16:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.SCHEDULE_CHECK
            assert p.start_date == date(2026, 3, 13)  # Today (Friday)
            assert p.end_date == date(2026, 3, 16)

    def test_friday_before_16_utc_skips(self):
        now = datetime(2026, 3, 13, 14, 0, tzinfo=UTC)  # Friday 14:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.SKIP

    # --- Sat-Sun: always score sync ---

    def test_saturday_always_score_syncs(self):
        now = datetime(2026, 3, 14, 14, 0, tzinfo=UTC)  # Saturday 14:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.SCORE_SYNC
            assert p.start_date == date(2026, 3, 13)  # Friday
            assert p.end_date == date(2026, 3, 16)  # Monday

    def test_sunday_always_score_syncs(self):
        now = datetime(2026, 3, 15, 8, 0, tzinfo=UTC)  # Sunday 08:00
        plan = compute_scrape_plan(self._all_scored_mt(), SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.SCORE_SYNC

    # --- Edge cases ---

    def test_missing_from_mt_triggers_full_sync(self):
        mt_targets = [
            _mt_target("U14", "Homegrown", "Northeast", total=105, last_played_date="2026-03-08"),
        ]
        now = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)  # Tuesday
        plan = compute_scrape_plan(mt_targets, SAMPLE_CONFIGS, now)

        u13 = next(p for p in plan.plans if p.target_key == "u13-hg")
        assert u13.action == ScrapeAction.FULL_SYNC
        assert "No matches in MT" in u13.reason
        assert u13.start_date == date(2026, 3, 6)  # Uses weekend window
        assert u13.end_date == date(2026, 3, 9)

    def test_zero_total_triggers_full_sync(self):
        mt_targets = [
            _mt_target("U14", "Homegrown", "Northeast", total=0),
        ]
        now = datetime(2026, 3, 14, 8, 0, tzinfo=UTC)  # Saturday
        plan = compute_scrape_plan(mt_targets, SAMPLE_CONFIGS, now)

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.FULL_SYNC

    def test_empty_mt_response_full_sync_all(self):
        now = datetime(2026, 3, 12, 8, 0, tzinfo=UTC)  # Thursday
        plan = compute_scrape_plan([], SAMPLE_CONFIGS, now)
        for p in plan.plans:
            assert p.action == ScrapeAction.FULL_SYNC

    def test_ifa_targets_excluded(self):
        now = datetime(2026, 3, 12, 8, 0, tzinfo=UTC)
        plan = compute_scrape_plan([], SAMPLE_CONFIGS, now)
        keys = [p.target_key for p in plan.plans]
        assert "u14-hg-ifa" not in keys

    def test_academy_conference_mapping(self):
        mt_targets = [
            _mt_target("U14", "Academy", "New England", total=99, last_played_date="2026-03-07"),
        ]
        now = datetime(2026, 3, 9, 8, 0, tzinfo=UTC)  # Monday
        plan = compute_scrape_plan(mt_targets, SAMPLE_CONFIGS, now)

        academy = next(p for p in plan.plans if p.target_key == "u14-academy")
        assert academy.action == ScrapeAction.SKIP


class TestFormatPlanPrompt:
    def test_contains_scrape_params(self):
        plan = RunPlan(
            plans=[
                ScrapePlan(
                    target_key="u14-hg",
                    target_label="U14 Homegrown Northeast",
                    action=ScrapeAction.SCORE_SYNC,
                    start_date=date(2026, 3, 6),
                    end_date=date(2026, 3, 9),
                    reason="3 match(es) awaiting scores",
                    scraper_params={
                        "age_group": "U14",
                        "league": "Homegrown",
                        "division": "Northeast",
                    },
                ),
            ]
        )
        prompt = format_plan_prompt(plan)
        assert 'age_group="U14"' in prompt
        assert 'division="Northeast"' in prompt
        assert "scrape_matches(" in prompt
        assert "submit_matches()" in prompt
        assert "SCORE SYNC" in prompt

    def test_schedule_check_format(self):
        plan = RunPlan(
            plans=[
                ScrapePlan(
                    target_key="u14-hg",
                    target_label="U14 Homegrown Northeast",
                    action=ScrapeAction.SCHEDULE_CHECK,
                    start_date=date(2026, 3, 13),
                    end_date=date(2026, 3, 16),
                    reason="Checking upcoming weekend schedule",
                    scraper_params={
                        "age_group": "U14",
                        "league": "Homegrown",
                        "division": "Northeast",
                    },
                ),
            ]
        )
        prompt = format_plan_prompt(plan)
        assert "SCHEDULE CHECK" in prompt
        assert "2026-03-13" in prompt
        assert "2026-03-16" in prompt

    def test_skip_shows_reason(self):
        plan = RunPlan(
            plans=[
                ScrapePlan(
                    target_key="u14-hg",
                    target_label="U14 Homegrown Northeast",
                    action=ScrapeAction.SKIP,
                    reason="Up to date (105 matches)",
                    scraper_params={},
                ),
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
