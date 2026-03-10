#!/bin/bash
#
# Backfill U15 HG Northeast matches from fall 2025 + spring 2026 into Missing Table.
#
# Usage:
#   ./scripts/backfill-u15-northeast.sh                          # Show plan (dry run)
#   ./scripts/backfill-u15-northeast.sh --run                    # Run week 1
#   ./scripts/backfill-u15-northeast.sh --run --week 3           # Run specific week
#   ./scripts/backfill-u15-northeast.sh --run --season fall      # Run all fall weeks
#   ./scripts/backfill-u15-northeast.sh --run --season spring    # Run all spring weeks
#   ./scripts/backfill-u15-northeast.sh --status                 # Check job status
#   ./scripts/backfill-u15-northeast.sh --cleanup                # Delete completed backfill jobs
#

set -e

NAMESPACE="match-scraper"
IMAGE="ghcr.io/silverbeer/match-scraper-agent:latest"
TARGET="u15-hg"

# Fall 2025: weekly date ranges (2025-08-28 to 2025-12-01)
FALL_WEEKS=(
  "2025-08-28 2025-09-04"
  "2025-09-04 2025-09-11"
  "2025-09-11 2025-09-18"
  "2025-09-18 2025-09-25"
  "2025-09-25 2025-10-02"
  "2025-10-02 2025-10-09"
  "2025-10-09 2025-10-16"
  "2025-10-16 2025-10-23"
  "2025-10-23 2025-10-30"
  "2025-10-30 2025-11-06"
  "2025-11-06 2025-11-13"
  "2025-11-13 2025-11-20"
  "2025-11-20 2025-11-27"
  "2025-11-27 2025-12-01"
)

# Spring 2026: weekly date ranges (2026-03-01 to 2026-06-30)
SPRING_WEEKS=(
  "2026-03-01 2026-03-08"
  "2026-03-08 2026-03-15"
  "2026-03-15 2026-03-22"
  "2026-03-22 2026-03-29"
  "2026-03-29 2026-04-05"
  "2026-04-05 2026-04-12"
  "2026-04-12 2026-04-19"
  "2026-04-19 2026-04-26"
  "2026-04-26 2026-05-03"
  "2026-05-03 2026-05-10"
  "2026-05-10 2026-05-17"
  "2026-05-17 2026-05-24"
  "2026-05-24 2026-05-31"
  "2026-05-31 2026-06-07"
  "2026-06-07 2026-06-14"
  "2026-06-14 2026-06-21"
  "2026-06-21 2026-06-30"
)

# Parse arguments
ACTION="plan"
WEEK_NUM=""
SEASON_FILTER=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --run) ACTION="run"; shift ;;
    --status) ACTION="status"; shift ;;
    --cleanup) ACTION="cleanup"; shift ;;
    --week) WEEK_NUM="$2"; shift 2 ;;
    --season) SEASON_FILTER="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$ACTION" == "status" ]]; then
  echo "Backfill job status:"
  kubectl get jobs -n "$NAMESPACE" -l task=backfill,division=northeast,age-group=u15 --sort-by=.metadata.creationTimestamp
  exit 0
fi

if [[ "$ACTION" == "cleanup" ]]; then
  echo "Deleting completed U15 Northeast backfill jobs..."
  kubectl delete jobs -n "$NAMESPACE" -l task=backfill,division=northeast,age-group=u15 --field-selector status.successful=1
  exit 0
fi

