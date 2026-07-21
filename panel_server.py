"""Control-panel backend for the Exuno UNPUSDT training demo.

Stdlib-only HTTP server that:
  * serves the graphical panel (panel.html),
  * proxies the token-protected API (avoids CORS + Cloudflare UA blocking),
  * runs the continuous buy/sell loop in a background thread you start/stop,
  * keeps live balances + a running log of every order it sends.

Run:  python3 panel_server.py     then open  http://127.0.0.1:8787

Trading rules per round (values sorted low -> high):
  * first `same_count` items : BUY price = value,            SELL price = value
  * remaining items          : BUY price = value - discount, SELL price = value
Buys go to the buyer account, sells to the seller account. Each order expires
after `expire` seconds. A round sends `count` pairs spaced `window/count`
seconds apart, then immediately starts the next round until you press Stop.
"""

from __future__ import annotations

import json
import os
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import orders as api
from algorithm_a import algorithm_a

# Bind localhost by default (dev); the container sets HOST=0.0.0.0 so the
# reverse proxy on the docker network can reach it. The port is never published
# to the host — only nginx-proxy-manager (same network) talks to it.
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "8787"))
PANEL_HTML = Path(__file__).with_name("panel.html")

# ---------------------------------------------------------------------------#
# Shared state                                                               #
# ---------------------------------------------------------------------------#

_lock = threading.Lock()

STATE = {
    "running": False,
    "config": {
        "low": "0.10000",
        "high": "0.11000",
        "count": 10,
        "quantity": "20",
        "discount": "0.001",
        "same_count": 3,
        "expire": 60,
        "window": 60,          # seconds per round; interval = window / count
        "align_candle": True,  # wait for the next 1M candle open before a round
        "mode": "auto",        # "auto" (roles from balances) or "manual"
        "buyer": 2,
        "seller": 1,
    },
    "roles": {"buyer": 2, "seller": 1},
    "balances": None,
    "orders": [],              # our sent log, newest last
    "round": 0,
    "phase": "idle",           # idle | waiting | running | stopping
    "candle_wait_until": None, # epoch of the next candle open we're waiting for
    "last_error": None,
}

CANDLE_SECONDS = 60            # 1M candle period (UTC-aligned minute boundary)

_worker: threading.Thread | None = None
_order_seq = 0


def _snapshot() -> dict:
    with _lock:
        return json.loads(json.dumps(STATE, default=str))


# ---------------------------------------------------------------------------#
# Order sending loop                                                         #
# ---------------------------------------------------------------------------#

def _record(side, account, price, quantity, resp):
    global _order_seq
    _order_seq += 1
    rec = {
        "seq": _order_seq,
        "ts": time.time(),
        "round": STATE["round"],
        "side": side,
        "account": account,
        "price": price,
        "quantity": str(quantity),
        "ok": bool(resp.get("ok")),
        "id": resp.get("id"),
        "orderId": resp.get("orderId"),
        "status": "open" if resp.get("ok") else "rejected",
        "error": None if resp.get("ok") else (resp.get("error") or resp.get("status")),
    }
    with _lock:
        STATE["orders"].append(rec)
        # keep the log bounded
        if len(STATE["orders"]) > 500:
            STATE["orders"] = STATE["orders"][-500:]


