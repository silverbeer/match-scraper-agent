"""PydanticAI Agent factory for match-scraper-agent."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from agent.deps import AgentDeps
from agent.result import AgentResult
from agent.tools import get_today_info, scrape_matches, submit_matches
from config.settings import AgentSettings

AGENT_MD = Path(__file__).resolve().parents[2] / "agent.md"


def _load_system_prompt() -> str:
    """Load the system prompt from agent.md at the repo root."""
    if AGENT_MD.is_file():
        return AGENT_MD.read_text().strip()
    msg = f"agent.md not found at {AGENT_MD}"
    raise FileNotFoundError(msg)


def create_agent(settings: AgentSettings) -> Agent[AgentDeps, AgentResult]:
    """Create a PydanticAI agent configured with the given settings.

    Uses a factory function (not a module singleton) because base_url and
    model_name come from runtime configuration.

    Args:
        settings: Agent configuration with proxy URL, model name, and API key.

    Returns:
        A configured PydanticAI Agent instance.
    """
    if settings.proxy_enabled:
        provider = AnthropicProvider(
            base_url=settings.proxy_base_url,
            api_key=settings.anthropic_api_key,
        )
    else:
        provider = AnthropicProvider(api_key=settings.anthropic_api_key)
    model = AnthropicModel(settings.model_name, provider=provider)

    return Agent(
        model,
        output_type=AgentResult,
        deps_type=AgentDeps,
        system_prompt=_load_system_prompt(),
        tools=[get_today_info, scrape_matches, submit_matches],
        retries=1,
        max_concurrency=1,  # One Chromium at a time — fits pod resources, polite to MLS
    )
