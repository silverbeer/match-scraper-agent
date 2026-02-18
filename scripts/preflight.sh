#!/usr/bin/env bash
# preflight.sh — Verify all services before running match-scraper-agent
#
# Usage:
#   ./scripts/preflight.sh local          # Check only
#   ./scripts/preflight.sh local --fix    # Check + auto-start missing services
#   ./scripts/preflight.sh prod           # Check K3s pod status
set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IRON_CLAW_DIR="/Users/silverbeer/gitrepos/iron-claw"

# ── Colors ─────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'  RED='\033[0;31m'  YELLOW='\033[0;33m'
    CYAN='\033[0;36m'   BOLD='\033[1m'    DIM='\033[2m'  RESET='\033[0m'
else
    GREEN='' RED='' YELLOW='' CYAN='' BOLD='' DIM='' RESET=''
fi

# ── Counters ───────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

# ── Helpers ────────────────────────────────────────────────────────────
pass() { ((PASS_COUNT++)); printf "  ${GREEN}PASS${RESET}  %s\n" "$1"; }
fail() { ((FAIL_COUNT++)); printf "  ${RED}FAIL${RESET}  %s\n" "$1"; }
warn() { ((WARN_COUNT++)); printf "  ${YELLOW}WARN${RESET}  %s\n" "$1"; }
info() { printf "  ${CYAN}INFO${RESET}  %s\n" "$1"; }
section() { printf "\n${BOLD}%s${RESET}\n" "$1"; }

port_listening() {
    lsof -iTCP:"$1" -sTCP:LISTEN -P -n >/dev/null 2>&1
}

wait_for_port() {
    local port=$1 timeout=$2 elapsed=0
    while ! port_listening "$port"; do
        sleep 1
        ((elapsed++))
        if [[ $elapsed -ge $timeout ]]; then
            return 1
        fi
    done
    return 0
}

wait_for_url() {
    local url=$1 timeout=$2 elapsed=0
    while ! curl -sf --connect-timeout 2 "$url" >/dev/null 2>&1; do
        sleep 1
        ((elapsed++))
        if [[ $elapsed -ge $timeout ]]; then
            return 1
        fi
    done
    return 0
}

container_running() {
    docker ps --filter "name=^/${1}$" --format '{{.Names}}' 2>/dev/null | grep -q "^${1}$"
}

# ── Usage ──────────────────────────────────────────────────────────────
usage() {
    cat <<'USAGE'
Usage: ./scripts/preflight.sh <env> [--fix]

  env:    local | prod
  --fix:  Auto-start missing services (local only)

Examples:
  ./scripts/preflight.sh local          # Check only
  ./scripts/preflight.sh local --fix    # Check + auto-start
  ./scripts/preflight.sh prod           # Check K3s pods
USAGE
    exit 1
}

