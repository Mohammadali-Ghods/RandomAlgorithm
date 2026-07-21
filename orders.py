"""Order sender for the Exuno training API.

Takes the 10 sorted values from AlgorithmA and, over exactly one minute, sends
each one as a LIMIT buy AND a LIMIT sell for UNPUSDT.

Timing:  60s / 10 values = one value every 6 seconds (both APIs each tick).

Pricing per item (values are sorted low -> high):
  * first N items : SendBuy price = value            SendSell price = value
                    (same price on both accounts -> they cross and FILL)
  * items after N : SendBuy price = value - discount SendSell price = value + discount
                    (bid below / ask above -> they REST in the book, no fill)

Every order expires (auto-cancels if unfilled) after 60 seconds.

This drives an order book / candle demo for students; it is not trading advice.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import List, Union

from algorithm_a import algorithm_a

Number = Union[int, float, str, Decimal]

BASE_URL = os.environ.get("EXUNO_BASE_URL", "https://integrate.exuno.io")
# Secret — provided via the environment, never committed. Set EXUNO_TOKEN.
TOKEN = os.environ.get("EXUNO_TOKEN", "")
if not TOKEN:
    print("WARNING: EXUNO_TOKEN is not set — API calls will be unauthorized.")

# Some edge/WAF layers reject the default urllib agent (Cloudflare err 1010).
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

EXPIRE_SECONDS = 60          # each order auto-cancels after 1 minute
INTERVAL_SECONDS = 6         # 60s / 10 values
SAME_PRICE_COUNT = 3         # first N items keep the raw value on both sides
BUY_DISCOUNT = Decimal("0.1")  # subtracted from buy price for the rest

# Prices allow up to 5 decimals, quantity up to 2 decimals (per API docs).
_PRICE_Q = Decimal("0.00001")
_QTY_Q = Decimal("0.01")


def _get(path: str) -> dict:
    """GET JSON from the API and return the parsed response (or an error dict)."""
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        method="GET",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode()}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}


def get_balances() -> dict:
    """UNP + USDT for both accounts in one call."""
    return _get("/balances")


def get_balance(account: int) -> dict:
    """UNP + USDT for a single account (1 or 2)."""
    return _get(f"/balances/{account}")


def get_orders() -> dict:
    """All orders known to the service (open / filled / cancelled / ...)."""
    return _get("/orders")


def get_order(order_id: str) -> dict:
    """Status of a single order by its returned id."""
    return _get(f"/orders/{order_id}")


def _free(account_blob: dict, asset: str) -> Decimal:
    """Free (available) amount of an asset from one account's balance blob:
    { "account": n, "ok": true, "assets": { "UNP": {"free": ...}, ... } }.
    Returns 0 if missing or the account errored."""
    assets = (account_blob or {}).get("assets", {})
    node = assets.get(asset, {})
    raw = node.get("free", node.get("available", 0)) if isinstance(node, dict) else node
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal(0)


def decide_roles(balances: dict) -> dict:
    """Pick which account buys and which sells, from the live balances.

    A buyer spends USDT to acquire UNP -> give BUY to the account with more
    free USDT.  A seller gives up UNP for USDT -> give SELL to the account with
    more free UNP.  If one account wins both, it takes the role it is richer in
    and the other account gets the remaining role.
    """
    accounts = balances.get("accounts", {})
    b1 = accounts.get("1") or accounts.get(1) or {}
    b2 = accounts.get("2") or accounts.get(2) or {}

    usdt1, usdt2 = _free(b1, "USDT"), _free(b2, "USDT")
    unp1, unp2 = _free(b1, "UNP"), _free(b2, "UNP")

    buyer = 1 if usdt1 >= usdt2 else 2      # more USDT -> buyer
    seller = 1 if unp1 >= unp2 else 2       # more UNP  -> seller

    if buyer == seller:
        # One account leads on both assets; split so both roles are staffed.
        other = 2 if buyer == 1 else 1
        # Keep the dominant account on whichever role its lead is larger for.
        usdt_lead = abs(usdt1 - usdt2)
        unp_lead = abs(unp1 - unp2)
        if usdt_lead >= unp_lead:
            seller = other
        else:
            buyer = other

    return {
        "buyer": buyer,
        "seller": seller,
        "detail": {
            "1": {"USDT_free": usdt1, "UNP_free": unp1},
            "2": {"USDT_free": usdt2, "UNP_free": unp2},
        },
    }


def _post(path: str, body: dict) -> dict:
    """POST JSON to the API and return the parsed response (or an error dict)."""
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode()}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}


def _fmt(value: Decimal, quantum: Decimal) -> str:
    """Format a Decimal to the API's precision, without scientific notation."""
    return format(value.quantize(quantum), "f")


