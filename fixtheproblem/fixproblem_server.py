"""FixTheProblem — incident console backend.

When mm_monitor detects a problem it POSTs the incident here (internal, shared
secret). This server mints a SINGLE-USE, 30-minute magic-link. The admin opens
the link (no login): the token is burned for a short session, the PWA loads with
the problem pre-filled, live indexes for both makers, and MEXC/CoinW charts. The
admin edits the prompt and dispatches a scoped fixer agent (a job folder on the
shared /queue volume that the worker container drains). The admin polls the
agent transcript, sends follow-ups, and Exit kills the agent + ends the session.

This app holds NO exchange keys and has NO docker socket — it only writes job
folders. The worker container is the only thing that runs the agent. Stdlib only.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("FP_HOST", "0.0.0.0")
PORT = int(os.environ.get("FP_PORT", "8790"))
BASE_URL = os.environ.get("FP_BASE_URL", "https://fixtheproblem.exuno.io")
INTERNAL_TOKEN = os.environ.get("FP_INTERNAL_TOKEN", "")   # shared secret for mm_monitor
TOKEN_TTL = int(os.environ.get("FP_TOKEN_TTL", "1800"))    # 30 min link life
SESSION_TTL = int(os.environ.get("FP_SESSION_TTL", "1800"))  # 30 min session
QUEUE_DIR = Path(os.environ.get("FP_QUEUE_DIR", "/queue"))
STATE_FILE = Path(os.environ.get("FP_STATE_FILE", "/data/incidents.json"))
PWA_HTML = Path(__file__).with_name("fixproblem_pwa.html")
MANIFEST = Path(__file__).with_name("manifest.webmanifest")
SW_JS = Path(__file__).with_name("sw.js")

# maker state endpoints (docker network names) + public klines
MAKERS = {
    "market4": {"label": "MEXC", "state": os.environ.get("FP_MEXC_STATE", "http://exuno-market-panel4:8787/api/state")},
    "market5": {"label": "CoinW", "state": os.environ.get("FP_COINW_STATE", "http://exuno-market-coinw-mm:8787/api/state")},
}
MEXC_KLINES = "https://api.mexc.com/api/v3/klines?symbol=UNPUSDT&interval=1m&limit=120"
# CoinW returnChartData: needs currencyPair (NOT symbol) + start/end; returns
# newest-first with 'date' in milliseconds. period 3600 = 1h candles.
COINW_KLINES_BASE = os.environ.get("FP_COINW_KLINES", "https://api.coinw.com/api/v1/public?command=returnChartData&currencyPair=UNP_USDT")
COINW_KLINES_PERIOD = int(os.environ.get("FP_COINW_PERIOD", "3600"))

QUEUE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

_UA = "fixproblem/1.0"


# ---------- incident + session store (small JSON, single process) ----------
def _load():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"incidents": {}, "sessions": {}}


def _save(d):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(STATE_FILE)


DB = _load()


def _gc(now):
    changed = False
    for tok, inc in list(DB["incidents"].items()):
        if not inc.get("consumed") and now > inc["expires"]:
            del DB["incidents"][tok]; changed = True
    for sid, s in list(DB["sessions"].items()):
        if now > s["expires"]:
            del DB["sessions"][sid]; changed = True
    if changed:
        _save(DB)


def create_incident(problem):
    now = time.time()
    _gc(now)
    token = secrets.token_urlsafe(32)
    inc = {"token": token, "created": now, "expires": now + TOKEN_TTL,
           "consumed": False, "problem": problem, "job": None}
    DB["incidents"][token] = inc
    _save(DB)
    return {"token": token, "link": f"{BASE_URL}/t/{token}", "expires_in": TOKEN_TTL}


def open_session(token):
    """Open (or re-attach to) the session for a token. Valid for the whole
    30-min window and re-openable — robust against email link-scanners and page
    reloads. Dead only after expiry or an explicit Exit (inc['dead'])."""
    now = time.time()
    inc = DB["incidents"].get(token)
    if not inc or now > inc["expires"] or inc.get("dead"):
        return None
    sid = inc.get("session")
    if sid and session_ok(sid):
        return sid  # re-attach to the live session (reload / second tab)
    inc["consumed"] = True
    sid = secrets.token_urlsafe(24)
    DB["sessions"][sid] = {"token": token, "created": now, "expires": now + SESSION_TTL}
    inc["session"] = sid
    _save(DB)
    return sid


def session_ok(sid):
    s = DB["sessions"].get(sid or "")
    if not s or time.time() > s["expires"]:
        return None
    return s


def session_incident(sid):
    s = session_ok(sid)
    if not s:
        return None
    return DB["incidents"].get(s["token"])


# ---------- data helpers ----------
def _get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def maker_indexes():
    out = {}
    for name, m in MAKERS.items():
        item = {"label": m["label"], "ok": False, "reachable": False}
        try:
            st = _get_json(m["state"])
            c = st.get("compliance", {}) or {}
            item.update({
                "reachable": True, "running": bool(st.get("running")),
                "round": st.get("round"), "mid": st.get("mid"),
                "bids": c.get("bid_count"), "asks": c.get("ask_count"),
                "spread": c.get("spread_pct"), "depth": c.get("depth_usd_within_2pct"),
                "last_error": st.get("last_error"),
            })
            item["ok"] = bool(item["running"] and not item["last_error"]
                              and (item.get("bids") or 0) >= 30 and (item.get("asks") or 0) >= 30
                              and (item.get("depth") or 0) >= 1000)
        except Exception as e:
            item["err"] = f"{type(e).__name__}: {str(e)[:120]}"
        out[name] = item
    return out


def klines(exchange):
    try:
        if exchange == "mexc":
            rows = _get_json(MEXC_KLINES)
            # [openTime, open, high, low, close, volume, ...]
            return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                     "l": float(r[3]), "c": float(r[4])} for r in rows]
        else:
            now = int(time.time())
            url = f"{COINW_KLINES_BASE}&period={COINW_KLINES_PERIOD}&start={now - 120 * COINW_KLINES_PERIOD}&end={now}"
            d = _get_json(url)
            data = d.get("data") if isinstance(d, dict) else d
            out = []
            for r in (data or []):
                if isinstance(r, dict) and r.get("date") is not None:
                    out.append({"t": int(r["date"]), "o": float(r.get("open", 0)),  # date already in ms
                                "h": float(r.get("high", 0)), "l": float(r.get("low", 0)),
                                "c": float(r.get("close", 0))})
            out.sort(key=lambda x: x["t"])  # CoinW returns newest-first
            return out[-120:]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


# ---------- job queue (dispatch to worker) ----------
def dispatch_job(sid, prompt):
    inc = session_incident(sid)
    if inc is None:
        return None
    job_id = "job_" + secrets.token_hex(8)
    d = QUEUE_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(prompt)
    (d / "job.json").write_text(json.dumps({
        "job": job_id, "sid": sid, "token": inc["token"],
        "created": time.time(), "scope": "fixer"}))
    (d / "status").write_text("queued")
    (d / "transcript.jsonl").write_text("")
    inc["job"] = job_id
    _save(DB)
    return job_id


def _job_dir(sid, job_id):
    inc = session_incident(sid)
    if inc is None or inc.get("job") != job_id:
        return None
    d = QUEUE_DIR / job_id
    return d if d.exists() else None


def job_state(sid, job_id):
    d = _job_dir(sid, job_id)
    if d is None:
        return None
    status = (d / "status").read_text().strip() if (d / "status").exists() else "unknown"
    transcript = []
    tf = d / "transcript.jsonl"
    if tf.exists():
        for line in tf.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    transcript.append(json.loads(line))
                except Exception:
                    transcript.append({"role": "agent", "text": line})
    return {"status": status, "transcript": transcript}


def add_message(sid, job_id, text):
    d = _job_dir(sid, job_id)
    if d is None:
        return False
    # append a follow-up the worker will pick up (resume the agent session)
    with open(d / "followups.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")
    with open(d / "transcript.jsonl", "a") as f:
        f.write(json.dumps({"role": "admin", "text": text, "ts": time.time()}) + "\n")
    if (d / "status").exists() and (d / "status").read_text().strip() in ("done", "failed"):
        (d / "status").write_text("queued")  # re-open for the follow-up
    return True


def kill_session(sid):
    inc = session_incident(sid)
    if inc:
        inc["dead"] = True  # Exit kills the link for good
        if inc.get("job"):
            d = QUEUE_DIR / inc["job"]
            if d.exists():
                (d / ".kill").write_text(str(time.time()))
    # end the session
    s = DB["sessions"].pop(sid, None)
    _save(DB)
    return s is not None


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _sid(self):
        # session id from cookie or X-Session header
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            part = part.strip()
            if part.startswith("fp_sid="):
                return part[7:]
        return self.headers.get("X-Session", "")

    def _need_session(self):
        sid = self._sid()
        if session_ok(sid):
            return sid
        self._send(401, {"error": "session expired — the one-time link is used up"})
        return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/manifest.webmanifest" and MANIFEST.exists():
            return self._send(200, MANIFEST.read_bytes(), "application/manifest+json")
        if path == "/sw.js" and SW_JS.exists():
            return self._send(200, SW_JS.read_bytes(), "application/javascript")
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if path.startswith("/t/"):
            token = path[3:]
            inc = DB["incidents"].get(token)
            now = time.time()
            if not inc or now > inc["expires"] or inc.get("dead"):
                return self._send(410, "<h2>This link is invalid, expired, or the session was ended.</h2>", "text/html")
            html = PWA_HTML.read_text().replace("__TOKEN__", token)
            return self._send(200, html, "text/html; charset=utf-8")
        if path in ("/", "/index.html"):
            return self._send(200, "<h3>FixTheProblem — open the one-time link from your alert email.</h3>", "text/html")

        # ---- authed API ----
        if path == "/api/indexes":
            if not self._need_session():
                return
            return self._send(200, {"indexes": maker_indexes()})
        if path == "/api/klines":
            if not self._need_session():
                return
            ex = "mexc" if "mexc" in self.path else "coinw"
            return self._send(200, {"exchange": ex, "candles": klines(ex)})
        if path == "/api/context":
            sid = self._need_session()
            if not sid:
                return
            inc = session_incident(sid)
            return self._send(200, {"problem": (inc or {}).get("problem", {}), "job": (inc or {}).get("job")})
        if path == "/api/job":
            sid = self._need_session()
            if not sid:
                return
            job = self.path.split("job=")[-1].split("&")[0] if "job=" in self.path else ""
            st = job_state(sid, job)
            return self._send(200, st or {"error": "no such job"}, )
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        # internal: mm_monitor mints an incident link
        if path == "/internal/incident":
            auth = self.headers.get("Authorization", "")
            if not INTERNAL_TOKEN or not hmac.compare_digest(auth, f"Bearer {INTERNAL_TOKEN}"):
                return self._send(403, {"error": "forbidden"})
            data = self._body()
            return self._send(200, create_incident(data.get("problem", {})))

        # exchange a single-use token for a session
        if path == "/api/session":
            token = self._body().get("token", "")
            sid = open_session(token)
            if not sid:
                return self._send(410, {"error": "link expired or session ended"})
            return self._send(200, {"ok": True, "sid": sid},
                              extra=[("Set-Cookie", f"fp_sid={sid}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={SESSION_TTL}")])

        sid = self._need_session()
        if not sid:
            return
        if path == "/api/dispatch":
            prompt = (self._body().get("prompt") or "").strip()
            if not prompt:
                return self._send(400, {"error": "empty prompt"})
            job = dispatch_job(sid, prompt)
            return self._send(200, {"ok": bool(job), "job": job})
        if path == "/api/message":
            b = self._body()
            ok = add_message(sid, b.get("job", ""), (b.get("text") or "").strip())
            return self._send(200, {"ok": ok})
        if path == "/api/exit":
            kill_session(sid)
            return self._send(200, {"ok": True}, extra=[("Set-Cookie", "fp_sid=; Path=/; Max-Age=0")])
        return self._send(404, {"error": "not found"})


def main():
    print(f"FixTheProblem backend on {HOST}:{PORT} (base {BASE_URL})", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
