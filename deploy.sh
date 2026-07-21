#!/usr/bin/env bash
# Deploy the Exuno market panel behind the existing nginx-proxy-manager.
# Usage:
#   export NPM_PASS='...your password...'      # NPM admin + basic-auth password
#   ./deploy.sh
set -euo pipefail

IMAGE=exuno-market-panel:latest
NAME=exuno-market-panel
NETWORK=npm_default          # nginx-proxy-manager's docker network

echo "==> Building image"
docker build -t "$IMAGE" .

echo "==> (Re)starting container on $NETWORK (no host port published)"
: "${EXUNO_TOKEN:?set EXUNO_TOKEN to the Exuno API bearer token}"
docker rm -f "$NAME" 2>/dev/null || true
docker run -d --name "$NAME" --network "$NETWORK" --restart unless-stopped \
  -e EXUNO_TOKEN="$EXUNO_TOKEN" "$IMAGE"

echo "==> Waiting for the panel to answer on the docker network"
sleep 4
docker exec nginx-proxy-manager sh -c \
  "curl -sf -o /dev/null http://$NAME:8787/api/state && echo OK" \
  || { echo "Panel not reachable from NPM network"; exit 1; }

echo "==> Configuring nginx-proxy-manager (proxy host + basic auth + SSL)"
NPM_USER="${NPM_USER:-info@botify.trade}" \
NPM_PASS="${NPM_PASS:?set NPM_PASS to the admin/basic-auth password}" \
python3 npm_setup.py

echo "==> Done. Test:  https://market.exuno.io  (expects a login prompt)"
