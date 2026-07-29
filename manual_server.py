"""Manual trading terminal for the two CoinW accounts (market3).

No bot / no loop — this is a hands-on panel. It proxies the token-protected
CoinW API so the browser can, per account: see balances, place buy/sell limit
orders, list open orders, cancel one order, or cancel all.

Run:  python3 manual_server.py   then open  http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import orders as api

HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "8787"))
PANEL_HTML = Path(__file__).with_name("manual_panel.html")
EXCHANGE = os.environ.get("EXCHANGE_NAME", "CoinW")
# Public ticker for reference price (CoinW only; optional).
TICKER_URL = os.environ.get(
    "TICKER_URL", "https://api.coinw.com/api/v1/public?command=returnTicker"
)
TICKER_SYMBOL = os.environ.get("TICKER_SYMBOL", "UNP_USDT")


def _market_price() -> dict:
    """Best-effort live reference price from the exchange's public ticker."""
    try:
        req = urllib.request.Request(TICKER_URL, headers={"User-Agent": api.USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
        t = (d.get("data") or {}).get(TICKER_SYMBOL, {})
        return {
            "last": t.get("last"), "bid": t.get("highestBid"),
            "ask": t.get("lowestAsk"), "high": t.get("high24hr"), "low": t.get("low24hr"),
        }
    except Exception as exc:
        return {"error": str(exc)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
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
            self._json({
                "exchange": EXCHANGE,
                "balances": api.get_balances(),
                "orders": api.get_orders(),
                "market": _market_price(),
            })
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        data = self._body()
        if self.path == "/api/send":
            try:
                account = int(data["account"])
                price = str(data["price"]).strip()
                quantity = str(data["quantity"]).strip()
                expire = int(data.get("expire", 60))
                side = str(data["side"]).upper()
            except Exception as exc:
                self._json({"ok": False, "error": f"bad input: {exc}"}, 400)
                return
            api.EXPIRE_SECONDS = expire
            if side == "BUY":
                resp = api.send_buy(account, price, quantity)
            elif side == "SELL":
                resp = api.send_sell(account, price, quantity)
            else:
                self._json({"ok": False, "error": "side must be BUY or SELL"}, 400)
                return
            self._json(resp)
            return
        if self.path == "/api/cancel":
            oid = str(data.get("id", "")).strip()
            if not oid:
                self._json({"ok": False, "error": "missing id"}, 400)
                return
            self._json(api.cancel_order(oid))
            return
        if self.path == "/api/cancelall":
            try:
                account = int(data["account"])
            except Exception:
                self._json({"ok": False, "error": "missing account"}, 400)
                return
            self._json(api.cancel_all(account))
            return
        self._json({"error": "not found"}, 404)


def main():
    print(f"Manual panel ({EXCHANGE}) at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