if [[ "$ACTION" == "plan" ]]; then
  echo "U15 HG Northeast Backfill Plan"
  echo "========================================="
  echo ""
  echo "Fall 2025 (14 weeks):"
  for i in "${!FALL_WEEKS[@]}"; do
    read -r from_date to_date <<< "${FALL_WEEKS[$i]}"
    week=$((i + 1))
    printf "  Week %2d: %s to %s\n" "$week" "$from_date" "$to_date"
  done
  echo ""
  echo "Spring 2026 (17 weeks):"
  for i in "${!SPRING_WEEKS[@]}"; do
    read -r from_date to_date <<< "${SPRING_WEEKS[$i]}"
    week=$((i + 1))
    printf "  Week %2d: %s to %s\n" "$week" "$from_date" "$to_date"
  done
  echo ""
  echo "To run fall week 1:    $0 --run --season fall --week 1"
  echo "To run all fall:       $0 --run --season fall"
  echo "To run all spring:     $0 --run --season spring"
  echo "To run everything:     $0 --run"
  echo "To check:              $0 --status"
  exit 0
fi

# ACTION == "run"
run_week() {
  local season=$1
  local week_idx=$2
  local from_date to_date

  if [[ "$season" == "fall" ]]; then
    read -r from_date to_date <<< "${FALL_WEEKS[$week_idx]}"
  else
    read -r from_date to_date <<< "${SPRING_WEEKS[$week_idx]}"
  fi

  local week_num=$((week_idx + 1))
  local week_padded
  week_padded=$(printf "%02d" "$week_num")
  local job_name="backfill-u15-northeast-${season}-week${week_padded}"

  # Check if job already exists
  if kubectl get job "$job_name" -n "$NAMESPACE" &>/dev/null; then
    echo "  [SKIP] $job_name already exists"
    return
  fi

  echo "  [RUN] $job_name: $TARGET $from_date to $to_date"

  kubectl apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${NAMESPACE}
  labels:
    app: match-scraper-agent
    task: backfill
    division: northeast
    age-group: u15
    season: ${season}
spec:
  backoffLimit: 1
  activeDeadlineSeconds: 1800
  template:
    metadata:
      labels:
        app: match-scraper-agent
        task: backfill
    spec:
      restartPolicy: Never
      containers:
        - name: agent
          image: ${IMAGE}
          args:
            - scrape
            - --target
            - ${TARGET}
            - --from
            - "${from_date}"
            - --to
            - "${to_date}"
            - --submit
            - --env
            - prod
          envFrom:
            - configMapRef:
                name: match-scraper-agent-config
            - secretRef:
                name: match-scraper-agent-secret
          resources:
            requests:
              cpu: 200m
              memory: 1Gi
            limits:
              cpu: "1"
              memory: 2Gi
YAML
}

run_season() {
  local season=$1
  local weeks_ref

  if [[ "$season" == "fall" ]]; then
    weeks_ref=("${FALL_WEEKS[@]}")
  else
    weeks_ref=("${SPRING_WEEKS[@]}")
  fi

  echo "Running ALL backfill weeks for U15 HG Northeast (${season})..."
  for i in "${!weeks_ref[@]}"; do
    run_week "$season" "$i"
  done
}

if [[ -n "$WEEK_NUM" ]]; then
  # Single week — require --season
  if [[ -z "$SEASON_FILTER" ]]; then
    echo "Error: --week requires --season (fall or spring)"
    exit 1
  fi

  if [[ "$SEASON_FILTER" == "fall" ]]; then
    max=${#FALL_WEEKS[@]}
  else
    max=${#SPRING_WEEKS[@]}
  fi

  week_idx=$((WEEK_NUM - 1))
  if [[ $week_idx -lt 0 || $week_idx -ge $max ]]; then
    echo "Invalid week number: $WEEK_NUM (valid: 1-${max})"
    exit 1
  fi

  echo "Running backfill ${SEASON_FILTER} week $WEEK_NUM for U15 HG Northeast..."
  run_week "$SEASON_FILTER" "$week_idx"
elif [[ -n "$SEASON_FILTER" ]]; then
  # All weeks for one season
  run_season "$SEASON_FILTER"
else
  # All weeks, both seasons
  run_season "fall"
  run_season "spring"
fi

echo ""
echo "Check status: $0 --status"
echo "View logs:    kubectl logs -n $NAMESPACE -l task=backfill,age-group=u15 --tail=50 -f"
