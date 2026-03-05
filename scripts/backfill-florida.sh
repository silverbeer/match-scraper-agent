#!/bin/bash
#
# Backfill Florida HG matches from fall 2025 into Missing Table.
#
# Usage:
#   ./scripts/backfill-florida.sh                    # Show plan (dry run)
#   ./scripts/backfill-florida.sh --run              # Run week 1 (U14)
#   ./scripts/backfill-florida.sh --run --week 3     # Run specific week
#   ./scripts/backfill-florida.sh --run --age u13    # Run U13 instead of U14
#   ./scripts/backfill-florida.sh --status           # Check job status
#   ./scripts/backfill-florida.sh --cleanup          # Delete completed backfill jobs
#

set -e

NAMESPACE="match-scraper"
IMAGE="ghcr.io/silverbeer/match-scraper-agent:latest"

# Weekly date ranges (2025-08-28 to 2025-12-01)
WEEKS=(
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

# Parse arguments
ACTION="plan"
WEEK_NUM=""
AGE_GROUP="u14"

while [[ $# -gt 0 ]]; do
  case $1 in
    --run) ACTION="run"; shift ;;
    --status) ACTION="status"; shift ;;
    --cleanup) ACTION="cleanup"; shift ;;
    --week) WEEK_NUM="$2"; shift 2 ;;
    --age) AGE_GROUP="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

TARGET="${AGE_GROUP}-hg-florida"

if [[ "$ACTION" == "status" ]]; then
  echo "Backfill job status:"
  kubectl get jobs -n "$NAMESPACE" -l task=backfill --sort-by=.metadata.creationTimestamp
  exit 0
fi

if [[ "$ACTION" == "cleanup" ]]; then
  echo "Deleting completed backfill jobs..."
  kubectl delete jobs -n "$NAMESPACE" -l task=backfill --field-selector status.successful=1
  exit 0
fi

if [[ "$ACTION" == "plan" ]]; then
  echo "Florida HG Backfill Plan (${AGE_GROUP^^})"
  echo "========================================="
  echo ""
  for i in "${!WEEKS[@]}"; do
    read -r from_date to_date <<< "${WEEKS[$i]}"
    week=$((i + 1))
    printf "  Week %2d: %s to %s\n" "$week" "$from_date" "$to_date"
  done
  echo ""
  echo "To run week 1:  $0 --run --week 1 --age $AGE_GROUP"
  echo "To run all:     $0 --run --age $AGE_GROUP"
  echo "To check:       $0 --status"
  exit 0
fi

# ACTION == "run"
run_week() {
  local week_idx=$1
  local from_date to_date
  read -r from_date to_date <<< "${WEEKS[$week_idx]}"
  local week_num=$((week_idx + 1))
  local week_padded
  week_padded=$(printf "%02d" "$week_num")
  local job_name="backfill-florida-${AGE_GROUP}-week${week_padded}"

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
    division: florida
    age-group: ${AGE_GROUP}
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

if [[ -n "$WEEK_NUM" ]]; then
  # Run a single week
  week_idx=$((WEEK_NUM - 1))
  if [[ $week_idx -lt 0 || $week_idx -ge ${#WEEKS[@]} ]]; then
    echo "Invalid week number: $WEEK_NUM (valid: 1-${#WEEKS[@]})"
    exit 1
  fi
  echo "Running backfill week $WEEK_NUM for ${AGE_GROUP^^} Florida HG..."
  run_week "$week_idx"
else
  # Run all weeks
  echo "Running ALL backfill weeks for ${AGE_GROUP^^} Florida HG..."
  for i in "${!WEEKS[@]}"; do
    run_week "$i"
  done
fi

echo ""
echo "Check status: $0 --status"
echo "View logs:    kubectl logs -n $NAMESPACE -l task=backfill --tail=50 -f"
