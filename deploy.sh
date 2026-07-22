#!/usr/bin/env bash
# Deploy the Exuno market panel behind the existing nginx-proxy-manager.
# Usage:
#   export NPM_PASS='...your password...'      # NPM admin + basic-auth password
#   ./deploy.sh                                # default -> market.exuno.io
#   NAME=exuno-market-panel1 DOMAIN=market1.exuno.io IMAGE=exuno-market-panel1:latest \
#     ./deploy.sh                              # a second, independent instance
set -euo pipefail

# Everything is overridable so a second instance never touches the first.
IMAGE="${IMAGE:-exuno-market-panel:latest}"
NAME="${NAME:-exuno-market-panel}"
DOMAIN="${DOMAIN:-market.exuno.io}"
NETWORK=npm_default          # nginx-proxy-manager's docker network

echo "==> Building image"
docker build -t "$IMAGE" .

echo "==> (Re)starting container on $NETWORK (no host port published)"
: "${EXUNO_TOKEN:?set EXUNO_TOKEN to the Exuno API bearer token}"
docker rm -f "$NAME" 2>/dev/null || true
docker run -d --name "$NAME" --network "$NETWORK" --restart unless-stopped \
  -e EXUNO_TOKEN="$EXUNO_TOKEN" \
  -e EXUNO_BASE_URL="${EXUNO_BASE_URL:-https://integrate.exuno.io}" \
  -e EXCHANGE_NAME="${EXCHANGE_NAME:-MEXC}" \
  -e MIN_ORDER_USDT="${MIN_ORDER_USDT:-1.05}" \
  -e PANEL_QUANTITY="${PANEL_QUANTITY:-20}" \
  -e BUDGET_USDT="${BUDGET_USDT:-0}" \
  -e BUDGET_UNP_USDT="${BUDGET_UNP_USDT:-0}" \
  "$IMAGE"

echo "==> Waiting for the panel to answer on the docker network"
sleep 4
docker exec nginx-proxy-manager sh -c \
  "curl -sf -o /dev/null http://$NAME:8787/api/state && echo OK" \
  || { echo "Panel not reachable from NPM network"; exit 1; }

echo "==> Configuring nginx-proxy-manager for $DOMAIN (proxy host + basic auth + SSL)"
NPM_USER="${NPM_USER:-info@botify.trade}" \
NPM_PASS="${NPM_PASS:?set NPM_PASS to the admin/basic-auth password}" \
DOMAIN="$DOMAIN" UPSTREAM_HOST="$NAME" \
python3 npm_setup.py

echo "==> Done. Test:  https://$DOMAIN  (expects a login prompt)"