# ── Parse args ─────────────────────────────────────────────────────────
[[ $# -lt 1 ]] && usage

ENV="$1"
FIX=false
[[ "${2:-}" == "--fix" ]] && FIX=true

if [[ "$ENV" != "local" && "$ENV" != "prod" ]]; then
    echo "Error: env must be 'local' or 'prod'" >&2
    exit 1
fi

# ── Header ─────────────────────────────────────────────────────────────
printf "\n${BOLD}=== match-scraper-agent preflight ===${RESET}\n"
printf "  env:  %s\n" "$ENV"
printf "  fix:  %s\n" "$FIX"
printf "  time: %s\n" "$(date '+%Y-%m-%d %H:%M:%S')"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL MODE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if [[ "$ENV" == "local" ]]; then

    # 1. Docker daemon
    section "Docker"
    if docker info >/dev/null 2>&1; then
        pass "Docker daemon is running"
    else
        fail "Docker daemon is not running"
        info "Start Docker Desktop or run: open -a Docker"
    fi

    # 2. Env file
    section "Environment"
    ENV_FILE="$REPO_DIR/envs/.env.local"
    if [[ -f "$ENV_FILE" ]]; then
        pass "envs/.env.local exists"
    else
        fail "envs/.env.local not found"
        info "Copy from template: cp envs/.env.example envs/.env.local"
    fi

    # 3. PostgreSQL (Supabase local — port 54322)
    section "PostgreSQL (port 54322)"
    if port_listening 54322; then
        pass "PostgreSQL listening on port 54322"
    else
        fail "PostgreSQL not listening on port 54322"
        info "Start Supabase: supabase start"
    fi

    # 4. FreeRADIUS (Docker container)
    section "FreeRADIUS (Docker)"
    if container_running "iron-claw-radius"; then
        pass "Container 'iron-claw-radius' is running"
    else
        fail "Container 'iron-claw-radius' is not running"
        if [[ "$FIX" == true ]]; then
            if [[ -d "$IRON_CLAW_DIR/docker" ]]; then
                info "Starting FreeRADIUS..."
                (cd "$IRON_CLAW_DIR/docker" && docker compose up -d) 2>&1 | sed 's/^/       /'
                info "Waiting for container (up to 10s)..."
                elapsed=0
                while ! container_running "iron-claw-radius"; do
                    sleep 1
                    ((elapsed++))
                    if [[ $elapsed -ge 10 ]]; then break; fi
                done
                if container_running "iron-claw-radius"; then
                    # Undo the fail, count as pass
                    ((FAIL_COUNT--))
                    pass "Container 'iron-claw-radius' started"
                else
                    info "Container failed to start — check docker compose logs"
                fi
            else
                info "iron-claw repo not found at $IRON_CLAW_DIR"
            fi
        else
            info "Fix: cd $IRON_CLAW_DIR/docker && docker compose up -d"
            info "Or run with --fix to auto-start"
        fi
    fi

    # 5. RabbitMQ (port 5672)
    section "RabbitMQ (port 5672)"
    if port_listening 5672; then
        pass "RabbitMQ listening on port 5672"
    else
        fail "RabbitMQ not listening on port 5672"
        if [[ "$FIX" == true ]]; then
            info "Starting RabbitMQ..."
            if docker ps -a --filter "name=^/rabbitmq$" --format '{{.Names}}' | grep -q "^rabbitmq$"; then
                docker start rabbitmq 2>&1 | sed 's/^/       /'
            else
                docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management 2>&1 | sed 's/^/       /'
            fi
            info "Waiting for RabbitMQ (up to 15s)..."
            if wait_for_port 5672 15; then
                ((FAIL_COUNT--))
                pass "RabbitMQ started on port 5672"
            else
                info "RabbitMQ failed to start in time — check: docker logs rabbitmq"
            fi
        else
            info "Fix: docker run -d --name rabbitmq -p 5672:5672 rabbitmq:3-management"
            info "Or run with --fix to auto-start"
        fi
    fi

    # 6. Celery worker (consumers on RabbitMQ)
    MISSING_TABLE_DIR="$HOME/gitrepos/missing-table/backend"
    WORKER_LOG="/tmp/celery-worker.log"
    WORKER_PID_FILE="/tmp/celery-worker.pid"
    section "Celery worker"
    if port_listening 5672; then
        CONSUMERS=$(command docker exec rabbitmq rabbitmqctl list_consumers -q 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$CONSUMERS" -gt 0 ]]; then
            pass "$CONSUMERS consumer(s) connected to RabbitMQ"
        else
            fail "No Celery workers consuming from RabbitMQ"
            info "Submitted matches won't be processed without a worker"
            if [[ "$FIX" == true ]]; then
                if [[ -d "$MISSING_TABLE_DIR" ]]; then
                    info "Starting Celery worker..."
                    (cd "$MISSING_TABLE_DIR" && \
                        RABBITMQ_URL="amqp://admin:admin123@localhost:5672//" \
                        nohup uv run celery -A celery_app worker -l info -Q match_processing \
                        > "$WORKER_LOG" 2>&1 &
                        echo $! > "$WORKER_PID_FILE"
                    )
                    info "PID $(cat "$WORKER_PID_FILE") — log at $WORKER_LOG"
                    info "Waiting for worker to connect (up to 30s)..."
                    elapsed=0
                    while true; do
                        CONSUMERS=$(command docker exec rabbitmq rabbitmqctl list_consumers -q 2>/dev/null | wc -l | tr -d ' ')
                        if [[ "$CONSUMERS" -gt 0 ]]; then
                            ((FAIL_COUNT--))
                            pass "Celery worker started ($CONSUMERS consumer(s))"
                            break
                        fi
                        sleep 1
                        ((elapsed++))
                        if [[ $elapsed -ge 30 ]]; then
                            info "Worker not consuming yet — check: tail $WORKER_LOG"
                            break
                        fi
                    done
                else
                    info "missing-table repo not found at $MISSING_TABLE_DIR"
                fi
            else
                info "Fix: cd $MISSING_TABLE_DIR && RABBITMQ_URL=\"amqp://admin:admin123@localhost:5672//\" uv run celery -A celery_app worker -l info -Q match_processing"
                info "Or run with --fix to auto-start"
            fi
        fi
    else
        info "Skipped — RabbitMQ not running"
    fi

    # 7. iron-claw proxy (port 8100)
    section "iron-claw proxy (port 8100)"
    if curl -sf --connect-timeout 3 http://localhost:8100/health >/dev/null 2>&1; then
        pass "Proxy responding at http://localhost:8100/health"
    else
        fail "Proxy not responding at http://localhost:8100/health"
        if [[ "$FIX" == true ]]; then
            if [[ -d "$IRON_CLAW_DIR" ]]; then
                info "Starting iron-claw proxy..."
                cd "$IRON_CLAW_DIR"
                nohup uv run iron-claw proxy > /tmp/iron-claw-proxy.log 2>&1 &
                PROXY_PID=$!
                echo "$PROXY_PID" > /tmp/iron-claw-proxy.pid
                cd "$REPO_DIR"
                info "PID $PROXY_PID — log at /tmp/iron-claw-proxy.log"
                info "Waiting for proxy (up to 10s)..."
                if wait_for_url http://localhost:8100/health 10; then
                    ((FAIL_COUNT--))
                    pass "Proxy started on port 8100"
                else
                    info "Proxy failed to start in time — check: tail /tmp/iron-claw-proxy.log"
                fi
            else
                info "iron-claw repo not found at $IRON_CLAW_DIR"
            fi
        else
            info "Fix: cd $IRON_CLAW_DIR && uv run iron-claw proxy"
            info "Or run with --fix to auto-start"
        fi
    fi

    # 8. Playwright / Chromium
    section "Playwright / Chromium"
    if uv run python -c "from playwright.sync_api import sync_playwright" 2>/dev/null; then
        pass "Playwright importable"
    else
        warn "Playwright not importable"
        info "Install: playwright install chromium"
    fi

    # 9. Internet (MLS Next)
    section "Internet (MLS Next)"
    if curl -sf --connect-timeout 5 -o /dev/null https://www.mlssoccer.com; then
        pass "mlssoccer.com reachable"
    else
        warn "mlssoccer.com unreachable"
        info "Check internet connection"
    fi

    # 10. App-level check
    section "App-level check"
    if [[ $FAIL_COUNT -eq 0 ]]; then
        info "Running: uv run match-scraper-agent check --env local"
        echo ""
        if (cd "$REPO_DIR" && uv run match-scraper-agent check --env local 2>&1) | sed 's/^/       /'; then
            pass "App-level check passed"
        else
            fail "App-level check failed"
        fi
    else
        info "Skipped — $FAIL_COUNT critical check(s) failed above"
    fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PROD MODE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
elif [[ "$ENV" == "prod" ]]; then

    # 1. kubectl reachable
    section "Kubernetes cluster"
    if kubectl cluster-info >/dev/null 2>&1; then
        pass "kubectl can reach the cluster"
    else
        fail "kubectl cannot reach the cluster"
        info "Check: kubectl config current-context"
    fi

    # 2. iron-claw-proxy pod
    section "iron-claw-proxy pod"
    POD_STATUS=$(kubectl get pods -n iron-claw -l app=iron-claw-proxy -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")
    if [[ "$POD_STATUS" == "Running" ]]; then
        pass "iron-claw-proxy pod is Running"
    elif [[ -n "$POD_STATUS" ]]; then
        fail "iron-claw-proxy pod status: $POD_STATUS"
    else
        fail "iron-claw-proxy pod not found in namespace iron-claw"
    fi

    # 3. RabbitMQ pod
    section "RabbitMQ pod"
    POD_STATUS=$(kubectl get pods -n match-scraper -l app=rabbitmq -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")
    if [[ "$POD_STATUS" == "Running" ]]; then
        pass "RabbitMQ pod is Running"
    elif [[ -n "$POD_STATUS" ]]; then
        fail "RabbitMQ pod status: $POD_STATUS"
    else
        fail "RabbitMQ pod not found in namespace match-scraper"
    fi

    # 4. missing-table-api pod
    section "missing-table-api pod"
    POD_STATUS=$(kubectl get pods -n missing-table -l app=missing-table-api -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")
    if [[ "$POD_STATUS" == "Running" ]]; then
        pass "missing-table-api pod is Running"
    elif [[ -n "$POD_STATUS" ]]; then
        fail "missing-table-api pod status: $POD_STATUS"
    else
        fail "missing-table-api pod not found in namespace missing-table"
    fi

    # 5. CronJob exists
    section "CronJob"
    if kubectl get cronjob match-scraper-agent -n match-scraper >/dev/null 2>&1; then
        SCHEDULE=$(kubectl get cronjob match-scraper-agent -n match-scraper -o jsonpath='{.spec.schedule}')
        pass "CronJob 'match-scraper-agent' exists (schedule: $SCHEDULE)"
    else
        fail "CronJob 'match-scraper-agent' not found in namespace match-scraper"
    fi

fi

# ── Summary ────────────────────────────────────────────────────────────
printf "\n"
printf "${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
printf "${BOLD}Summary${RESET}  env=%s\n" "$ENV"
printf "  ${GREEN}PASS:${RESET}  %d\n" "$PASS_COUNT"
if [[ $FAIL_COUNT -gt 0 ]]; then
    printf "  ${RED}FAIL:${RESET}  %d\n" "$FAIL_COUNT"
fi
if [[ $WARN_COUNT -gt 0 ]]; then
    printf "  ${YELLOW}WARN:${RESET}  %d\n" "$WARN_COUNT"
fi
printf "${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
printf "\n"

if [[ $FAIL_COUNT -gt 0 ]]; then
    printf "${RED}PREFLIGHT FAILED${RESET} — %d check(s) failed\n\n" "$FAIL_COUNT"
    exit 1
else
    printf "${GREEN}PREFLIGHT PASSED${RESET}\n\n"
    exit 0
fi
