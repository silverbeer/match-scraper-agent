"""Unit tests for the PydanticAI match agent with TestModel."""

from __future__ import annotations

from unittest.mock import MagicMock

from pydantic_ai.models.test import TestModel

from agent.core import create_agent
from agent.deps import AgentDeps
from agent.result import AgentResult
from config.settings import AgentSettings


def _make_deps() -> AgentDeps:
    """Create AgentDeps with a mocked queue client (not called during TestModel runs)."""
    queue_client = MagicMock()
    settings = AgentSettings()
    return AgentDeps(queue_client=queue_client, settings=settings, dry_run=True)


def _make_agent():
    """Create an agent from default settings."""
    settings = AgentSettings()
    return create_agent(settings)


class TestMatchAgent:
    def test_returns_agent_result(self) -> None:
        agent = _make_agent()
        result = agent.run_sync(
            "Review today's matches.",
            deps=_make_deps(),
            model=TestModel(call_tools=[]),
        )
        assert isinstance(result.output, AgentResult)
        assert isinstance(result.output.actions, list)

    def test_reports_usage(self) -> None:
        agent = _make_agent()
        result = agent.run_sync(
            "Review today's matches.",
            deps=_make_deps(),
            model=TestModel(call_tools=[]),
        )
        usage = result.usage()
        assert usage.requests >= 1

    def test_summary_is_string(self) -> None:
        agent = _make_agent()
        result = agent.run_sync(
            "Review today's matches.",
            deps=_make_deps(),
            model=TestModel(call_tools=[]),
        )
        assert isinstance(result.output.summary, str)

    def test_actions_default_empty(self) -> None:
        agent = _make_agent()
        result = agent.run_sync(
            "Review today's matches.",
            deps=_make_deps(),
            model=TestModel(call_tools=[]),
        )
        assert result.output.actions == []

    def test_matches_found_default_zero(self) -> None:
        agent = _make_agent()
        result = agent.run_sync(
            "Review today's matches.",
            deps=_make_deps(),
            model=TestModel(call_tools=[]),
        )
        assert result.output.matches_found == 0

    def test_matches_submitted_default_zero(self) -> None:
        agent = _make_agent()
        result = agent.run_sync(
            "Review today's matches.",
            deps=_make_deps(),
            model=TestModel(call_tools=[]),
        )
        assert result.output.matches_submitted == 0
