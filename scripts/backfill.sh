#!/usr/bin/env bash
# backfill.sh — Generate and optionally apply a k3s backfill Job for one or more targets.
#
# Usage:
#   ./scripts/backfill.sh --targets u14-hg,u15-hg --from 2026-03-14 --to 2026-03-16
#   ./scripts/backfill.sh --targets u14-hg --from 2026-03-14 --to 2026-03-16 --apply
#
# Generated YAML is written to ~/.local/share/match-scraper-agent/backfills/
# and is NOT committed to the repo (k3s/backfill/ is gitignored).
#
# Options:
#   --targets   Comma-separated list of target keys (e.g. u14-hg,u15-hg,u16-hg)
#   --from      Start date (YYYY-MM-DD)
#   --to        End date (YYYY-MM-DD)
#   --apply     Apply the generated YAML to the cluster via kubectl
#   --dry-run   Add --dry-run flag to scrape command (no submission)
#   --env       Environment (default: prod)

set -euo pipefail

TARGETS=""
FROM_DATE=""
TO_DATE=""
APPLY=false
SCRAPE_DRY_RUN=false
ENV="prod"

while [[ $# -gt 0 ]]; do
  case $1 in
    --targets) TARGETS="$2"; shift 2 ;;
    --from)    FROM_DATE="$2"; shift 2 ;;
    --to)      TO_DATE="$2"; shift 2 ;;
    --apply)   APPLY=true; shift ;;
    --dry-run) SCRAPE_DRY_RUN=true; shift ;;
    --env)     ENV="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -z "$TARGETS" || -z "$FROM_DATE" || -z "$TO_DATE" ]]; then
  echo "Usage: $0 --targets <targets> --from <YYYY-MM-DD> --to <YYYY-MM-DD> [--apply] [--dry-run] [--env prod]"
  exit 1
fi

OUTDIR="${HOME}/.local/share/match-scraper-agent/backfills"
mkdir -p "$OUTDIR"

SLUG="${FROM_DATE//-/}"
OUTFILE="${OUTDIR}/backfill-${SLUG}.yaml"

echo "# Backfill job: targets=${TARGETS} from=${FROM_DATE} to=${TO_DATE} env=${ENV}" > "$OUTFILE"
echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUTFILE"

FIRST=true
IFS=',' read -ra TARGET_LIST <<< "$TARGETS"
for TARGET in "${TARGET_LIST[@]}"; do
  TARGET=$(echo "$TARGET" | xargs)  # trim whitespace
  JOB_NAME="backfill-${TARGET}-${SLUG}"

  ARGS="- scrape\n            - --target\n            - ${TARGET}\n            - --from\n            - \"${FROM_DATE}\"\n            - --to\n            - \"${TO_DATE}\"\n            - --submit\n            - --env\n            - ${ENV}"
  if [[ "$SCRAPE_DRY_RUN" == "true" ]]; then
    ARGS="${ARGS}\n            - --dry-run"
  fi

  if [[ "$FIRST" == "false" ]]; then
    echo "---" >> "$OUTFILE"
  fi
  FIRST=false

  cat >> "$OUTFILE" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: match-scraper
  labels:
    app: match-scraper-agent
    task: backfill
    target: ${TARGET}
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
          image: ghcr.io/silverbeer/match-scraper-agent:latest
          args:
$(echo -e "$ARGS" | sed 's/^/            /')
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
EOF
done

echo "Generated: ${OUTFILE}"

if [[ "$APPLY" == "true" ]]; then
  echo "Applying to cluster..."
  kubectl apply -f "$OUTFILE"
  echo "Done. Monitor with: kubectl logs -n match-scraper -l task=backfill --follow"
else
  echo "Dry run — review then apply with:"
  echo "  kubectl apply -f ${OUTFILE}"
fi
