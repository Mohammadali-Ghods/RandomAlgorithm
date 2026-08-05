"""FixTheProblem — scoped fixer worker.

Drains the job-folder queue (shared /queue volume) and runs a SCOPED Claude
agent per job. Security is structural: this container mounts the bot code +
integration order-tokens + the makers' HTTP API, but NOT the raw exchange key
files, NOT the docker socket, and NOT any withdrawal path — so the agent can
fix the makers (config/resync/stop-start/cancel-replace/edit code) but cannot
read fund keys, withdraw, or escape the sandbox.

Queue protocol (per job dir): task.md (brief) · job.json (ids) · status
(queued/running/done/failed) · transcript.jsonl (chat) · followups.jsonl
(admin replies) · session_id (Claude resume id) · .kill (admin pressed Exit).

Gotchas handled (from the design brief): child stdin closed; large output
captured via PIPE; the prompt is never echoed into error text; a superseded /
killed run is terminated; model output is parsed by walking to the matching
brace, not naive json.loads.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

QUEUE_DIR = Path(os.environ.get("FP_QUEUE_DIR", "/queue"))
POLL_SEC = float(os.environ.get("FP_WORKER_POLL", "3"))
MODEL = os.environ.get("FP_AGENT_MODEL", "claude-opus-4-8")
TIMEOUT = int(os.environ.get("FP_AGENT_TIMEOUT", "3600"))
# The container IS the sandbox, so bypassPermissions here is still scoped by the
# mounts. Override to a stricter mode via env if desired.
PERM_MODE = os.environ.get("FP_AGENT_PERM_MODE", "bypassPermissions")

SYSTEM_SCOPE = (
    "You are the on-call fixer for two live UNP/USDT market makers: market4 (MEXC, "
    "container exuno-market-panel4) and market5 (CoinW, container exuno-market-coinw-mm). "
    "Each maker exposes an HTTP API on port 8787: /api/state, /api/config (POST to change "
    "sizing/levels — NEVER price), /api/stop, /api/start, /api/resync. You MAY: read state/logs, "
    "adjust config, stop/start/resync, cancel/replace orders via the integration client, and "
    "edit the bot code in /workspace. You MUST NOT: change the quoted price band, attempt to "
    "read exchange key files or withdraw funds (you have no access to either), or touch any "
    "market other than the one in the task. Report clearly what you found and every change you "
    "made. The admin may reply with follow-ups — continue the conversation."
)


def log(m):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)


def _append(d: Path, role: str, text: str):
    with open(d / "transcript.jsonl", "a") as f:
        f.write(json.dumps({"role": role, "text": text, "ts": time.time()}) + "\n")


def _status(d: Path, s: str):
    (d / "status").write_text(s)


def _read_status(d: Path) -> str:
    p = d / "status"
    return p.read_text().strip() if p.exists() else "unknown"


def _extract_text(stdout: str) -> str:
    """Claude --output-format json returns an object with a 'result' field. Fall
    back to walking to the first balanced {...} if extra prose wraps it."""
    stdout = (stdout or "").strip()
    if not stdout:
        return ""
    try:
        return str(json.loads(stdout).get("result", stdout))
    except Exception:
        pass
    start = stdout.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(stdout)):
            if stdout[i] == "{":
                depth += 1
            elif stdout[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return str(json.loads(stdout[start:i + 1]).get("result", stdout))
                    except Exception:
                        break
    return stdout  # plain text reply


def _run_claude(prompt: str, resume_id: str | None, kill_flag: Path):
    """Run one Claude turn. Returns (text, session_id). Honors the .kill flag."""
    cmd = ["claude", "-p", prompt, "--model", MODEL,
           "--permission-mode", PERM_MODE, "--output-format", "json",
           "--append-system-prompt", SYSTEM_SCOPE]
    if resume_id:
        cmd += ["--resume", resume_id]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd="/workspace", text=True, start_new_session=True)
    started = time.time()
    while True:
        try:
            out, err = proc.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            if kill_flag.exists() or (time.time() - started) > TIMEOUT:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass
                return ("⛔ run stopped (admin Exit or timeout).", resume_id)
    text = _extract_text(out)
    sid = resume_id
    try:
        j = json.loads(out)
        sid = j.get("session_id") or j.get("sessionId") or resume_id
    except Exception:
        pass
    if not text and err:
        # never leak the prompt/argv from stderr into the transcript
        text = "⚠️ the agent produced no output (internal error)."
    return (text, sid)


def process(d: Path):
    kill_flag = d / ".kill"
    if kill_flag.exists():
        _status(d, "failed"); _append(d, "agent", "Stopped by admin (Exit).")
        return
    sid_file = d / "session_id"
    resume_id = sid_file.read_text().strip() if sid_file.exists() else None

    # first run uses task.md; a resume uses the newest unprocessed follow-up
    if resume_id is None:
        prompt = (d / "task.md").read_text()
    else:
        fups = d / "followups.jsonl"
        pending = []
        if fups.exists():
            done_n = int((d / ".fup_done").read_text()) if (d / ".fup_done").exists() else 0
            lines = [l for l in fups.read_text().splitlines() if l.strip()]
            pending = lines[done_n:]
            (d / ".fup_done").write_text(str(len(lines)))
        if not pending:
            _status(d, "done"); return
        prompt = "\n".join(json.loads(l).get("text", "") for l in pending)

    _status(d, "running")
    log(f"{d.name}: running ({'resume' if resume_id else 'new'})")
    text, sid = _run_claude(prompt, resume_id, kill_flag)
    if sid:
        sid_file.write_text(sid)
    _append(d, "agent", text)
    # assert an artifact exists (a real reply), not just exit 0
    _status(d, "done" if text and not text.startswith("⚠️") else "failed")
    log(f"{d.name}: done")


def main():
    log(f"fixproblem_worker start — queue={QUEUE_DIR} model={MODEL} perm={PERM_MODE}")
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            for d in sorted(QUEUE_DIR.glob("job_*")):
                if not d.is_dir():
                    continue
                st = _read_status(d)
                if (d / ".kill").exists() and st not in ("done", "failed"):
                    try:
                        (d / "session_id").exists()
                    except Exception:
                        pass
                    _status(d, "failed"); _append(d, "agent", "Stopped by admin (Exit).")
                    continue
                if st == "queued":
                    process(d)
        except Exception as e:
            log(f"loop error: {type(e).__name__}: {str(e)[:200]}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