def send_buy(account: int, price: Decimal, quantity: Number) -> dict:
    return _post("/SendBuy", {
        "account": account,
        "price": _fmt(Decimal(str(price)), _PRICE_Q),
        "quantity": _fmt(Decimal(str(quantity)), _QTY_Q),
        "expire": EXPIRE_SECONDS,
    })


def send_sell(account: int, price: Decimal, quantity: Number) -> dict:
    return _post("/SendSell", {
        "account": account,
        "price": _fmt(Decimal(str(price)), _PRICE_Q),
        "quantity": _fmt(Decimal(str(quantity)), _QTY_Q),
        "expire": EXPIRE_SECONDS,
    })


def send_orders(
    values: List[Decimal],
    buyer_account: int,
    seller_account: int,
    quantity: Number,
) -> List[dict]:
    """Send a buy (on buyer_account) + sell (on seller_account) for each value,
    one pair every 6 seconds.

    Args:
        values:         the 10 sorted Decimals from AlgorithmA.
        buyer_account:  1 or 2 — account that places the BUY orders (spends USDT).
        seller_account: 1 or 2 — account that places the SELL orders (spends UNP).
        quantity:       UNP amount per order. Note the API's ~1 USDT minimum
                        order value (price * quantity), so keep price*qty >= ~1.

    Returns a list of {"index", "value", "buy", "sell"} records, one per item.
    """
    results: List[dict] = []
    total = len(values)

    for i, value in enumerate(values):
        if i < SAME_PRICE_COUNT:
            buy_price = value          # same price on both sides -> crosses & fills
            sell_price = value
        else:
            buy_price = value - BUY_DISCOUNT   # bid below  -> rests, no fill
            sell_price = value + BUY_DISCOUNT  # ask above  -> rests, no fill

        buy_resp = send_buy(buyer_account, buy_price, quantity)
        sell_resp = send_sell(seller_account, sell_price, quantity)

        record = {
            "index": i,
            "value": _fmt(value, _PRICE_Q),
            "buy": {"price": _fmt(buy_price, _PRICE_Q), "response": buy_resp},
            "sell": {"price": _fmt(sell_price, _PRICE_Q), "response": sell_resp},
        }
        results.append(record)
        print(
            f"[{i + 1}/{total}] "
            f"BUY  acct{buyer_account} @ {record['buy']['price']} -> ok={buy_resp.get('ok')}  |  "
            f"SELL acct{seller_account} @ {record['sell']['price']} -> ok={sell_resp.get('ok')}"
        )

        # Space the pairs 6s apart; no wait after the final one.
        if i < total - 1:
            time.sleep(INTERVAL_SECONDS)

    return results


if __name__ == "__main__":
    # 1) Read live balances and decide roles (read-only, no orders).
    balances = get_balances()
    roles = decide_roles(balances)
    print("Roles from balances ->", {"buyer": roles["buyer"], "seller": roles["seller"]})
    for acct, d in roles["detail"].items():
        print(f"  acct{acct}: USDT_free={d['USDT_free']}  UNP_free={d['UNP_free']}")

    # 2) Build the values and fire buy+sell pairs (this places LIVE orders).
    vals = algorithm_a("0.10000", "0.12000")
    print("Values:", [_fmt(v, _PRICE_Q) for v in vals])
    send_orders(
        vals,
        buyer_account=roles["buyer"],
        seller_account=roles["seller"],
        quantity=20,
    )
