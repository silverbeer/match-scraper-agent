#!/usr/bin/env bash
# trigger.sh — Manually trigger match-scraper-agent (local run or K3s Job)
#
# Usage:
#   ./scripts/trigger.sh local              # Run agent locally
#   ./scripts/trigger.sh local --dry-run    # Dry run (no mutations)
#   ./scripts/trigger.sh prod               # Create K3s Job from CronJob
#   ./scripts/trigger.sh prod --follow      # Create Job + tail logs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

NAMESPACE="match-scraper"
CRONJOB_NAME="match-scraper-agent"
WORKER_LOG="/tmp/celery-worker.log"

# ── Colors ─────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'  RED='\033[0;31m'  YELLOW='\033[0;33m'
    CYAN='\033[0;36m'   BOLD='\033[1m'    DIM='\033[2m'  RESET='\033[0m'
else
    GREEN='' RED='' YELLOW='' CYAN='' BOLD='' DIM='' RESET=''
fi

# ── Usage ──────────────────────────────────────────────────────────────
usage() {
    cat <<'USAGE'
Usage: ./scripts/trigger.sh <env> [options]

  env:        local | prod

Local options:
  --dry-run       Skip mutating operations (no queue submissions)
  --model X       Override model name
  --target NAME   Scrape only this target:
                    u14-hg           U14 Homegrown Northeast (all teams)
                    u14-hg-ifa       U14 Homegrown Northeast (IFA only)
                    u13-hg           U13 Homegrown Northeast (all teams)
                    u13-hg-ifa       U13 Homegrown Northeast (IFA only)
                    u14-academy      U14 Academy New England (all teams)
                    u14-academy-ifa  U14 Academy New England (IFA only)

Prod options:
  --follow    Tail pod logs after creating the Job
  --dry-run   Pass --dry-run to the agent container

Examples:
  ./scripts/trigger.sh local                            # Run agent (all targets)
  ./scripts/trigger.sh local --target u14-hg-ifa       # Only U14 HG IFA matches
  ./scripts/trigger.sh local --target u14-academy-ifa  # Only U14 Academy IFA matches
  ./scripts/trigger.sh local --target u14-hg           # All U14 HG Northeast teams
  ./scripts/trigger.sh local --dry-run                 # Local dry run
  ./scripts/trigger.sh prod                            # Create K3s Job
  ./scripts/trigger.sh prod --follow                   # Create Job + tail logs
USAGE
    exit 1
}

# ── Parse args ─────────────────────────────────────────────────────────
[[ $# -lt 1 ]] && usage

ENV="$1"
shift

if [[ "$ENV" != "local" && "$ENV" != "prod" ]]; then
    echo "Error: env must be 'local' or 'prod'" >&2
    exit 1
fi

DRY_RUN=false
FOLLOW=false
MODEL=""
TARGET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --follow)  FOLLOW=true ;;
        --model)   MODEL="$2"; shift ;;
        --target)  TARGET="$2"; shift ;;
        *)         echo "Unknown option: $1" >&2; usage ;;
    esac
    shift
done

# ── Load env file ──────────────────────────────────────────────────────
ENV_FILE="$REPO_DIR/envs/.env.${ENV}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

