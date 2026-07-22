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
import secrets
import threading
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
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

# Exchange-specific bits are env-driven so one codebase serves both the MEXC
# panel and its CoinW twin without any code change.
EXCHANGE_NAME = os.environ.get("EXCHANGE_NAME", "MEXC")
MIN_ORDER_USDT_ENV = os.environ.get("MIN_ORDER_USDT", "1.05")  # min order value + buffer

STATE = {
    "running": False,
    "config": {
        "low": "0.10000",
        "high": "0.11000",
        "count": 10,
        "quantity": os.environ.get("PANEL_QUANTITY", "20"),
        "auto_quantity": False,  # random qty per order, scaled to the round budget
        # Per-round spend caps in USDT (0 = use live balances instead). For CoinW
        # the sub-accounts read 0 and draw on a shared parent, so a cap is needed.
        "budget_usdt": os.environ.get("BUDGET_USDT", "0"),
        "budget_unp_usdt": os.environ.get("BUDGET_UNP_USDT", "0"),
        "discount": "0.005",
        "same_count": 3,
        "expire": 60,
        "window": 60,          # seconds per round; interval = window / count
        "align_candle": True,  # wait for the next 1M candle open before a round
        "mode": "auto",        # "auto" (roles from balances) or "manual"
        "buyer": 2,
        "seller": 1,
        "exchange": EXCHANGE_NAME,
        "min_order_usdt": MIN_ORDER_USDT_ENV,
    },
    "roles": {"buyer": 2, "seller": 1},
    "balances": None,
    "orders": [],              # our sent log, newest last
    "round": 0,
    "phase": "idle",           # idle | waiting | running | stopping
    "candle_wait_until": None, # epoch of the next candle open we're waiting for
    "round_started_at": None,  # epoch when the current round began sending
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


MIN_ORDER_USDT = Decimal(MIN_ORDER_USDT_ENV)  # keep price*qty above the exchange min
_Q01 = Decimal("0.01")             # quantity has up to 2 decimals


_BUDGET_SAFETY = Decimal("0.90")   # leave headroom for fees / partial locks


def _budget_units(cfg, balances, buyer=None, seller=None) -> Decimal:
    """UNP-quantity a single round may move — the smaller of the buy side and
    the sell side.

    Buy side  = the BUYER account's free USDT (the buyer spends USDT).
    Sell side = the SELLER account's free UNP (the seller spends UNP).
    Each side is capped by its manual budget (budget_usdt / budget_unp_usdt, in
    USDT) when set, but NEVER exceeds the account's real free balance, so a round
    can't be sized past what the account actually holds. A safety factor leaves
    headroom. Returns 0 if the mid price is unavailable.
    """
    accounts = (balances or {}).get("accounts", {})
    b1, b2 = accounts.get("1", {}), accounts.get("2", {})
    # Which account funds each side. Fall back to the union if roles unknown.
    buyer_usdt = api._free({1: b1, 2: b2}.get(buyer, {}), "USDT") if buyer else \
        api._free(b1, "USDT") + api._free(b2, "USDT")
    seller_unp = api._free({1: b1, 2: b2}.get(seller, {}), "UNP") if seller else \
        api._free(b1, "UNP") + api._free(b2, "UNP")
    try:
        low, high = Decimal(str(cfg["low"])), Decimal(str(cfg["high"]))
        b_usdt = Decimal(str(cfg.get("budget_usdt", "0") or "0"))
        b_unp_usdt = Decimal(str(cfg.get("budget_unp_usdt", "0") or "0"))
    except Exception:
        return Decimal(0)
    ref = (low + high) / 2
    if ref <= 0:
        return Decimal(0)
    # cap by budget if set, then always by the real free balance
    eff_usdt = min(b_usdt, buyer_usdt) if b_usdt > 0 else buyer_usdt
    eff_unp = min(b_unp_usdt / ref, seller_unp) if b_unp_usdt > 0 else seller_unp
    return min(eff_usdt / ref, eff_unp) * _BUDGET_SAFETY


def _cap_per_order(cfg, balances, count, buyer=None, seller=None) -> Decimal:
    """Max quantity for one order so a whole round stays within the budget.
    Returns 0 when no cap applies (no budget set and balances unknown)."""
    units = _budget_units(cfg, balances, buyer, seller)
    return (units / Decimal(max(count, 1))).quantize(_Q01, rounding=ROUND_DOWN)


def _auto_quantity(cfg, balances, count, low_price, buyer=None, seller=None) -> Decimal:
    """A CSPRNG-random quantity for one order, scaled to the round's budget.

    Upper bound: budget / count (see _budget_units). Lower bound: enough that
    price*qty clears the exchange minimum order value. Falls back to the manual
    quantity if the budget/mid price can't be determined.
    """
    try:
        lp = Decimal(str(low_price))
    except Exception:
        return Decimal(str(cfg["quantity"]))
    if lp <= 0:
        return Decimal(str(cfg["quantity"]))
    max_q = _cap_per_order(cfg, balances, count, buyer, seller)
    min_q = (MIN_ORDER_USDT / lp).quantize(_Q01, rounding=ROUND_UP)
    if max_q <= min_q:
        return min_q
    steps = int((max_q - min_q) / _Q01)
    return min_q + Decimal(secrets.randbelow(steps + 1)) * _Q01


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
            STATE["round_started_at"] = time.time()

        _refresh_balances_and_roles()
        with _lock:
            buyer = STATE["roles"]["buyer"] if cfg["mode"] == "auto" else cfg["buyer"]
            seller = STATE["roles"]["seller"] if cfg["mode"] == "auto" else cfg["seller"]

        count = int(cfg["count"])
        discount = Decimal(str(cfg["discount"]))
        same_count = int(cfg["same_count"])
        interval = float(cfg["window"]) / max(count, 1)

        # Manual quantity: clamp down so a round never exceeds the budget cap
        # (when one is set). Auto quantity handles this itself, per order.
        qty = cfg["quantity"]
        if not cfg.get("auto_quantity"):
            cap = _cap_per_order(cfg, STATE["balances"], count, buyer, seller)
            if cap > 0:
                manual = Decimal(str(cfg["quantity"]))
                qty = api._fmt(min(manual, cap), api._QTY_Q)

        values = algorithm_a(cfg["low"], cfg["high"], count)

        for i, value in enumerate(values):
            with _lock:
                if not STATE["running"]:
                    break
                api.EXPIRE_SECONDS = int(cfg["expire"])
            if i < same_count:
                # First N: buy and sell at the same price -> the two accounts
                # cross each other and the pair FILLS (creates the trade/candle).
                buy_price = value
                sell_price = value
            else:
                # After N: bid below and ask above -> the pair RESTS in the book
                # and does not fill (order-book depth only).
                buy_price = value - discount
                sell_price = value + discount

            # Random quantity (scaled to total assets) when enabled; the pair
            # shares one qty so the first-N orders cross fully.
            if cfg.get("auto_quantity"):
                low_price = min(buy_price, sell_price)
                qty = api._fmt(
                    _auto_quantity(cfg, STATE["balances"], count, low_price, buyer, seller),
                    api._QTY_Q,
                )

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
                for k in ("low", "high", "quantity", "discount", "mode",
                          "budget_usdt", "budget_unp_usdt"):
                    if k in data:
                        STATE["config"][k] = str(data[k])
                for k in ("count", "same_count", "expire", "window", "buyer", "seller"):
                    if k in data:
                        STATE["config"][k] = int(data[k])
                for k in ("align_candle", "auto_quantity"):
                    if k in data:
                        STATE["config"][k] = bool(data[k])
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
