# match-scraper-agent

Pipeline-based match data manager for youth soccer. A deterministic rules engine queries MT for current state, applies journal-based modifier rules, and orchestrates scraping and submission via the [match-scraper](https://github.com/silverbeer/match-scraper) library.

## Architecture

```
PLAN → MODIFY → EXECUTE → REPORT

K3s CronJob (4x/day: 02:00, 08:00, 14:00, 20:00 UTC)
  → match-scraper-agent run --json-logs
    → Planner: queries MT API, builds RunPlan with ScrapePlan entries
    → Modifier: journal-based rules upgrade/downgrade plan entries
    → Executor: scrape_matches() → submit_matches() per target
    → Reporter: writes RunJournal to S3, sends Telegram summary
```

No LLM or proxy required. The rules engine decides what to scrape; `mls-match-scraper` handles browser automation (Playwright + CSS selectors) and queue submission (Celery/RabbitMQ).

## Dependency on match-scraper

This repo consumes [match-scraper](https://github.com/silverbeer/match-scraper) as a library. The scraping engine, data models, and `mls-scraper` CLI all live there; this repo handles scheduling, plan logic, and K3s deployment.

```
match-scraper (library)  ←── match-scraper-agent (this repo, deployment + orchestration)
        │
        └── publishes to: RabbitMQ → Celery workers → Supabase (missingtable.com)
```

**Dependency is tracked via `uv.lock`.** `pyproject.toml` pins to `@main`; the lockfile records the exact resolved commit SHA for reproducible Docker builds.

### Updating match-scraper

When match-scraper merges new features to main:

```bash
uv sync --upgrade-package mls-match-scraper
git add uv.lock
git commit -m "chore: bump mls-match-scraper"
```

Open a PR — CI rebuilds the Docker image with the new library version automatically on merge.

## Prerequisites

| Dependency | Purpose |
|------------|---------|
| Python >= 3.12 | Runtime |
| uv | Package manager |
| RabbitMQ | Match submission queue |
| Playwright | Browser automation (via match-scraper) |

## Quickstart

```bash
git clone https://github.com/silverbeer/match-scraper-agent.git
cd match-scraper-agent
uv sync
playwright install chromium
```

Copy `envs/.env.local.example` to `envs/.env.local` and fill in values.

### Dry run (no mutations)

```bash
uv run match-scraper-agent run --dry-run
```

Scrapes match data and logs what it would submit without publishing to the queue.

### Production run (JSON logs)

```bash
uv run match-scraper-agent run --json-logs
```

### Verify RabbitMQ connectivity

```bash
uv run match-scraper-agent check
```

## Configuration

All settings use the `AGENT_` prefix, loaded from dotenv files in `envs/` or real environment variables.

| Variable | Description |
|----------|-------------|
| `AGENT_RABBITMQ_URL` | RabbitMQ connection URL |
| `AGENT_QUEUE_NAME` | Queue name (default: `matches.prod`) |
| `AGENT_LEAGUE` | League to scrape (default: `Homegrown`) |
| `AGENT_AGE_GROUP` | Age group (default: `U14`) |
| `AGENT_DIVISION` | Division (default: `Northeast`) |
| `AGENT_MISSING_TABLE_API_URL` | MT API base URL |
| `AGENT_MISSING_TABLE_API_TOKEN` | MT API token |
| `AGENT_JOURNAL_S3_BUCKET` | S3 bucket for run journal |
| `AGENT_JOURNAL_S3_KEY` | S3 key for run journal |
| `AGENT_TELEGRAM_CHAT_ID` | Telegram chat for run reports |
| `AGENT_TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `AGENT_DRY_RUN` | Skip mutating operations (default: `false`) |
| `AGENT_JSON_LOGS` | Output structured JSON log lines (default: `false`) |

## CLI Reference

### `match-scraper-agent run`

Run the full PLAN → MODIFY → EXECUTE → REPORT pipeline.

```
Options:
  --dry-run    Scrape but skip queue submission
  --json-logs  Output structured JSON log lines (production mode)
```

### `match-scraper-agent check`

Verify RabbitMQ connectivity.

### `match-scraper-agent audit`

Run an audit report comparing scraped data against MT records.

### `match-scraper-agent scrape`

Ad-hoc scrape without running the full pipeline.

## Pipeline Tools

| Tool | What it does | Mutating? |
|------|-------------|-----------|
| `scrape_matches` | Playwright + CSS extraction via mls-match-scraper | No |
| `submit_matches` | Publishes to RabbitMQ via MatchQueueClient | Yes (respects dry_run) |

## Modifier Rules

| Rule | Condition | Effect |
|------|-----------|--------|
| `all_scored_skip` | 0 missing scores + planner says SCORE_SYNC | Downgrade to SKIP |
| `error_retry` | Prior errors > 0 + planner says SKIP | Upgrade to FULL_SYNC |

## K3s Deployment

```bash
# Full deploy (namespace → RabbitMQ → match-scraper-agent → qop-rankings)
./k3s/deploy.sh
```

CronJob schedules:

| Job | Schedule |
|-----|----------|
| match-scraper-agent | 02:00, 08:00, 14:00, 20:00 UTC daily |
| qop-rankings-scraper | 06:00 UTC every Monday |

## Ad-hoc Backfills

Backfill YAMLs are **not committed** — generated files live locally. Use the generator:

```bash
# Generate YAML (review before applying)
./scripts/backfill.sh --targets u14-hg,u15-hg --from 2026-03-14 --to 2026-03-16

# Generate and apply immediately
./scripts/backfill.sh --targets u14-hg --from 2026-03-14 --to 2026-03-16 --apply
```

## Development

```bash
# Run tests
cd tests && uv run pytest -v

# Lint and format check
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```