def _interruptible_sleep(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        with _lock:
            if not STATE["running"]:
                return
        time.sleep(0.15)


def _wait_for_candle_open() -> bool:
    """Block until the next 1M candle opens (next UTC minute boundary).

    Candle opens land where epoch % 60 == 0 (second-of-minute is the same in
    every timezone), so we sleep until then. Returns False if Stop was pressed
    during the wait, True once the candle opens.
    """
    now = time.time()
    target = now + (CANDLE_SECONDS - (now % CANDLE_SECONDS))
    with _lock:
        STATE["phase"] = "waiting"
        STATE["candle_wait_until"] = target
    while time.time() < target:
        with _lock:
            if not STATE["running"]:
                STATE["candle_wait_until"] = None
                return False
        time.sleep(0.1)
    with _lock:
        STATE["candle_wait_until"] = None
    return True


def _refresh_balances_and_roles():
    bal = api.get_balances()
    with _lock:
        if bal.get("ok") is not False:
            STATE["balances"] = bal
        mode = STATE["config"]["mode"]
    if bal.get("ok") is not False and mode == "auto":
        roles = api.decide_roles(bal)
        with _lock:
            STATE["roles"] = {"buyer": roles["buyer"], "seller": roles["seller"]}


def _merge_live_statuses():
    live = api.get_orders()
    if live.get("ok") is False:
        return
    by_id = {o.get("id"): o for o in live.get("orders", [])}
    with _lock:
        for rec in STATE["orders"]:
            live_o = by_id.get(rec["id"])
            if live_o:
                rec["status"] = live_o.get("status", rec["status"])


def _worker_loop():
    while True:
        with _lock:
            if not STATE["running"]:
                STATE["phase"] = "idle"
                STATE["candle_wait_until"] = None
                return
            cfg = dict(STATE["config"])

        # Align each round to the real 1M candle open, if enabled.
        if cfg.get("align_candle", True):
            if not _wait_for_candle_open():
                continue  # Stop pressed during the wait -> loop re-checks & exits

        with _lock:
            if not STATE["running"]:
                continue
            STATE["round"] += 1
            STATE["phase"] = "running"

        _refresh_balances_and_roles()
        with _lock:
            buyer = STATE["roles"]["buyer"] if cfg["mode"] == "auto" else cfg["buyer"]
            seller = STATE["roles"]["seller"] if cfg["mode"] == "auto" else cfg["seller"]

        count = int(cfg["count"])
        discount = Decimal(str(cfg["discount"]))
        same_count = int(cfg["same_count"])
        qty = cfg["quantity"]
        interval = float(cfg["window"]) / max(count, 1)

        values = algorithm_a(cfg["low"], cfg["high"], count)

        for i, value in enumerate(values):
            with _lock:
                if not STATE["running"]:
                    break
                api.EXPIRE_SECONDS = int(cfg["expire"])
            buy_price = value if i < same_count else value - discount
            sell_price = value

            bp = api._fmt(buy_price, api._PRICE_Q)
            sp = api._fmt(sell_price, api._PRICE_Q)

            buy_resp = api.send_buy(buyer, bp, qty)
            _record("BUY", buyer, bp, qty, buy_resp)
            sell_resp = api.send_sell(seller, sp, qty)
            _record("SELL", seller, sp, qty, sell_resp)

            _merge_live_statuses()
            _refresh_balances_and_roles()

            if i < count - 1:
                _interruptible_sleep(interval)

        _merge_live_statuses()


# ---------------------------------------------------------------------------#
# Background poller (keeps balances/orders live even while stopped)          #
# ---------------------------------------------------------------------------#

def _poller_loop():
    while True:
        try:
            _refresh_balances_and_roles()
            _merge_live_statuses()
        except Exception as exc:  # never let the poller die
            with _lock:
                STATE["last_error"] = str(exc)
        time.sleep(3)


# ---------------------------------------------------------------------------#
# HTTP handler                                                               #
# ---------------------------------------------------------------------------#

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default logging
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if self.path == "/api/state":
            self._send_json(_snapshot())
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/config":
            data = self._read_json()
            with _lock:
                for k in ("low", "high", "quantity", "discount", "mode"):
                    if k in data:
                        STATE["config"][k] = str(data[k])
                for k in ("count", "same_count", "expire", "window", "buyer", "seller"):
                    if k in data:
                        STATE["config"][k] = int(data[k])
                if "align_candle" in data:
                    STATE["config"]["align_candle"] = bool(data["align_candle"])
                if STATE["config"]["mode"] == "manual":
                    STATE["roles"] = {
                        "buyer": STATE["config"]["buyer"],
                        "seller": STATE["config"]["seller"],
                    }
            self._send_json({"ok": True, "config": _snapshot()["config"]})
            return

        if self.path == "/api/start":
            self._start()
            self._send_json({"ok": True, "running": True})
            return

        if self.path == "/api/stop":
            with _lock:
                STATE["running"] = False
                STATE["phase"] = "stopping"
            self._send_json({"ok": True, "running": False})
            return

        self._send_json({"error": "not found"}, 404)

    def _start(self):
        global _worker
        with _lock:
            if STATE["running"]:
                return
            STATE["running"] = True
            STATE["last_error"] = None
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, daemon=True)
            _worker.start()


def main():
    threading.Thread(target=_poller_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Panel running at  http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
