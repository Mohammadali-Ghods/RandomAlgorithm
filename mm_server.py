"""Persistent re-centering market maker for UNPUSDT (MEXC).

Purpose: keep the MEXC order book continuously compliant with the exchange's
liquidity standards, at all times:

  * >= 30 live orders on EACH side (bids and asks)      -> LEVELS (default 32)
  * bid/ask spread well under 2%                        -> INNER_PCT (default 0.3% -> ~0.6% spread)
  * order depth within +-2% of price exceeding limits   -> orders span INNER..BAND (<=1.8%), sized from balance
  * buy-side always populated (never one-sided)         -> re-centers on the live price every loop

Unlike a grid, this anchors to the *live market price* each loop and rebuilds
the ladder around it, so it never walks off to one side. It uses BOTH accounts
(round-robin per price level) to pool capital across bids and asks.

It talks to the hardened Exuno integration (orders.py) for all order actions,
and reads the live mid-price from MEXC's public API. Stdlib only.

This maintains liquidity; it is not trading advice.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import orders as api

# ----------------------------------------------------------------------------- config
def _f(env, default):
    try:
        return float(os.environ.get(env, default))
    except Exception:
        return float(default)

def _i(env, default):
    try:
        return int(float(os.environ.get(env, default)))
    except Exception:
        return int(default)

CONFIG = {
    "levels": _i("MM_LEVELS", 32),            # CURRENT orders/side — set dynamically by the breathing cycle
    "min_levels": _i("MM_MIN_LEVELS", 30),    # never below 30 (MEXC floor) — floor of the breathing cycle
    "max_levels": _i("MM_MAX_LEVELS", 40),    # peak of the breathing cycle (kept modest so prices don't collide)
    "breathe": _i("MM_BREATHE", 1),           # 1 = ramp order count up 10 min then down 20 min, repeating
    "cycle_up_min": _i("MM_CYCLE_UP_MIN", 10),
    "cycle_down_min": _i("MM_CYCLE_DOWN_MIN", 20),
    "inner_pct": _f("MM_INNER_PCT", 0.004),   # innermost bid/ask offset from mid (0.4%) -> ~0.8% spread
    "band_pct": _f("MM_BAND_PCT", 0.010),     # outermost offset (1.0%; well inside +-2%)
    "target_side_usd": _f("MM_TARGET_SIDE_USD", 600.0),  # max $ to deploy per side (capped by balance)
    "reserve_usdt": _f("MM_RESERVE_USDT", 0.0),   # per-account USDT to keep FREE, never used for MM
    "reserve_unp": _f("MM_RESERVE_UNP", 0.0),     # per-account UNP to keep FREE, never used for MM
    # (1) inventory skew: shift the ladder centre a touch to lean toward rebalancing
    #     our real holdings. Bounded + driven by real inventory (legal risk mgmt).
    "skew_enabled": _i("MM_SKEW", 1),
    "max_skew": _f("MM_MAX_SKEW", 0.002),         # max centre shift (0.2%) — keeps orders inside +-2%
    # (2) volatility-based spread: widen the inner quote in fast markets, tighten when calm.
    "vol_spread_enabled": _i("MM_VOL_SPREAD", 1),
    "inner_min": _f("MM_INNER_MIN", 0.002),       # tightest inner offset (calm) -> ~0.4% spread
    "inner_max": _f("MM_INNER_MAX", 0.005),       # widest inner offset (volatile) -> ~1.0% spread
    "vol_calm": _f("MM_VOL_CALM", 0.003),         # recent range/price <= this -> tightest
    "vol_wild": _f("MM_VOL_WILD", 0.02),          # >= this -> widest
    "vol_window": _i("MM_VOL_WINDOW", 20),        # loops of mid-price history for the vol estimate
    # HARD spread ceiling: the quoted spread can never be set above this, and a guard
    # snaps the inner quotes back if a fill ever widens the visible spread past it.
    "max_spread_pct": _f("MM_MAX_SPREAD_PCT", 0.015),
    "refresh_sec": _f("MM_REFRESH_SEC", 12.0),
    "reprice_pct": _f("MM_REPRICE_PCT", 0.004),  # re-center when mid moves more than this from anchor
    "expire": _i("MM_EXPIRE", 1800),          # safety expiry; loop refreshes long before
    "min_order_usdt": _f("MIN_ORDER_USDT", 1.1),
    "price_min": _f("PRICE_MIN", 0.03),       # sanity guard on the anchor price
    "price_max": _f("PRICE_MAX", 0.5),
    "max_place_per_loop": _i("MM_MAX_PLACE", 70),
    "accounts": [1, 2],
}

MEXC_SYMBOL = os.environ.get("MM_MEXC_SYMBOL", "UNPUSDT")
MEXC_BOOK_URL = f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={MEXC_SYMBOL}"
MEXC_PRICE_URL = f"https://api.mexc.com/api/v3/ticker/price?symbol={MEXC_SYMBOL}"

# Which exchange this maker instance drives its reference PRICE from (order actions
# always go through EXUNO_BASE_URL/EXUNO_TOKEN — set those to the right integration).
# Default "mexc" keeps market4 behaviour identical; "coinw" uses CoinW's public ticker.
MM_EXCHANGE = os.environ.get("MM_EXCHANGE", "mexc").lower()
COINW_TICKER_URL = os.environ.get(
    "MM_COINW_TICKER_URL", "https://api.coinw.com/api/v1/public?command=returnTicker")
COINW_TICKER_SYMBOL = os.environ.get("MM_COINW_SYMBOL", "UNP_USDT")

_PQ = api._PRICE_Q  # Decimal('0.00001')
_QQ = api._QTY_Q    # Decimal('0.01')
_TERMINAL = {"filled", "cancelled", "canceled", "rejected", "expired", "closed", "done"}

STATE = {
    "running": False,
    "round": 0,
    "anchor_mid": None,
    "mid": None,
    "last_error": None,
    "started_at": None,
    "last_loop_at": None,
    "compliance": {},     # bid_count/ask_count/spread_pct/bid_depth/ask_depth/ok
    "actions": {"placed": 0, "cancelled": 0},
    "balances": {},
    "note": "",
    "cycle_start": None,  # anchor time for the breathing cycle
    "levels_now": None,   # current per-side target from the breathing cycle
    "mid_window": [],     # recent mid prices for the volatility estimate
    "active_inner": None, # inner offset committed at the last re-centre (vol-based)
    "active_skew": 0.0,   # inventory skew committed at the last re-centre
    "vol": None,          # last volatility estimate (recent range / price)
    "spread_guard_trips": 0,  # times the hard spread ceiling snapped the inner quotes back
}
_LOCK = threading.Lock()


# ----------------------------------------------------------------------------- price
def _guard_price(mid):
    """Clamp to the sane price band; None if out of band/unusable."""
    if mid is None or mid <= 0:
        return None
    lo, hi = Decimal(str(CONFIG["price_min"])), Decimal(str(CONFIG["price_max"]))
    return None if (mid < lo or mid > hi) else mid


def _fetch_mid_mexc():
    """Live mid from MEXC public API. Prefer last trade price; fall back to
    (bestbid+bestask)/2. Returns Decimal or None if unusable."""
    last = book_mid = None
    try:
        req = urllib.request.Request(MEXC_PRICE_URL, headers={"User-Agent": api.USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            last = Decimal(str(json.loads(r.read().decode())["price"]))
    except Exception:
        last = None
    try:
        req = urllib.request.Request(MEXC_BOOK_URL, headers={"User-Agent": api.USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
            bid = Decimal(str(d["bidPrice"])); ask = Decimal(str(d["askPrice"]))
            if bid > 0 and ask > bid and (ask - bid) / bid < Decimal("0.15"):
                book_mid = (bid + ask) / 2
    except Exception:
        book_mid = None
    return _guard_price(last if (last and last > 0) else book_mid)


def _fetch_mid_coinw():
    """Live mid from CoinW's public returnTicker. CoinW's best bid/ask are often
    very wide/thin, so we anchor to LAST trade price (fall back to bid/ask mid)."""
    try:
        req = urllib.request.Request(COINW_TICKER_URL, headers={"User-Agent": api.USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            t = (json.loads(r.read().decode()).get("data") or {}).get(COINW_TICKER_SYMBOL, {})
    except Exception:
        return None
    mid = None
    try:
        if t.get("last") is not None:
            mid = Decimal(str(t["last"]))
        elif t.get("highestBid") and t.get("lowestAsk"):
            bid = Decimal(str(t["highestBid"])); ask = Decimal(str(t["lowestAsk"]))
            if 0 < bid < ask:
                mid = (bid + ask) / 2
    except Exception:
        mid = None
    return _guard_price(mid)


def fetch_mid():
    """Reference mid-price for the ladder, from the configured exchange."""
    return _fetch_mid_coinw() if MM_EXCHANGE == "coinw" else _fetch_mid_mexc()


# ----------------------------------------------------------------------------- orders
def send_limit(side, account, price, qty, expire=None):
    path = "/SendBuy" if side == "buy" else "/SendSell"
    return api._post(path, {
        "account": account,
        "price": api._fmt(Decimal(str(price)), _PQ),
        "quantity": api._fmt(Decimal(str(qty)), _QQ),
        "expire": int(expire if expire is not None else CONFIG["expire"]),
    })


def current_levels():
    """Per-side order count for right now, following the breathing cycle:
    ramp up over cycle_up_min minutes, then down over cycle_down_min, repeating.
    Never below min_levels (the MEXC 30/side floor)."""
    lo, hi = CONFIG["min_levels"], CONFIG["max_levels"]
    if not CONFIG.get("breathe") or hi <= lo:
        return lo
    up = max(1, int(CONFIG["cycle_up_min"]))
    down = max(1, int(CONFIG["cycle_down_min"]))
    if STATE.get("cycle_start") is None:
        STATE["cycle_start"] = time.time()
    minute = int((time.time() - STATE["cycle_start"]) / 60) % (up + down)
    if minute < up:
        n = lo + (hi - lo) * (minute / up)          # ramp up
    else:
        n = hi - (hi - lo) * ((minute - up) / down)  # ramp down
    return max(lo, int(round(n)))


def compute_skew(bals, mid):
    """(1) Inventory skew: shift the ladder centre toward rebalancing our real
    holdings. If we hold more UNP than USDT (by value) we lean the book DOWN a
    touch (sell UNP more readily); if we hold more USDT we lean UP. Bounded by
    max_skew so the book stays two-sided and every order stays inside +-2%.
    This reacts to REAL inventory — it does not impose a price path."""
    if not CONFIG.get("skew_enabled"):
        return Decimal(0)

    def amt(acct, asset, field):
        a = ((bals.get("accounts") or {}).get(str(acct)) or {}).get("assets", {}) or {}
        try:
            return Decimal(str((a.get(asset, {}) or {}).get(field, 0) or 0))
        except Exception:
            return Decimal(0)

    tu = sum((amt(x, "USDT", "free") + amt(x, "USDT", "locked") for x in CONFIG["accounts"]), Decimal(0))
    tunp = sum((amt(x, "UNP", "free") + amt(x, "UNP", "locked") for x in CONFIG["accounts"]), Decimal(0))
    unp_val = tunp * mid
    denom = unp_val + tu
    if denom <= 0:
        return Decimal(0)
    frac = unp_val / denom                                   # UNP share of total value, 0..1
    imbalance = max(Decimal(-1), min(Decimal(1), (frac - Decimal("0.5")) * 2))
    return (-Decimal(str(CONFIG["max_skew"])) * imbalance).quantize(Decimal("0.00001"))


def compute_inner():
    """(2) Volatility-based spread: choose the inner quote offset from recent
    price movement — tight when calm, wide when volatile. Returns a Decimal
    fraction. Committed at each re-centre so the ladder stays stable between.
    HARD-CAPPED so the quoted spread (2*inner) can never exceed max_spread_pct."""
    cap = Decimal(str(CONFIG["max_spread_pct"])) / 2      # inner <= half the spread ceiling
    base = min(Decimal(str(CONFIG["inner_pct"])), cap)
    if not CONFIG.get("vol_spread_enabled"):
        return base
    w = STATE.get("mid_window") or []
    lo = min(Decimal(str(CONFIG["inner_min"])), cap)
    if len(w) < 3:
        return lo
    mean = sum(w) / len(w)
    vol = (Decimal(str(max(w))) - Decimal(str(min(w)))) / Decimal(str(mean)) if mean else Decimal(0)
    STATE["vol"] = float(vol)
    calm = Decimal(str(CONFIG["vol_calm"]))
    wild = Decimal(str(CONFIG["vol_wild"]))
    hi = min(Decimal(str(CONFIG["inner_max"])), cap)
    if wild <= calm:
        return lo
    t = max(Decimal(0), min(Decimal(1), (vol - calm) / (wild - calm)))
    return (lo + (hi - lo) * t).quantize(Decimal("0.0001"))


def _open_orders(raw):
    """Filter the integration's order list to live/open UNPUSDT orders."""
    out = []
    for o in (raw or {}).get("orders", []):
        if (o.get("symbol") or "").upper() not in (MEXC_SYMBOL, "UNP/USDT", "UNPUSDT", "UNP_USDT"):
            continue
        if str(o.get("status", "")).lower() in _TERMINAL:
            continue
        try:
            price = Decimal(str(o.get("price")))
        except Exception:
            continue
        try:
            rem = o.get("remainingQuantity")
            qty = Decimal(str(rem if rem is not None else o.get("quantity") or 0))
        except Exception:
            qty = Decimal(0)
        out.append({
            "id": o.get("id") or o.get("orderId"),
            "account": int(o.get("account") or 0),
            "side": str(o.get("side", "")).lower(),   # buy / sell
            "price": price,
            "qty": qty,
        })
    return out


