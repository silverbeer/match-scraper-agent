# Infrastructure Topology

Two-cluster topology: the match-scraper pipeline runs on a local M4 Mac (rancher-desktop K3s), while the missing-table web application runs in cloud K8s.

## Clusters

| Cluster | Context | Where | Role |
|---------|---------|-------|------|
| Cloud K8s | `lke560651-ctx` | Cloud provider (currently LKE — provider may change) | missing-table API + frontend |
| rancher-desktop | `rancher-desktop` | M4 Mac, local K3s | Scraper pipeline (prod — writes to prod Supabase) |

## Component Placement

### rancher-desktop — Scraper Pipeline (`rancher-desktop`)

All scraper components run here. This IS production for match data — it writes to **prod Supabase**.

| Namespace | Component | Type |
|-----------|-----------|------|
| `match-scraper` | match-scraper-agent | CronJob (daily 14:00 UTC) |
| `match-scraper` | RabbitMQ | StatefulSet |
| `match-scraper` | Celery worker | Deployment |
| `iron-claw` | iron-claw-proxy | Deployment |
| `iron-claw` | FreeRADIUS | Deployment |

### Cloud K8s — Web Application (`lke560651-ctx`)

The missing-table API and frontend run here. Provider has changed over time (GKE → DOKS → LKE) and may move again — docs should say "cloud K8s", not a specific provider.

| Namespace | Component | Type |
|-----------|-----------|------|
| `missing-table` | missing-table-backend (FastAPI) | Deployment |
| `missing-table` | missing-table-frontend (Vue/Nginx) | Deployment |

### Supabase (Managed)

| Instance | Purpose | Used by |
|----------|---------|---------|
| Prod (`ppgxasqgqbnauvxozmjw.supabase.co`) | missingtable.com | Cloud K8s backend + Celery workers on M4 |
| Local (`localhost:54321`) | Local development only | Developer workstation |

There is **no dev Supabase instance** — only prod and local.

## Production Data Flow

```
CronJob (match-scraper namespace, M4 Mac K3s)
  → match-scraper-agent container
    → LLM calls → iron-claw-proxy (iron-claw namespace, M4 Mac K3s) → Anthropic API
    → scrape_matches() → Playwright → mlssoccer.com
    → submit_matches() → RabbitMQ (match-scraper namespace, M4 Mac K3s)
      → Celery worker (match-scraper namespace, M4 Mac K3s)
        → prod Supabase (cloud-hosted)
```

## Context Configuration

Each environment's kubectl context is set via `AGENT_KUBE_CONTEXT` in the corresponding env file:

| File | Value | Target |
|------|-------|--------|
| `envs/.env.prod` | `rancher-desktop` | M4 Mac K3s (scraper pipeline) |
| `envs/.env.local` | `rancher-desktop` | Local Rancher Desktop |

## Operational Scripts

| Script | What it does |
|--------|-------------|
| `scripts/preflight.sh local [--fix]` | Check (and optionally start) local Docker services |
| `scripts/preflight.sh prod` | Check pod health on M4 K3s via `--context rancher-desktop` |
| `scripts/trigger.sh local [--dry-run]` | Run agent locally with preflight + post-run monitoring |
| `scripts/trigger.sh prod [--follow]` | Create a Job from the CronJob on M4 K3s |
