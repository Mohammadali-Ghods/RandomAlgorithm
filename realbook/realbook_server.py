"""Real Order Book — shows ONLY external traders' orders on MEXC UNP/USDT.

It pulls the full public MEXC depth and subtracts OUR own open orders (market4 +
market1, accounts 1&2) at each price level, so what remains is genuine
third-party buy/sell interest. Read-only. Stdlib only.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import orders as api

HOST = os.environ.get("RB_HOST", "0.0.0.0")
PORT = int(os.environ.get("RB_PORT", "8792"))
DEPTH_URL = os.environ.get("RB_DEPTH_URL", "https://api.mexc.com/api/v3/depth?symbol=UNPUSDT&limit=5000")
PANEL = Path(__file__).with_name("realbook_panel.html")
UA = "realbook/1.0"
DUST = float(os.environ.get("RB_DUST_USD", "0.50"))   # ignore external remainders under this $


def _our_open_qty():
    """Map {('BUY'|'SELL', price5): our_open_qty} across accounts 1&2."""
    out = defaultdict(float)
    try:
        o = api.get_orders()
        rows = o if isinstance(o, list) else o.get("orders", o.get("data", []))
    except Exception:
        rows = []
    for r in rows or []:
        if str(r.get("status", "")).lower() not in ("open", "new", "partially_filled"):
            continue
        side = "BUY" if "BUY" in str(r.get("side", "")).upper() else "SELL"
        try:
            p = round(float(r.get("price")), 5)
            q = float(r.get("remainingQuantity") or r.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        out[(side, p)] += q
    return out


def real_book():
    d = json.loads(urllib.request.urlopen(
        urllib.request.Request(DEPTH_URL, headers={"User-Agent": UA}), timeout=10).read().decode())
    bids = sorted(((float(p), float(q)) for p, q in d.get("bids", [])), reverse=True)
    asks = sorted((float(p), float(q)) for p, q in d.get("asks", []))
    if not bids or not asks:
        return {"error": "empty book"}
    mid = (bids[0][0] + asks[0][0]) / 2
    ours = _our_open_qty()

    def externalize(levels, side):
        out = []
        for p, tot in levels:
            ext = tot - ours.get((side, round(p, 5)), 0.0)
            if ext * p < DUST:
                continue
            out.append({"price": round(p, 5), "qty": round(ext, 2), "usd": round(ext * p, 2),
                        "pct": round((p / mid - 1) * 100, 2)})
        return out

    rbids = externalize(bids, "BUY")
    rasks = externalize(asks, "SELL")
    within = lambda rows: round(sum(r["usd"] for r in rows if abs(r["pct"]) <= 2), 2)
    return {
        "mid": round(mid, 5), "best_bid": bids[0][0], "best_ask": asks[0][0],
        "bids": rbids, "asks": rasks,
        "buy_total": round(sum(r["usd"] for r in rbids), 2),
        "sell_total": round(sum(r["usd"] for r in rasks), 2),
        "buy_2pct": within(rbids), "sell_2pct": within(rasks),
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, PANEL.read_text(), "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, {"ok": True}, "application/json")
        if path == "/api/realbook":
            try:
                return self._send(200, real_book(), "application/json")
            except Exception as e:
                return self._send(200, {"error": f"{type(e).__name__}: {str(e)[:160]}"}, "application/json")
        return self._send(404, {"error": "not found"}, "application/json")


def main():
    print(f"Real Order Book on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