def build_targets(mid):
    """Return two lists (bids, asks) of dicts {price, account, qty} for the ladder."""
    n = CONFIG["levels"]
    inner = Decimal(str(STATE.get("active_inner") or CONFIG["inner_pct"]))
    band = Decimal(str(CONFIG["band_pct"]))
    accts = CONFIG["accounts"]
    step = (band - inner) / (n - 1) if n > 1 else Decimal(0)
    min_usd = Decimal(str(CONFIG["min_order_usdt"]))

    def side(direction):
        levels = []
        for i in range(n):
            frac = inner + step * i
            px = (mid * (1 - frac)) if direction == "buy" else (mid * (1 + frac))
            px = px.quantize(_PQ)
            if px <= 0:
                continue
            levels.append({"price": px, "account": accts[i % len(accts)]})
        return levels

    bids = side("buy")
    asks = side("sell")

    # size each level from its account's free balance for that side
    bals = STATE.get("balances") or {}

    def bal(acct, asset, field):
        a = ((bals.get("accounts") or {}).get(str(acct)) or {}).get("assets", {}) or {}
        node = a.get(asset, {})
        try:
            return Decimal(str(node.get(field, 0)))
        except Exception:
            return Decimal(0)

    def total(acct, asset):
        # free + locked: locked capital is already sitting in OUR existing orders,
        # so per-order size must be based on the whole balance, not just what is
        # free right now — otherwise, as capital gets deployed/consumed, order size
        # shrinks below the exchange minimum and missing levels can never refill.
        return bal(acct, asset, "free") + bal(acct, asset, "locked")

    target_side = Decimal(str(CONFIG["target_side_usd"]))
    res_usdt = Decimal(str(CONFIG["reserve_usdt"]))
    res_unp = Decimal(str(CONFIG["reserve_unp"]))
    for acct in CONFIG["accounts"]:
        # BIDS: sized from USABLE USDT = total minus the per-account reserve we must keep free
        acct_bids = [b for b in bids if b["account"] == acct]
        if acct_bids:
            usable_usdt = max(Decimal(0), total(acct, "USDT") - res_usdt)
            budget = min(target_side * Decimal(len(acct_bids)) / Decimal(n), usable_usdt)
            per = budget / Decimal(len(acct_bids)) if budget > 0 else Decimal(0)
            for b in acct_bids:
                q = (per / b["price"]) if b["price"] > 0 else Decimal(0)
                b["qty"] = q.quantize(_QQ, rounding=ROUND_DOWN)
        # ASKS: sized from USABLE UNP = total minus the per-account UNP reserve
        acct_asks = [a for a in asks if a["account"] == acct]
        if acct_asks:
            usable_unp = max(Decimal(0), total(acct, "UNP") - res_unp)
            unp_budget = min((target_side / mid) * Decimal(len(acct_asks)) / Decimal(n), usable_unp)
            per_unp = unp_budget / Decimal(len(acct_asks)) if unp_budget > 0 else Decimal(0)
            for a in acct_asks:
                a["qty"] = per_unp.quantize(_QQ, rounding=ROUND_DOWN)

    # drop levels too small to meet the exchange minimum
    def viable(o):
        q = o.get("qty") or Decimal(0)
        return q > 0 and (q * o["price"]) >= min_usd
    return [b for b in bids if viable(b)], [a for a in asks if viable(a)]


