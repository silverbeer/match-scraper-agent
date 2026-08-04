#!/usr/bin/env bash
# Deploy match-scraper-agent stack to K3s.
#
# ⚠️  DO NOT RUN THIS AGAINST rancher-desktop — it is production for the
# scraper pipeline, and this script deploys more than the agent:
#
#   * rabbitmq/{pvc,deployment,service}.yaml stands up a SECOND broker. The
#     Celery workers consume from messaging-rabbitmq (the messaging-platform
#     Helm release), not from this one.
#   * qop-rankings/cronjob.yaml is deliberately not deployed — see SB-544.
#
# The ConfigMap itself is safe to apply as of SB-558: its RabbitMQ hostname
# now matches the live broker, and the dead LLM-era keys are gone. Apply
# individual manifests:
#
#   kubectl apply -f k3s/match-scraper-agent/configmap.yaml
#   kubectl apply -f k3s/match-scraper-agent/cronjob.yaml
#   kubectl apply -f k3s/release-watch/cronjob.yaml
#
# Applies: namespace → RabbitMQ (pvc, deployment, service) →
#          match-scraper-agent (configmap, secret, cronjob) →
#          qop-rankings (cronjob) → release-watch (cronjob)
#
# Reads envs/.env.prod for secrets and generates the K8s Secret manifest.
#
# Required vars in envs/.env.prod:
#   AGENT_MISSING_TABLE_API_KEY   — missing-table API key
#   AGENT_TELEGRAM_BOT_TOKEN      — Telegram bot token for notifications
#   AGENT_TELEGRAM_CHAT_ID        — Telegram chat to notify

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/envs/.env.prod"

# --- Load envs/.env.prod ---
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: $ENV_FILE not found"
  echo ""
  echo "Create it with at least:"
  echo "  AGENT_MISSING_TABLE_API_KEY=..."
  echo "  AGENT_TELEGRAM_BOT_TOKEN=..."
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

# --- Validate required vars ---
missing=()
[[ -z "${AGENT_MISSING_TABLE_API_KEY+x}" ]] && missing+=("AGENT_MISSING_TABLE_API_KEY")
[[ -z "${AGENT_TELEGRAM_BOT_TOKEN:-}" ]]    && missing+=("AGENT_TELEGRAM_BOT_TOKEN")

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Error: missing required vars in $ENV_FILE:"
  printf '  %s\n' "${missing[@]}"
  exit 1
fi

# --- Generate K8s Secret manifest ---
SECRET_FILE="$SCRIPT_DIR/match-scraper-agent/secret.yaml"

cat > "$SECRET_FILE" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: match-scraper-agent-secret
  namespace: match-scraper
type: Opaque
stringData:
  AGENT_MISSING_TABLE_API_KEY: "$AGENT_MISSING_TABLE_API_KEY"
  AGENT_TELEGRAM_BOT_TOKEN: "$AGENT_TELEGRAM_BOT_TOKEN"
  AGENT_TELEGRAM_CHAT_ID: "${AGENT_TELEGRAM_CHAT_ID:-}"
EOF

echo "Generated $SECRET_FILE"

# --- Apply manifests ---
echo ""
echo "Applying namespace..."
kubectl apply -f "$SCRIPT_DIR/namespace.yaml"

echo ""
echo "Applying RabbitMQ..."
kubectl apply -f "$SCRIPT_DIR/rabbitmq/configmap.yaml"
kubectl apply -f "$SCRIPT_DIR/rabbitmq/pvc.yaml"
kubectl apply -f "$SCRIPT_DIR/rabbitmq/deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/rabbitmq/service.yaml"

echo ""
echo "Waiting for RabbitMQ rollout..."
kubectl rollout status deployment/rabbitmq -n match-scraper --timeout=60s

echo ""
echo "Applying match-scraper-agent..."
kubectl apply -f "$SCRIPT_DIR/match-scraper-agent/pvc.yaml"
kubectl apply -f "$SCRIPT_DIR/match-scraper-agent/configmap.yaml"
kubectl apply -f "$SECRET_FILE"
kubectl apply -f "$SCRIPT_DIR/match-scraper-agent/cronjob.yaml"

echo ""
echo "Applying QoP rankings scraper..."
kubectl apply -f "$SCRIPT_DIR/qop-rankings/cronjob.yaml"

echo ""
echo "Applying schedule release watcher..."
kubectl apply -f "$SCRIPT_DIR/release-watch/cronjob.yaml"

echo ""
echo "Done. Verify with:"
echo "  kubectl get all -n match-scraper"
echo "  kubectl get pods -n match-scraper"
echo "  ./scripts/preflight.sh prod"
