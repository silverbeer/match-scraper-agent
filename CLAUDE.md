# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**match-scraper-agent** is a pipeline-based match data manager that uses a deterministic rules engine to orchestrate youth soccer match scraping and submission. The planner queries MT for current state, modifier rules adjust the plan based on the previous run's journal, and the executor scrapes and submits via the `mls-match-scraper` library (Playwright + CSS selectors, Celery/RabbitMQ). No LLM or proxy required.

## Architecture

```
PLAN → MODIFY → EXECUTE → REPORT
```

- **Plan:** Deterministic planner queries MT API, computes `RunPlan` with `ScrapePlan` entries
- **Modify:** Journal-based modifier rules upgrade/downgrade plan entries (e.g. skip fully-scored targets, retry on prior errors)
- **Execute:** Pipeline engine iterates plan, calls `scrape_matches()` → `submit_matches()` per target
- **Report:** Writes `RunJournal` to disk, sends Telegram report, logs summary
- **Scraping:** `mls-match-scraper` library (Playwright + CSS selectors, zero LLM tokens)
- **Match submission:** `mls-match-scraper` MatchQueueClient → RabbitMQ → Celery workers
- **Deployment:** K3s CronJob (4x/day at 02:00, 08:00, 14:00, 20:00 UTC)

## Key Technology Choices

| Tool | Purpose |
|------|---------|
| Python >= 3.12 + uv | Package management |
| Typer | CLI framework (`match-scraper-agent run`, `check`) |
| mls-match-scraper | Playwright scraping + Celery/RabbitMQ submission |
| Pydantic v2 | Data models and settings |
| pydantic-settings | Environment-based configuration (AGENT_ prefix) |
| httpx | HTTP client for MT API calls |
| structlog | Structured logging |
| Ruff | Linting and formatting |
| pytest | Test framework |

## Common Commands

### CLI
```bash
uv run match-scraper-agent run --dry-run     # Dry run (submit tools log but skip queue)
uv run match-scraper-agent run --json-logs   # Production run with JSON logging
uv run match-scraper-agent check             # Verify RabbitMQ connectivity
```

### Testing
```bash
cd tests && uv run pytest                    # All tests
cd tests && uv run pytest test_agent.py      # Engine + modifier tests
cd tests && uv run pytest test_tools.py      # Tool tests only
cd tests && uv run pytest -k "test_dry"      # Filter by name
```

### Linting
```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Repo Layout

- `src/agent/` — Pipeline engine, tools, deps (RunContext), planner, result model
- `src/cli/` — Typer CLI application (`run`, `check`, `scrape`, `audit` commands)
- `src/config/` — pydantic-settings configuration (AGENT_ prefix)
- `src/utils/` — structlog configuration, run journal, Telegram report builder
- `tests/` — pytest test suite for engine, modifiers, and tools
- `envs/` — dotenv files for local and prod environments
- `k3s/` — K3s CronJob manifests
- `.github/workflows/` — CI pipelines

## Pipeline Tools

| Tool | What it does |
|------|-------------|
| `scrape_matches` | Playwright + CSS extraction via mls-match-scraper |
| `submit_matches` | Publishes to RabbitMQ via MatchQueueClient |

## Modifier Rules

| Rule | Condition | Effect |
|------|-----------|--------|
| `all_scored_skip` | Journal shows 0 missing scores + planner says SCORE_SYNC | Downgrade to SKIP |
| `error_retry` | Journal shows errors > 0 + planner says SKIP | Upgrade to FULL_SYNC |

## Ad-hoc Backfills

Backfill Job YAMLs are **not committed to the repo** — they accumulate fast and add no long-term value. Generated files live in `~/.local/share/match-scraper-agent/backfills/` and `k3s/backfill/` is gitignored.

Use the generator script:

```bash
# Generate YAML (review before applying)
./scripts/backfill.sh --targets u14-hg,u15-hg,u16-hg --from 2026-03-14 --to 2026-03-16

# Generate and apply immediately
./scripts/backfill.sh --targets u14-hg --from 2026-03-14 --to 2026-03-16 --apply

# Dry run (no submission, just scrape + log)
./scripts/backfill.sh --targets u14-hg --from 2026-03-14 --to 2026-03-16 --apply --dry-run
```

Monitor a running backfill:
```bash
kubectl logs -n match-scraper -l task=backfill --follow
kubectl get jobs -n match-scraper -l task=backfill
```

Clean up completed jobs:
```bash
kubectl delete jobs -n match-scraper -l task=backfill
```

## Design Conventions

- Pydantic v2 for all data models (use `model_validate`, not `parse_obj`)
- `from __future__ import annotations` in all Python files
- Ruff for linting and formatting (line length 99)
- Type hints on all public functions
- No default exports; use explicit imports
- Pipeline engine entry point: `run_pipeline(plan, ctx, journal)`
- All tools return `str` (human-readable summary for logging)
- Mutating tools respect `dry_run` flag from RunContext
- Inter-tool state (scraped matches) stored on RunContext._scraped_matches
