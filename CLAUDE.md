# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**match-scraper-agent** is an agentic match data manager that uses PydanticAI to reason about youth soccer match scraping and submission. The LLM decides WHAT to do each run (agentic reasoning), while the proven `mls-match-scraper` library handles actual scraping (Playwright + CSS selectors) and match submission (Celery/RabbitMQ). All LLM calls route through the iron-claw proxy for RADIUS-based cost monitoring.

## Architecture

- **Agent framework:** PydanticAI with tool-calling pattern
- **LLM routing:** All calls go through iron-claw proxy (`ANTHROPIC_BASE_URL`)
- **Scraping:** `mls-match-scraper` library (Playwright + CSS selectors, zero LLM tokens)
- **Match submission:** `mls-match-scraper` MatchQueueClient → RabbitMQ → Celery workers
- **Deployment:** K3s CronJob (4x/day at 02:00, 08:00, 14:00, 20:00 UTC)
- **Default model:** `claude-haiku-4-5-20251001` (cheap, fast — fits small token budgets)

## Key Technology Choices

| Tool | Purpose |
|------|---------|
| Python >= 3.12 + uv | Package management |
| Typer | CLI framework (`match-scraper-agent run`, `check`) |
| PydanticAI | Agent framework with tool-calling |
| mls-match-scraper | Playwright scraping + Celery/RabbitMQ submission |
| Pydantic v2 | Data models and settings |
| pydantic-settings | Environment-based configuration (AGENT_ prefix) |
| httpx | HTTP client for proxy health checks |
| structlog | Structured logging |
| Ruff | Linting and formatting |
| pytest | Test framework |

## Common Commands

### CLI
```bash
uv run match-scraper-agent run --dry-run     # Dry run (submit tools log but skip queue)
uv run match-scraper-agent run --json-logs   # Production run with JSON logging
uv run match-scraper-agent check             # Verify proxy health + RabbitMQ connectivity
```

### Testing
```bash
cd tests && uv run pytest                    # All tests
cd tests && uv run pytest test_agent.py      # Agent tests only
cd tests && uv run pytest test_tools.py      # Tool tests only
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
- `src/utils/` — structlog configuration
- `tests/` — pytest test suite with PydanticAI TestModel
- `envs/` — dotenv files for local and prod environments
- `k3s/` — K3s CronJob manifests
- `.github/workflows/` — CI pipelines

## Agent Tools

| Tool | What it does | LLM tokens? |
|------|-------------|-------------|
| `get_today_info` | Date/day/week (pure python) | No |
| `scrape_matches` | Playwright + CSS extraction via mls-match-scraper | No |
| `submit_matches` | Publishes to RabbitMQ via MatchQueueClient | No |

## Design Conventions

- Pydantic v2 for all data models (use `model_validate`, not `parse_obj`)
- `from __future__ import annotations` in all Python files
- Ruff for linting and formatting (line length 99)
- Type hints on all public functions
- No default exports; use explicit imports
- Agent uses factory function `create_agent(settings)`, not a module singleton
- All tools return `str` (human-readable summary for the LLM)
- Mutating tools respect `dry_run` flag from AgentDeps
- Inter-tool state (scraped matches) stored on AgentDeps._scraped_matches
