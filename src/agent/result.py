"""Structured output models for the match-scraper-agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AgentAction(BaseModel):
    """A tool action taken by the agent during a run."""

    action: Literal["scrape", "submit", "skip"]
    detail: str
    dry_run: bool = False


class AgentResult(BaseModel):
    """Structured result returned by the match-scraper-agent."""

    summary: str
    actions: list[AgentAction] = []
    matches_found: int = 0
    matches_submitted: int = 0
