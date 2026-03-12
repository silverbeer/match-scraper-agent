"""Agent dependencies — injected into PydanticAI tool functions via RunContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.celery.queue_client import MatchQueueClient

    from config.settings import AgentSettings


@dataclass
class AgentDeps:
    """Carries queue client, scraper config, and dry_run flag into agent tool calls.

    The _scraped_matches list is inter-tool state: scrape_matches populates it,
    submit_matches reads from it.
    """

    queue_client: MatchQueueClient
    settings: AgentSettings
    dry_run: bool = False
    headless: bool = True
    team_filter: str = ""
    _scraped_matches: list[dict[str, Any]] = field(default_factory=list)
    _submission_errors: list[dict[str, str]] = field(default_factory=list)
    _mt_status: str = ""  # "ok", "failed:<reason>", or "" (not called)
    _scrape_plan: Any = None  # RunPlan from planner, or None for targeted runs
