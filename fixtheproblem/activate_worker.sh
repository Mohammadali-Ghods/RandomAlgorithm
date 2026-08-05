#!/usr/bin/env bash
# Activate Half B — the SCOPED fixer worker. Run this YOURSELF and watch each
# step; it mounts your Claude credentials into an auto-triggered agent, so it
# should run under your own eyes, not from an automated session.
#
#   bash activate_worker.sh
#
# Security model (enforced by what is / isn't mounted):
#   MOUNTED : fp-queue volume (the job protocol) + your Claude auth (read-only)
#             + baked-in COPIES of the bot code (no env files)
#   NOT MOUNTED : /root/projects/MexcIntegration (raw exchange keys),
#             /var/run/docker.sock, watchdog.env, fixproblem.env
# => the agent can fix the makers via their HTTP API / edit code copies, but
#    cannot read fund keys, withdraw, or escape the container.
set -euo pipefail
cd "$(dirname "$0")"
SRC=".."                      # where the bot .py files live (parent dir)

echo "== 1) stage bot code copies (NO env files) into workspace_src/ =="
rm -rf workspace_src && mkdir -p workspace_src
for f in mm_server.py panel_server.py orders.py manual_server.py algorithm_a.py; do
  cp "$SRC/$f" workspace_src/ 2>/dev/null && echo "  + $f" || echo "  (skip $f)"
done
# hard guarantee: never ship any secret into the image
rm -f workspace_src/*.env workspace_src/.env* 2>/dev/null || true

echo "== 2) build the worker image =="
docker build -f Dockerfile.worker -t exuno-fixproblem-worker:latest .

echo "== 3) locate Claude auth to mount (read-only) =="
CJSON="$HOME/.claude.json"; CDIR="$HOME/.claude"
[ -f "$CJSON" ] || { echo "!! $CJSON not found — claude CLI not logged in on this host"; exit 1; }

echo "== 4) run the worker (scoped) =="
docker rm -f exuno-fixproblem-worker >/dev/null 2>&1 || true
# Creds are mounted READ-ONLY at /seed, then copied to a WRITABLE /root/.claude
# inside the container (the runtime writes session-env there — a :ro mount breaks
# the agent's Bash with EROFS). projects/ is dropped so host memory/transcripts
# stay private. IS_SANDBOX=1 lets --dangerously-skip-permissions run as root.
docker run -d --name exuno-fixproblem-worker --restart unless-stopped \
  --network npm_default \
  -v fp-queue:/queue \
  -v "$CJSON":/seed/.claude.json:ro \
  -v "$CDIR":/seed/.claude:ro \
  -e FP_AGENT_MODEL=claude-opus-4-8 \
  -e FP_AGENT_PERM_MODE=bypassPermissions \
  -e IS_SANDBOX=1 \
  -e FP_AGENT_TIMEOUT=3600 \
  `# --- OPTIONAL: uncomment to let the agent cancel/replace orders ---` \
  `# -e EXUNO_BASE_URL=https://integrate.exuno.io -e EXUNO_TOKEN=<mexc-bearer>` \
  `# -e EXUNO_BASE_URL_COINW=https://integrate1.exuno.io -e EXUNO_TOKEN_COINW=<coinw-bearer>` \
  exuno-fixproblem-worker:latest \
  sh -c 'cp /seed/.claude.json /root/.claude.json && mkdir -p /root/.claude && cp -a /seed/.claude/. /root/.claude/ 2>/dev/null; rm -rf /root/.claude/projects; chmod -R u+rwX /root/.claude /root/.claude.json && exec python3 /app/fixproblem_worker.py'
echo
echo "== 5) verify =="
sleep 3
docker logs exuno-fixproblem-worker 2>&1 | tail -8
echo
echo "If auth failed (mounting ~/.claude did not authenticate the headless CLI),"
echo "re-run step 4 adding:  -e CLAUDE_CODE_OAUTH_TOKEN=<token from: claude setup-token>"
echo "Then dispatch a test from a real incident link and watch the transcript."
