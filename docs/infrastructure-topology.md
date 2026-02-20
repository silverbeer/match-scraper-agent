# Infrastructure Topology

Multi-cluster topology for the match-scraper-agent ecosystem.

## Clusters

| Cluster | Context | Provider | Role |
|---------|---------|----------|------|
| LKE (lke560651) | `lke560651-ctx` | Linode LKE | Production |
| rancher-desktop | `rancher-desktop` | Rancher Desktop (local) | Development |

## Component Placement

### LKE — Production (`lke560651-ctx`)

| Namespace | Component | Type |
|-----------|-----------|------|
| `iron-claw` | iron-claw-proxy | Deployment |
| `iron-claw` | FreeRADIUS | Deployment |
| `match-scraper` | match-scraper-agent | CronJob (daily 14:00 UTC) |
| `match-scraper` | RabbitMQ | StatefulSet |
| `match-scraper` | Celery worker | Deployment |
| `missing-table` | missing-table-api (backend) | Deployment |
| `missing-table` | missing-table-frontend | Deployment |
| `missing-table` | Supabase (PostgreSQL) | StatefulSet |

### rancher-desktop — Development (`rancher-desktop`)

Local development runs all services on the Mac via Docker containers and `uv run` processes. No production workloads run here.

## Production Data Flow

```
CronJob (match-scraper namespace)
  → match-scraper-agent container
    → LLM calls → iron-claw-proxy (iron-claw namespace) → Anthropic API
    → scrape_matches() → Playwright → mlssoccer.com
    → submit_matches() → RabbitMQ (match-scraper namespace)
      → Celery worker → missing-table-api (missing-table namespace)
        → PostgreSQL (missing-table namespace)
```

## Context Configuration

Each environment's kubectl context is set via `AGENT_KUBE_CONTEXT` in the corresponding env file:

| File | Value | Target |
|------|-------|--------|
| `envs/.env.prod` | `lke560651-ctx` | Linode LKE |
| `envs/.env.local` | `rancher-desktop` | Local Rancher Desktop |

The operational scripts (`preflight.sh`, `trigger.sh`) read this variable and pass `--context` to all `kubectl` calls, ensuring they always target the correct cluster regardless of the active kubeconfig context.

## Operational Scripts

| Script | What it does |
|--------|-------------|
| `scripts/preflight.sh local [--fix]` | Check (and optionally start) local Docker services |
| `scripts/preflight.sh prod` | Check pod health on LKE via `--context lke560651-ctx` |
| `scripts/trigger.sh local [--dry-run]` | Run agent locally with preflight + post-run monitoring |
| `scripts/trigger.sh prod [--follow]` | Create a Job from the CronJob on LKE |
