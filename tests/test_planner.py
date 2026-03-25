"""Tests for the deterministic scrape planner."""

from __future__ import annotations

from datetime import date, timedelta

from agent.planner import (
    _KICKOFF_LOOKAHEAD_DAYS,
    ScrapeAction,
    _match_weekend_window,
    compute_scrape_plan,
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


class TestMatchWeekendWindow:
    def test_on_friday(self):
        # Friday Mar 13 → last Fri Mar 6, this Mon Mar 16
        fri, mon = _match_weekend_window(date(2026, 3, 13))
        assert fri == date(2026, 3, 6)
        assert mon == date(2026, 3, 16)

    def test_on_saturday(self):
        # Saturday Mar 14 → last Fri Mar 13, this Mon Mar 23
        # (this Friday is Mar 20)
        fri, mon = _match_weekend_window(date(2026, 3, 14))
        assert fri == date(2026, 3, 13)
        assert mon == date(2026, 3, 23)

    def test_on_monday(self):
        # Monday Mar 16 → last Fri Mar 13, this Mon Mar 23
        fri, mon = _match_weekend_window(date(2026, 3, 16))
        assert fri == date(2026, 3, 13)
        assert mon == date(2026, 3, 23)

    def test_on_wednesday(self):
        # Wednesday Mar 18 → last Fri Mar 13, this Mon Mar 23
        fri, mon = _match_weekend_window(date(2026, 3, 18))
        assert fri == date(2026, 3, 13)
        assert mon == date(2026, 3, 23)

    def test_on_thursday(self):
        # Thursday Mar 19 → last Fri Mar 13, this Mon Mar 23
        fri, mon = _match_weekend_window(date(2026, 3, 19))
        assert fri == date(2026, 3, 13)
        assert mon == date(2026, 3, 23)


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
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.SCORE_SYNC
        assert u14.start_date == date(2026, 3, 6)  # Last Friday
        assert u14.end_date == date(2026, 3, 16)  # Monday after this weekend
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
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.FULL_SYNC

    def test_empty_mt_response_full_sync_all(self):
        plan = compute_scrape_plan([], SAMPLE_CONFIGS, date(2026, 3, 12), SEASON_END)

        for p in plan.plans:
            assert p.action == ScrapeAction.FULL_SYNC

    def test_ifa_targets_excluded(self):
        plan = compute_scrape_plan([], SAMPLE_CONFIGS, date(2026, 3, 12), SEASON_END)
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
        )

        u14 = next(p for p in plan.plans if p.target_key == "u14-hg")
        assert u14.action == ScrapeAction.SKIP
