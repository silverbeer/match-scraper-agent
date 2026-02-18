# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**match-scraper-agent** is an agentic match manager that uses PydanticAI to reason about youth soccer match operations. The agent routes all LLM calls through the iron-claw proxy for RADIUS-based cost monitoring and shells out to `mt-cli` for match data operations.

## Architecture

- **Agent framework:** PydanticAI with tool-calling pattern
- **LLM routing:** All calls go through iron-claw proxy (`ANTHROPIC_BASE_URL`)
- **Match operations:** Delegates to `mt-cli` via subprocess
- **Deployment:** K3s CronJob (daily at 14:00 UTC)
- **Default model:** `claude-haiku-4-5-20251001` (cheap, fast — fits small token budgets)

## Key Technology Choices

| Tool | Purpose |
|------|---------|
| Python >= 3.12 + uv | Package management |
| Typer | CLI framework (`match-scraper-agent run`, `check`) |
| PydanticAI | Agent framework with tool-calling |
| Pydantic v2 | Data models and settings |
| pydantic-settings | Environment-based configuration (AGENT_ prefix) |
| httpx | HTTP client for proxy health checks |
| structlog | Structured logging |
| Ruff | Linting and formatting |
| pytest | Test framework |

## Common Commands

### CLI
```bash
uv run match-scraper-agent run --dry-run     # Dry run (mutating tools log but skip execution)
uv run match-scraper-agent run --json-logs   # Production run with JSON logging
uv run match-scraper-agent check             # Verify proxy health + mt-cli availability
```

### Testing
```bash
cd tests && uv run pytest                    # All tests
cd tests && uv run pytest test_agent.py      # Agent tests only
cd tests && uv run pytest test_tools.py      # Tool tests only
cd tests && uv run pytest test_runner.py     # Runner tests only
cd tests && uv run pytest -k "test_dry"      # Filter by name
```

### Linting
```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Repo Layout

- `src/agent/` — PydanticAI agent factory, tools, deps, result model
- `src/cli/` — Typer CLI application (`run` and `check` commands)
- `src/config/` — pydantic-settings configuration (AGENT_ prefix)
- `src/mtcli/` — MtCliRunner subprocess wrapper for mt-cli
- `src/utils/` — structlog configuration
- `tests/` — pytest test suite with PydanticAI TestModel
- `k3s/` — K3s CronJob manifests
- `.github/workflows/` — CI pipelines

## Design Conventions

- Pydantic v2 for all data models (use `model_validate`, not `parse_obj`)
- `from __future__ import annotations` in all Python files
- Ruff for linting and formatting (line length 99)
- Type hints on all public functions
- No default exports; use explicit imports
- No "MLS" references — use "MT" or "match" throughout
- Agent uses factory function `create_agent(settings)`, not a module singleton
- All tools return `str` (human-readable summary for the LLM)
- Mutating tools respect `dry_run` flag from AgentDeps
