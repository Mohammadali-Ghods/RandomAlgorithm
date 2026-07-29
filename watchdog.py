#!/usr/bin/env python3
"""Bot watchdog — 30s always-on background monitor for market1 (MEXC) and
market2 (CoinW).

For each market every cycle it checks, and auto-remediates:
  1. Integration API healthy?      (mexc-integration / coinw-integration)
     -> if unhealthy/hung/exited, restart that container, wait, re-verify.
  2. Panel container up + answering (exuno-market-panel1 / -2)
     -> if down/unreachable, restart it.
  3. Bot running-state             (via the panel's own /api/state)
     -> if the bot was running and got knocked out by a crash/restart,
        start it again and verify it comes back running.

It respects an intentional Stop: if you stop a bot yourself (running=false with
no error, container unchanged) it records that as the desired state and will NOT
restart it. Anything it cannot fix after retries is written as an ALERT line in
the log for a human / Opus to handle.

Everything is done with local `docker` (exec/inspect/restart) — no dependency on
Cloudflare or the public domains. Stop it with:  kill $(cat watchdog.pid)
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "watchdog.log")
PIDFILE = os.path.join(HERE, "watchdog.pid")
STATEFILE = os.path.join(HERE, "watchdog_state.json")
INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "30"))
PANEL_PORT = "8787"
INTEG_PORT = "8080"

# market -> (panel container, integration container)
TARGETS = {
    "market1": {"panel": "exuno-market-panel1", "integration": "mexc-integration"},
    "market2": {"panel": "exuno-market-panel2", "integration": "coinw-integration"},
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def log(msg, level="info"):
    line = f"{now()} [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def alert(msg):
    # Something the watchdog could not auto-fix — surfaced for a human / Opus.
    log("ALERT (needs attention): " + msg, level="ALERT")


def sh(args, timeout=25):
    """Run a command, return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def container_running(name):
    rc, out, _ = sh(["docker", "inspect", "-f", "{{.State.Running}}", name])
    return rc == 0 and out == "true"


def container_started_at(name):
    rc, out, _ = sh(["docker", "inspect", "-f", "{{.State.StartedAt}}", name])
    return out if rc == 0 else ""


def health_status(name):
    # "healthy" | "unhealthy" | "starting" | "" (no healthcheck)
    rc, out, _ = sh(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", name])
    return out if rc == 0 else ""


def probe(container, port, path, method="GET", timeout=6):
    """GET/POST an in-container HTTP endpoint via docker exec. Returns parsed
    JSON dict, or None on failure."""
    code = (
        "import json,urllib.request as u;"
        f"r=u.Request('http://127.0.0.1:{port}{path}',method='{method}');"
        f"print(u.urlopen(r,timeout={timeout}).read().decode())"
    )
    rc, out, _ = sh(["docker", "exec", container, "python3", "-c", code], timeout=timeout + 6)
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out}


def restart(name):
    log(f"restarting container {name} ...", level="fix")
    rc, _, err = sh(["docker", "restart", name], timeout=60)
    if rc == 0:
        log(f"restarted {name}")
        return True
    alert(f"failed to restart {name}: {err}")
    return False


def load_state():
    try:
        with open(STATEFILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATEFILE, "w") as f:
            json.dump(st, f)
    except Exception:
        pass


def check_market(market, cfg, st):
    panel, integ = cfg["panel"], cfg["integration"]
    ms = st.setdefault(market, {"desired": "unknown", "panel_started": ""})

    # ---- 1. Integration API (uses the container's built-in healthcheck; the
    #         image is node:alpine so we can't exec python there) ----
    if not container_running(integ):
        alert(f"{market}: integration {integ} not running -> restart")
        restart(integ)
        time.sleep(4)
    else:
        hs = health_status(integ)
        if hs == "unhealthy":
            log(f"{market}: integration {integ} is UNHEALTHY (hung) -> restart", level="fix")
            restart(integ)
            time.sleep(4)  # next cycle re-verifies once it leaves 'starting'

    # ---- 2. Panel container ----
    if not container_running(panel):
        alert(f"{market}: panel {panel} not running")
        restart(panel)
        time.sleep(4)

    started = container_started_at(panel)
    panel_restarted = bool(ms.get("panel_started")) and started != ms.get("panel_started")
    ms["panel_started"] = started

    state = probe(panel, PANEL_PORT, "/api/state", timeout=6)
    if state is None:
        alert(f"{market}: panel {panel} not answering /api/state -> restart")
        restart(panel)
        time.sleep(4)
        state = probe(panel, PANEL_PORT, "/api/state", timeout=6)
        if state is None:
            alert(f"{market}: panel {panel} still unreachable after restart")
            return

    # ---- 3. Bot running-state ----
    running = bool(state.get("running"))
    phase = state.get("phase")
    last_error = state.get("last_error")
    rnd = state.get("round")
    desired = ms.get("desired", "unknown")

    if running:
        ms["desired"] = "running"
    else:
        if panel_restarted and desired == "running":
            # Container was recycled under us -> restore the bot we were running.
            log(f"{market}: panel was restarted while bot desired=running -> starting bot", level="fix")
            probe(panel, PANEL_PORT, "/api/start", method="POST", timeout=6)
            time.sleep(2)
            after = probe(panel, PANEL_PORT, "/api/state", timeout=6) or {}
            if after.get("running"):
                log(f"{market}: bot restored to running (round {after.get('round')})")
            else:
                alert(f"{market}: tried to restart bot but it did not come up running")
        elif last_error:
            # Loop died with an error but container stayed up -> restart the loop.
            log(f"{market}: bot not running with last_error={last_error!r} -> restarting bot", level="fix")
            probe(panel, PANEL_PORT, "/api/start", method="POST", timeout=6)
            time.sleep(2)
            after = probe(panel, PANEL_PORT, "/api/state", timeout=6) or {}
            if after.get("running"):
                log(f"{market}: bot restarted OK")
            else:
                alert(f"{market}: bot restart did not take; error was {last_error!r}")
        else:
            # Clean stop (you pressed Stop) — respect it, do not auto-start.
            if desired != "stopped":
                log(f"{market}: bot is stopped (clean) — recording desired=stopped, will not auto-start")
            ms["desired"] = "stopped"

    log(f"{market}: ok (running={running} phase={phase} round={rnd} desired={ms['desired']})", level="ok")


def main():
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    log(f"watchdog started (pid {os.getpid()}, interval {INTERVAL}s) monitoring {list(TARGETS)}")
    while True:
        st = load_state()
        for market, cfg in TARGETS.items():
            try:
                check_market(market, cfg, st)
            except Exception as e:
                alert(f"{market}: watchdog cycle error: {e}")
        save_state(st)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