def reconcile():
    """One market-making pass: refresh balances, read the live book, cancel
    out-of-band orders, and place any missing ladder levels around the live mid."""
    live_mid = fetch_mid()
    if live_mid is None:
        STATE["last_error"] = "no usable mid-price (guard)"
        return
    STATE["mid"] = str(live_mid)

    # (2) roll the mid-price history used for the volatility estimate
    w = STATE["mid_window"]
    w.append(float(live_mid))
    win = max(3, int(CONFIG["vol_window"]))
    if len(w) > win:
        del w[0:len(w) - win]

    # balances first — needed for the inventory skew and for sizing
    STATE["balances"] = api.get_balances()

    # (1) inventory skew -> target centre = real mid nudged toward rebalancing
    skew = compute_skew(STATE["balances"], live_mid)
    target_center = live_mid * (Decimal(1) + skew)

    # Re-centre (and commit a fresh vol-based inner + skew) only when the target
    # centre drifts more than reprice_pct from the built anchor. Between re-centres
    # the ladder is FIXED, so existing orders keep matching their targets (no churn).
    # target_center moves with BOTH the real price (3: tracking) and inventory (1: skew).
    if STATE["anchor_mid"] is None:
        STATE["anchor_mid"] = str(target_center)
        STATE["active_inner"] = str(compute_inner())
        STATE["active_skew"] = float(skew)
    anchor = Decimal(STATE["anchor_mid"])
    if anchor > 0 and abs(target_center - anchor) / anchor > Decimal(str(CONFIG["reprice_pct"])):
        anchor = target_center
        STATE["anchor_mid"] = str(target_center)
        STATE["active_inner"] = str(compute_inner())   # (2) commit vol-based spread
        STATE["active_skew"] = float(skew)             # (1) commit inventory skew
    mid = anchor

    # breathing cycle: set this minute's per-side order count (>= 30 floor)
    CONFIG["levels"] = current_levels()
    STATE["levels_now"] = CONFIG["levels"]

    bids, asks = build_targets(mid)
    live = _open_orders(api.get_orders())

    n = CONFIG["levels"]
    inner = Decimal(str(STATE.get("active_inner") or CONFIG["inner_pct"]))
    band = Decimal(str(CONFIG["band_pct"]))
    step = (band - inner) / (n - 1) if n > 1 else Decimal("0.001")
    tol = (mid * step) / 2  # half a level = matching tolerance

    # in-band window (a touch wider than band so edge orders are kept, not churned)
    lo_keep = mid * (1 - band - step)
    hi_keep = mid * (1 + band + step)

    placed = cancelled = 0

    # 1) cancel live orders that fall outside the current band (stale / wrong side)
    for o in live:
        p = o["price"]
        out_of_band = p < lo_keep or p > hi_keep
        wrong_side = (o["side"] == "buy" and p > mid) or (o["side"] == "sell" and p < mid)
        if out_of_band or wrong_side:
            if o["id"]:
                api.cancel_order(o["id"])
                cancelled += 1

    # 2) place missing ladder levels (per side/account), capped per loop
    def covered(target, side):
        for o in live:
            if o["side"] == side and o["account"] == target["account"] and abs(o["price"] - target["price"]) <= tol:
                return True
        return False

    # track free balance per account so we only place orders we can actually fund
    def _free(acct, asset):
        a = ((STATE["balances"].get("accounts") or {}).get(str(acct)) or {}).get("assets", {}) or {}
        node = a.get(asset, {})
        try:
            return Decimal(str(node.get("free", 0)))
        except Exception:
            return Decimal(0)
    free_usdt = {a: _free(a, "USDT") for a in CONFIG["accounts"]}
    free_unp = {a: _free(a, "UNP") for a in CONFIG["accounts"]}

    budget_left = CONFIG["max_place_per_loop"]
    for target, side in ([(b, "buy") for b in bids] + [(a, "sell") for a in asks]):
        if budget_left <= 0:
            break
        if covered(target, side):
            continue
        acct = target["account"]; px = target["price"]; qty = target["qty"]
        res_usdt = Decimal(str(CONFIG["reserve_usdt"]))
        res_unp = Decimal(str(CONFIG["reserve_unp"]))
        if side == "buy":
            val = px * qty
            if free_usdt.get(acct, Decimal(0)) - val < res_usdt:
                continue  # would dip into the USDT reserve -> skip
        else:
            if free_unp.get(acct, Decimal(0)) - qty < res_unp:
                continue  # would dip into the UNP reserve -> skip
        resp = send_limit(side, acct, px, qty)
        if isinstance(resp, dict) and resp.get("ok") is not False:
            placed += 1
            if side == "buy":
                free_usdt[acct] -= val
            else:
                free_unp[acct] -= qty
        budget_left -= 1

    # 2b) hard cap per side (safeguard): never keep more than LEVELS live orders on
    # a side. Cancel the outermost excess so inventory can never get locked in a
    # runaway pile of one-sided orders.
    live_cap = _open_orders(api.get_orders())
    for sd in ("buy", "sell"):
        so = [o for o in live_cap if o["side"] == sd]
        if len(so) > n:
            so.sort(key=lambda o: abs(o["price"] - mid), reverse=True)  # furthest first
            for o in so[: len(so) - n]:
                if o["id"]:
                    api.cancel_order(o["id"])
                    cancelled += 1

    STATE["actions"]["placed"] += placed
    STATE["actions"]["cancelled"] += cancelled

    # 3) compliance snapshot from the (post-action) live view
    live2 = _open_orders(api.get_orders())
    bid_ct = sum(1 for o in live2 if o["side"] == "buy")
    ask_ct = sum(1 for o in live2 if o["side"] == "sell")
    best_bid = max((o["price"] for o in live2 if o["side"] == "buy"), default=None)
    best_ask = min((o["price"] for o in live2 if o["side"] == "sell"), default=None)
    spread_pct = None
    if best_bid and best_ask and best_bid > 0:
        spread_pct = float((best_ask - best_bid) / ((best_ask + best_bid) / 2) * 100)

    # HARD SPREAD CEILING: if a fill just widened the visible spread past max_spread_pct,
    # re-place the inner bid+ask THIS loop so the spread can never sit above the ceiling.
    max_spread = float(CONFIG["max_spread_pct"]) * 100
    if spread_pct is not None and spread_pct > max_spread:
        STATE["spread_guard_trips"] = STATE.get("spread_guard_trips", 0) + 1
        infr = Decimal(str(STATE.get("active_inner") or CONFIG["inner_pct"]))
        per_usd = Decimal(str(CONFIG["target_side_usd"])) / Decimal(max(1, n))
        res_u = Decimal(str(CONFIG["reserve_usdt"]))
        res_n = Decimal(str(CONFIG["reserve_unp"]))
        ibp = (mid * (1 - infr)).quantize(_PQ)   # inner bid
        if ibp > 0:
            ibq = (per_usd / ibp).quantize(_QQ, rounding=ROUND_DOWN)
            for acct in CONFIG["accounts"]:
                if ibq > 0 and free_usdt.get(acct, Decimal(0)) - ibp * ibq >= res_u:
                    send_limit("buy", acct, ibp, ibq); free_usdt[acct] -= ibp * ibq; break
        iap = (mid * (1 + infr)).quantize(_PQ)   # inner ask
        if iap > 0:
            iaq = (per_usd / iap).quantize(_QQ, rounding=ROUND_DOWN)
            for acct in CONFIG["accounts"]:
                if iaq > 0 and free_unp.get(acct, Decimal(0)) - iaq >= res_n:
                    send_limit("sell", acct, iap, iaq); free_unp[acct] -= iaq; break

    lo2, hi2 = mid * Decimal("0.98"), mid * Decimal("1.02")

    def depth(side):
        tot = Decimal(0)
        for o in live2:
            if o["side"] == side and lo2 <= o["price"] <= hi2:
                tot += o["price"] * o["qty"]
        return tot
    bid_depth = depth("buy")
    ask_depth = depth("sell")
    combined = bid_depth + ask_depth
    STATE["compliance"] = {
        "bid_count": bid_ct,
        "ask_count": ask_ct,
        "min_side_ok": bid_ct >= 30 and ask_ct >= 30,
        "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
        "spread_ok": (spread_pct is not None and spread_pct < 2.0),
        "spread_ceiling_pct": max_spread,
        "spread_guard_trips": STATE.get("spread_guard_trips", 0),
        "best_bid": str(best_bid) if best_bid else None,
        "best_ask": str(best_ask) if best_ask else None,
        "bid_depth_usd": round(float(bid_depth), 2),
        "ask_depth_usd": round(float(ask_depth), 2),
        "depth_usd_within_2pct": round(float(combined), 2),
        "depth_ok": combined > Decimal("1000"),
        "placed_this_loop": placed,
        "cancelled_this_loop": cancelled,
    }
    STATE["last_error"] = None


