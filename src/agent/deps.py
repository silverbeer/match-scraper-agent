"""Run context — carries state through the pipeline engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.celery.queue_client import MatchQueueClient


@dataclass
class RunContext:
    """Carries queue client, scraper config, and dry_run flag into tool calls.

    The _scraped_matches list is inter-tool state: scrape_matches populates it,
    submit_matches reads from it.
    """

    queue_client: MatchQueueClient
    missing_table_api_url: str = "http://localhost:8000"
    missing_table_api_key: str = ""
    dry_run: bool = False
    headless: bool = True
    team_filter: str = ""
    # Default scraper params (used by tools when not overridden per-call)
    age_group: str = "U14"
    league: str = "Homegrown"
    division: str = "Northeast"
    _scraped_matches: list[dict[str, Any]] = field(default_factory=list)
    _submission_errors: list[dict[str, str]] = field(default_factory=list)
    _protected_matches: list[dict[str, Any]] = field(default_factory=list)
    _mt_status: str = ""  # "ok", "failed:<reason>", or "" (not called)
    _scrape_plan: Any = None  # RunPlan from planner, or None for targeted runs