# ── Resolve kubectl context for prod ──────────────────────────────────
if [[ "$ENV" == "prod" ]]; then
    KUBE_CONTEXT="${AGENT_KUBE_CONTEXT:-}"
    if [[ -z "$KUBE_CONTEXT" ]]; then
        printf "${RED}AGENT_KUBE_CONTEXT is not set.${RESET}\n"
        printf "Set it in envs/.env.prod or export it before running.\n"
        exit 1
    fi
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL MODE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if [[ "$ENV" == "local" ]]; then

    # ── Preflight ──────────────────────────────────────────────────────
    printf "${BOLD}Running preflight checks (auto-fix enabled)...${RESET}\n"
    if ! "$SCRIPT_DIR/preflight.sh" local --fix; then
        printf "\n${RED}Preflight failed — some services could not be started.${RESET}\n"
        printf "Check output above and fix manually.\n\n"
        exit 1
    fi

    # ── Snapshot queue state before run ────────────────────────────────
    QUEUE_BEFORE=$(command docker exec rabbitmq rabbitmqctl list_queues name messages -q 2>/dev/null || echo "")
    WORKER_LINES_BEFORE=0
    if [[ -f "$WORKER_LOG" ]]; then
        WORKER_LINES_BEFORE=$(wc -l < "$WORKER_LOG" | tr -d ' ')
    fi

    # ── Run the agent ──────────────────────────────────────────────────
    CMD=(uv run match-scraper-agent run --env local)
    [[ "$DRY_RUN" == true ]] && CMD+=(--dry-run)
    [[ -n "$MODEL" ]] && CMD+=(--model "$MODEL")
    [[ -n "$TARGET" ]] && CMD+=(--target "$TARGET")

    printf "\n${BOLD}Triggering agent...${RESET}\n"
    printf "  ${DIM}%s${RESET}\n\n" "${CMD[*]}"

    cd "$REPO_DIR"
    AGENT_EXIT=0
    "${CMD[@]}" || AGENT_EXIT=$?

    printf "\n"

    if [[ $AGENT_EXIT -ne 0 ]]; then
        printf "${RED}Agent exited with code %d${RESET}\n\n" "$AGENT_EXIT"
    fi

    # ── Skip monitoring for dry runs ───────────────────────────────────
    if [[ "$DRY_RUN" == true ]]; then
        printf "${DIM}Dry run — no queue monitoring needed.${RESET}\n\n"
        exit $AGENT_EXIT
    fi

    # ── Post-run: Monitor queue processing ─────────────────────────────
    printf "${BOLD}=== Post-run monitoring ===${RESET}\n"

    # 1. Check queue depth — wait for messages to drain
    printf "\n${BOLD}Queue status${RESET}\n"
    QUEUE_AFTER=$(command docker exec rabbitmq rabbitmqctl list_queues name messages -q 2>/dev/null || echo "")

    if [[ -z "$QUEUE_AFTER" ]]; then
        printf "  ${YELLOW}WARN${RESET}  Could not query RabbitMQ queues\n"
    else
        TOTAL_MESSAGES=0
        while IFS=$'\t' read -r QNAME QCOUNT _REST; do
            [[ -z "$QNAME" ]] && continue
            # Skip header row and non-numeric counts
            [[ ! "$QCOUNT" =~ ^[0-9]+$ ]] && continue
            TOTAL_MESSAGES=$((TOTAL_MESSAGES + QCOUNT))
            if [[ "$QCOUNT" -eq 0 ]]; then
                printf "  ${GREEN}PASS${RESET}  %s: %d messages (drained)\n" "$QNAME" "$QCOUNT"
            else
                printf "  ${CYAN}INFO${RESET}  %s: %d messages pending\n" "$QNAME" "$QCOUNT"
            fi
        done <<< "$QUEUE_AFTER"

        if [[ $TOTAL_MESSAGES -gt 0 ]]; then
            printf "\n  ${CYAN}INFO${RESET}  %d message(s) still in queue — waiting up to 30s...\n" "$TOTAL_MESSAGES"
            elapsed=0
            while [[ $elapsed -lt 30 ]]; do
                sleep 2
                ((elapsed += 2))
                REMAINING=0
                while IFS=$'\t' read -r QNAME QCOUNT _REST; do
                    [[ -z "$QNAME" ]] && continue
                    [[ ! "$QCOUNT" =~ ^[0-9]+$ ]] && continue
                    REMAINING=$((REMAINING + QCOUNT))
                done <<< "$(command docker exec rabbitmq rabbitmqctl list_queues name messages -q 2>/dev/null || echo "")"
                if [[ $REMAINING -eq 0 ]]; then
                    printf "  ${GREEN}PASS${RESET}  All messages processed (%ds)\n" "$elapsed"
                    break
                fi
            done
            if [[ $REMAINING -gt 0 ]]; then
                printf "  ${YELLOW}WARN${RESET}  %d message(s) still pending after 30s\n" "$REMAINING"
            fi
        else
            printf "  ${GREEN}PASS${RESET}  All queues drained\n"
        fi
    fi

    # 2. Worker log activity — show new lines since agent started
    printf "\n${BOLD}Worker activity${RESET}\n"
    if [[ -f "$WORKER_LOG" ]]; then
        WORKER_LINES_AFTER=$(wc -l < "$WORKER_LOG" | tr -d ' ')
        NEW_LINES=$((WORKER_LINES_AFTER - WORKER_LINES_BEFORE))
        if [[ $NEW_LINES -gt 0 ]]; then
            printf "  ${GREEN}PASS${RESET}  %d new log line(s) from worker\n\n" "$NEW_LINES"

            # Extract task results from new worker output
            # Cache new worker lines to a temp file to avoid repeated tail on large logs
            WORKER_TAIL=$(mktemp)
            tail -n "$NEW_LINES" "$WORKER_LOG" > "$WORKER_TAIL" 2>/dev/null

            CREATED=$(grep -c '"status": "created"' "$WORKER_TAIL" 2>/dev/null || true)
            CREATED="${CREATED:-0}"; CREATED="${CREATED//[^0-9]/}"
            UPDATED=$(grep -c '"status": "updated"' "$WORKER_TAIL" 2>/dev/null || true)
            UPDATED="${UPDATED:-0}"; UPDATED="${UPDATED//[^0-9]/}"
            SKIPPED=$(grep -c '"status": "skipped"' "$WORKER_TAIL" 2>/dev/null || true)
            SKIPPED="${SKIPPED:-0}"; SKIPPED="${SKIPPED//[^0-9]/}"
            ERRORS=$(grep -ci 'error\|traceback\|failed' "$WORKER_TAIL" 2>/dev/null || true)
            ERRORS="${ERRORS:-0}"; ERRORS="${ERRORS//[^0-9]/}"
            SUCCEEDED=$(grep -c 'Task .* succeeded' "$WORKER_TAIL" 2>/dev/null || true)
            SUCCEEDED="${SUCCEEDED:-0}"; SUCCEEDED="${SUCCEEDED//[^0-9]/}"

            if [[ $((CREATED + UPDATED + SKIPPED + SUCCEEDED)) -gt 0 ]]; then
                printf "  ${BOLD}Task results:${RESET}\n"
                [[ "$SUCCEEDED" -gt 0 ]] && printf "    ${GREEN}tasks succeeded:${RESET}  %s\n" "$SUCCEEDED"
                [[ "$CREATED" -gt 0 ]]   && printf "    ${GREEN}matches created:${RESET}  %s\n" "$CREATED"
                [[ "$UPDATED" -gt 0 ]]   && printf "    ${CYAN}matches updated:${RESET}  %s\n" "$UPDATED"
                [[ "$SKIPPED" -gt 0 ]]   && printf "    ${DIM}matches skipped:${RESET}  %s\n" "$SKIPPED"
            fi
            [[ "$ERRORS" -gt 0 ]] && printf "    ${RED}error lines:${RESET}     %s\n" "$ERRORS"

            # Show last few task results
            printf "\n  ${BOLD}Recent worker output:${RESET}\n"
            grep -E 'succeeded|ERROR|processed match' "$WORKER_TAIL" | tail -10 | sed 's/^/    /'

            rm -f "$WORKER_TAIL"
        else
            printf "  ${YELLOW}WARN${RESET}  No new worker log output\n"
            printf "  ${CYAN}INFO${RESET}  Worker may not have received tasks — check: tail %s\n" "$WORKER_LOG"
        fi
    else
        printf "  ${YELLOW}WARN${RESET}  Worker log not found at %s\n" "$WORKER_LOG"
    fi

    # 3. Database check — count recent matches from agent source
    printf "\n${BOLD}Database (matches table)${RESET}\n"
    DB_HOST="${AGENT_DB_HOST:-127.0.0.1}"
    DB_PORT="${AGENT_DB_PORT:-54332}"
    DB_USER="${AGENT_DB_USER:-postgres}"
    DB_NAME="${AGENT_DB_NAME:-postgres}"
    export PGPASSWORD="${AGENT_DB_PASSWORD:-postgres}"
    PSQL_OPTS=(-h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -w -t -A)

    if command -v psql >/dev/null 2>&1; then
        MATCH_COUNT=$(psql "${PSQL_OPTS[@]}" -c \
            "SELECT count(*) FROM matches WHERE source = 'match-scraper-agent';" 2>/dev/null || echo "")
        RECENT_COUNT=$(psql "${PSQL_OPTS[@]}" -c \
            "SELECT count(*) FROM matches WHERE source = 'match-scraper-agent' AND created_at > now() - interval '5 minutes';" 2>/dev/null || echo "")

        if [[ -n "$MATCH_COUNT" ]]; then
            printf "  ${GREEN}PASS${RESET}  %s total matches from agent in DB\n" "$MATCH_COUNT"
            if [[ -n "$RECENT_COUNT" && "$RECENT_COUNT" -gt 0 ]]; then
                printf "  ${GREEN}PASS${RESET}  %s matches inserted in last 5 minutes\n" "$RECENT_COUNT"

                # Show a sample of recent matches
                printf "\n  ${BOLD}Recent matches:${RESET}\n"
                psql "${PSQL_OPTS[@]}" -F '|' -c \
                    "SELECT m.match_date, ht.name, at.name, m.match_status
                     FROM matches m
                     JOIN teams ht ON m.home_team_id = ht.id
                     JOIN teams at ON m.away_team_id = at.id
                     WHERE m.source = 'match-scraper-agent'
                       AND m.created_at > now() - interval '5 minutes'
                     ORDER BY m.match_date
                     LIMIT 10;" 2>/dev/null | while IFS='|' read -r MDATE HOME AWAY STATUS; do
                    printf "    %s  %-25s vs %-25s [%s]\n" "$MDATE" "$HOME" "$AWAY" "$STATUS"
                done
            elif [[ "$MATCH_COUNT" -gt 0 ]]; then
                printf "  ${CYAN}INFO${RESET}  No new matches in last 5 min (may be duplicates/skipped)\n"
            else
                printf "  ${YELLOW}WARN${RESET}  No matches from agent found in DB\n"
            fi
        else
            printf "  ${YELLOW}WARN${RESET}  Could not query database (psql connect failed)\n"
            printf "  ${CYAN}INFO${RESET}  Try: psql -h 127.0.0.1 -p %s -U postgres -d postgres\n" "$DB_PORT"
        fi
    else
        printf "  ${DIM}SKIP${RESET}  psql not installed — cannot query matches table\n"
        printf "  ${CYAN}INFO${RESET}  View at: http://127.0.0.1:54323 (Supabase Studio)\n"
    fi

    # ── Summary ────────────────────────────────────────────────────────
    printf "\n${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
    if [[ $AGENT_EXIT -eq 0 ]]; then
        printf "${GREEN}Agent completed successfully${RESET}\n"
    else
        printf "${RED}Agent failed (exit %d)${RESET}\n" "$AGENT_EXIT"
    fi
    printf "  Worker log: %s\n" "$WORKER_LOG"
    printf "  Supabase Studio: http://127.0.0.1:54323\n"
    printf "  RabbitMQ UI: http://localhost:15672\n"
    printf "${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n\n"

    exit $AGENT_EXIT

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PROD MODE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
elif [[ "$ENV" == "prod" ]]; then

    # Verify kubectl access
    printf "${BOLD}Using kubectl context: %s${RESET}\n" "$KUBE_CONTEXT"
    if ! kubectl --context "$KUBE_CONTEXT" cluster-info >/dev/null 2>&1; then
        printf "${RED}Cannot reach cluster (context: %s).${RESET}\n" "$KUBE_CONTEXT"
        printf "Check: kubectl config get-contexts %s\n" "$KUBE_CONTEXT"
        exit 1
    fi

    # Verify CronJob exists
    if ! kubectl --context "$KUBE_CONTEXT" get cronjob "$CRONJOB_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
        printf "${RED}CronJob '%s' not found in namespace '%s'.${RESET}\n" "$CRONJOB_NAME" "$NAMESPACE"
        printf "Deploy first: kubectl --context %s apply -f k3s/match-scraper-agent/\n" "$KUBE_CONTEXT"
        exit 1
    fi

    # Generate a unique job name
    TIMESTAMP=$(date +%Y%m%d-%H%M%S)
    JOB_NAME="${CRONJOB_NAME}-manual-${TIMESTAMP}"

    # Build the Job, optionally overriding args for --dry-run
    if [[ "$DRY_RUN" == true ]]; then
        printf "${BOLD}Creating Job (dry-run): %s${RESET}\n" "$JOB_NAME"
        kubectl --context "$KUBE_CONTEXT" create job "$JOB_NAME" \
            --from="cronjob/$CRONJOB_NAME" \
            -n "$NAMESPACE" \
            --dry-run=none \
            -o json \
            | jq '.spec.template.spec.containers[0].args = ["run", "--env", "prod", "--dry-run"]' \
            | kubectl --context "$KUBE_CONTEXT" apply -f -
    else
        printf "${BOLD}Creating Job: %s${RESET}\n" "$JOB_NAME"
        kubectl --context "$KUBE_CONTEXT" create job "$JOB_NAME" \
            --from="cronjob/$CRONJOB_NAME" \
            -n "$NAMESPACE"
    fi

    printf "  ${GREEN}Job created${RESET}\n"
    printf "  ${DIM}kubectl get job %s -n %s${RESET}\n" "$JOB_NAME" "$NAMESPACE"

    # Follow logs if requested
    if [[ "$FOLLOW" == true ]]; then
        printf "\n${BOLD}Waiting for pod to start...${RESET}\n"
        kubectl --context "$KUBE_CONTEXT" wait --for=condition=Ready \
            -l "job-name=$JOB_NAME" \
            pod -n "$NAMESPACE" \
            --timeout=60s 2>/dev/null || true

        printf "${BOLD}Tailing logs:${RESET}\n\n"
        kubectl --context "$KUBE_CONTEXT" logs -f -l "job-name=$JOB_NAME" -n "$NAMESPACE"

        # Show final job status
        printf "\n${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
        STATUS=$(kubectl --context "$KUBE_CONTEXT" get job "$JOB_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[0].type}' 2>/dev/null || echo "Unknown")
        if [[ "$STATUS" == "Complete" ]]; then
            printf "${GREEN}Job completed successfully${RESET}\n"
        else
            printf "${RED}Job status: %s${RESET}\n" "$STATUS"
            printf "Details: kubectl describe job %s -n %s\n" "$JOB_NAME" "$NAMESPACE"
        fi
    else
        printf "\nTo follow logs:\n"
        printf "  kubectl --context %s logs -f -l job-name=%s -n %s\n\n" "$KUBE_CONTEXT" "$JOB_NAME" "$NAMESPACE"
    fi

fi
