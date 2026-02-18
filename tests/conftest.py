"""Shared test fixtures for match-scraper-agent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config.settings import AgentSettings


@pytest.fixture
def settings() -> AgentSettings:
    """Return default AgentSettings for testing."""
    return AgentSettings()


@pytest.fixture
def mock_queue_client() -> MagicMock:
    """Return a mocked MatchQueueClient."""
    client = MagicMock()
    client.submit_match.return_value = "task-id-123"
    client.check_connection.return_value = True
    return client