def _worker():
    STATE["started_at"] = STATE["started_at"] or time.time()
    while STATE["running"]:
        try:
            with _LOCK:
                reconcile()
            STATE["round"] += 1
            STATE["last_loop_at"] = time.time()
        except Exception as exc:
            STATE["last_error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        # sleep in small slices so stop is responsive
        slept = 0.0
        while STATE["running"] and slept < CONFIG["refresh_sec"]:
            time.sleep(0.5); slept += 0.5


def _supervisor():
    """Restart the worker if it ever dies while running (resilience)."""
    while True:
        if STATE["running"]:
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join()
            if STATE["running"]:
                STATE["last_error"] = "worker exited; restarting"
                time.sleep(2)
        else:
            time.sleep(1)


# ----------------------------------------------------------------------------- HTTP
STATUS_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>market4 - MEXC liquidity maker</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{background:#0a0e17;color:#cbd5e1;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}
 h1{font-size:18px;color:#e2e8f0;margin:0 0 4px} .sub{color:#64748b;font-size:12px;margin-bottom:18px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;max-width:900px}
 .card{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:14px}
 .k{color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 .v{font-size:22px;color:#e2e8f0;margin-top:4px} .ok{color:#34d399}.bad{color:#f87171}
 .row{display:flex;gap:8px;margin-top:16px}.btn{background:#1f2937;border:1px solid #374151;color:#e2e8f0;padding:8px 16px;border-radius:8px;cursor:pointer}
 .btn.go{background:#065f46;border-color:#047857}.btn.stop{background:#7f1d1d;border-color:#991b1b}
</style></head><body>
<h1>market4 &mdash; MEXC liquidity market-maker</h1>
<div class=sub>Re-centering ladder &middot; keeps 30+ orders/side within &plusmn;2% &middot; spread &lt; 2%</div>
<div class=grid id=g></div>
<div class=row><button class="btn go" onclick="act('start')">Start</button>
<button class="btn stop" onclick="act('stop')">Stop</button></div>
<script>
async function tick(){let s=await (await fetch('/api/state')).json();let c=s.compliance||{};
 let cell=(k,v,ok)=>`<div class=card><div class=k>${k}</div><div class="v ${ok===undefined?'':(ok?'ok':'bad')}">${v}</div></div>`;
 document.getElementById('g').innerHTML=
  cell('Running',s.running?'YES':'no',s.running)+
  cell('Bids live',c.bid_count??'-',c.bid_count>=30)+
  cell('Asks live',c.ask_count??'-',c.ask_count>=30)+
  cell('Spread %',(c.spread_pct??'-'),c.spread_ok)+
  cell('Depth ±2% $',(c.depth_usd_within_2pct??'-'),c.depth_ok)+
  cell('Mid',s.mid??'-')+
  cell('Best bid',c.best_bid??'-')+
  cell('Best ask',c.best_ask??'-')+
  cell('Round',s.round)+
  cell('Levels/side',s.levels_now??'-')+
  cell('Inv skew %',s.active_skew!=null?(s.active_skew*100).toFixed(3):'-')+
  cell('Volatility %',s.vol!=null?(s.vol*100).toFixed(2):'-')+
  cell('Placed/Cxl',(s.actions?.placed||0)+' / '+(s.actions?.cancelled||0))+
  cell('Last error',s.last_error||'none',!s.last_error);}
async function act(a){await fetch('/api/'+a,{method:'POST'});setTimeout(tick,500);}
tick();setInterval(tick,4000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, STATUS_HTML, "text/html; charset=utf-8")
        if self.path == "/health":
            return self._send(200, "ok", "text/plain")
        if self.path == "/api/state":
            return self._send(200, json.dumps(STATE, default=str))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln) if ln else b""
        if self.path == "/api/start":
            STATE["running"] = True
            STATE["started_at"] = STATE["started_at"] or time.time()
            return self._send(200, json.dumps({"ok": True, "running": True}))
        if self.path == "/api/stop":
            STATE["running"] = False
            return self._send(200, json.dumps({"ok": True, "running": False}))
        if self.path == "/api/resync":
            # emergency: cancel everything, clear anchor, rebuild next loop
            for acct in CONFIG["accounts"]:
                api.cancel_all(acct)
            STATE["anchor_mid"] = None
            return self._send(200, json.dumps({"ok": True, "note": "cancelled all; will rebuild"}))
        if self.path == "/api/config":
            try:
                body = json.loads(raw.decode() or "{}")
                for k, v in body.items():
                    if k in CONFIG and k != "accounts":
                        CONFIG[k] = type(CONFIG[k])(v)
                return self._send(200, json.dumps({"ok": True, "config": CONFIG}, default=str))
            except Exception as exc:
                return self._send(400, json.dumps({"ok": False, "error": str(exc)}))
        return self._send(404, json.dumps({"error": "not found"}))


def main():
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    port = int(os.environ.get("PANEL_PORT", "8787"))
    threading.Thread(target=_supervisor, daemon=True).start()
    if os.environ.get("MM_AUTOSTART", "1") == "1":
        STATE["running"] = True
        STATE["started_at"] = time.time()
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"market4 MM server on {host}:{port}  autostart={STATE['running']}  levels={CONFIG['levels']}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
