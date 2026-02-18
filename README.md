# match-scraper-agent

Agentic match data manager for youth soccer. Uses PydanticAI to reason about what to scrape and submit, routes all LLM calls through the [iron-claw](https://github.com/silverbeer/iron-claw) proxy for RADIUS-based cost monitoring, and delegates scraping + queue submission to [match-scraper](https://github.com/silverbeer/match-scraper).

## Architecture

```
K3s CronJob (daily 14:00 UTC)
  → match-scraper-agent run --json-logs
    → PydanticAI Agent (claude-haiku-4-5)
      → LLM reasoning via iron-claw proxy :8100 (RADIUS metering)
      → Tools:
          get_today_info()    → pure python (datetime)
          scrape_matches()    → match-scraper MLSScraper (Playwright + CSS)
          submit_matches()    → match-scraper MatchQueueClient (Celery/RabbitMQ)
```

The LLM decides WHAT to do (agentic reasoning) — it does **not** parse HTML. The proven `mls-match-scraper` library handles browser automation (Playwright + CSS selectors) and queue submission (Celery/RabbitMQ).

## Prerequisites

| Dependency | Purpose | Install |
|------------|---------|---------|
| Python >= 3.12 | Runtime | [python.org](https://www.python.org/) |
| uv | Package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| match-scraper | Scraping + queue library | Local path dependency |
| iron-claw proxy | LLM proxy with RADIUS metering | See [iron-claw](https://github.com/silverbeer/iron-claw) |
| RabbitMQ | Message queue for match submission | `docker run -d -p 5672:5672 rabbitmq:3` |
| Playwright | Browser automation (installed via match-scraper) | `playwright install chromium` |

## Quickstart

### 1. Clone and install

```bash
git clone https://github.com/silverbeer/match-scraper-agent.git
cd match-scraper-agent
uv sync
playwright install chromium
```

### 2. Verify dependencies

```bash
uv run match-scraper-agent check --env local
```

This checks that the iron-claw proxy and RabbitMQ are reachable. Expected output:

```
environment: local
proxy: checking http://localhost:8100/status
  status: 200
rabbitmq: checking amqp://guest:guest@localhost:5672/
  status: connected
```

### 3. Dry run (no mutations)

```bash
uv run match-scraper-agent run --env local --dry-run
```

The agent runs normally — calls the LLM, scrapes match data — but `submit_matches` logs what it *would* do without publishing to the queue.

### 4. Live run

```bash
uv run match-scraper-agent run --env local
```

### 5. Production run

```bash
uv run match-scraper-agent run --env prod
```

## Configuration

Configuration is loaded from dotenv files in `envs/`, selected via `--env`:

| File | Purpose |
|------|---------|
| `envs/.env.local` | Local development — all components on Mac Mini / Air |
| `envs/.env.prod` | Production — K3s cluster |

Precedence: **env vars > dotenv file > code defaults**. You can always override a setting with a real environment variable, even when using a dotenv file.

### Variables

All settings use the `AGENT_` prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_PROXY_BASE_URL` | `http://localhost:8100` | iron-claw proxy endpoint |
| `AGENT_MODEL_NAME` | `claude-haiku-4-5-20251001` | PydanticAI model identifier |
| `AGENT_ANTHROPIC_API_KEY` | `agent-via-proxy` | Dummy key — proxy handles the real Anthropic key |
| `AGENT_RABBITMQ_URL` | `amqp://guest:guest@localhost:5672/` | RabbitMQ connection URL |
| `AGENT_EXCHANGE_NAME` | `matches-fanout` | RabbitMQ fanout exchange name |
| `AGENT_LEAGUE` | `Homegrown` | Default league for scraping |
| `AGENT_AGE_GROUP` | `U15` | Default age group for scraping |
| `AGENT_DIVISION` | `Northeast` | Default division for scraping |
| `AGENT_MISSING_TABLE_API_URL` | `http://localhost:8000` | Missing Table API URL (for scraper config) |
| `AGENT_MISSING_TABLE_API_KEY` | *(empty)* | Missing Table API key |
| `AGENT_DRY_RUN` | `false` | Skip mutating operations |
| `AGENT_JSON_LOGS` | `false` | Output structured JSON log lines |
| `AGENT_LOG_LEVEL` | `info` | Minimum log level |

## CLI Reference

### `match-scraper-agent run`

Run the agent. It checks the date, scrapes matches, and submits them to the queue.

```
Options:
  --env TEXT       Environment name: local, prod (default: local)
  --dry-run        Skip mutating operations
  --json-logs      Output JSON log lines
  --model TEXT     Override AGENT_MODEL_NAME
  --proxy-url TEXT Override AGENT_PROXY_BASE_URL
```

### `match-scraper-agent check`

Verify that the iron-claw proxy and RabbitMQ are reachable.

```
Options:
  --env TEXT       Environment name: local, prod (default: local)
  --proxy-url TEXT Override AGENT_PROXY_BASE_URL
```

## Agent Tools

| Tool | What it does | Mutating? |
|------|-------------|-----------|
| `get_today_info` | Date/day/week (pure python) | No |
| `scrape_matches` | Playwright + CSS extraction via MLSScraper | No |
| `submit_matches` | Publishes to RabbitMQ via MatchQueueClient | Yes (respects dry_run) |

## Development

```bash
# Run tests
cd tests && uv run pytest -v

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## K3s Deployment

Manifests are in `k3s/match-scraper-agent/`:

```bash
kubectl apply -f k3s/match-scraper-agent/configmap.yaml
kubectl apply -f k3s/match-scraper-agent/secret.yaml
kubectl apply -f k3s/match-scraper-agent/cronjob.yaml
```

The CronJob runs daily at 14:00 UTC with `concurrencyPolicy: Forbid`.
