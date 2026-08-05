"""mm_monitor — passive health monitor + email reporter for the market makers.

READ-ONLY. It never places/cancels orders, never changes any bot config, and
never runs an AI/agent. It only:
  1. Polls each maker's /api/state (market4 = MEXC, market5 = CoinW).
  2. Pulls each exchange's 24h volume + average price from the public ticker.
  3. Emails info@botify.trade an HOURLY status summary (all panel stats + 24h
     volume/avg price + the average across both markets).
  4. Emails IMMEDIATELY (not waiting for the hour) when something bad is
     detected, and again a "recovered" note when it clears.

Email transport = Cloudflare email/sending/send (same as the rest of the stack);
creds come from env / watchdog.env (CF_EMAIL_TOKEN, CF_ACCOUNT_ID, EMAIL_FROM,
EMAIL_TO). Stdlib only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    path = os.path.join(HERE, "watchdog.env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

# ---- config ----
POLL_SEC = int(os.environ.get("MON_POLL_SEC", "60"))          # health-check cadence
SUMMARY_SEC = int(os.environ.get("MON_SUMMARY_SEC", "3600"))  # hourly summary
RENOTIFY_SEC = int(os.environ.get("MON_RENOTIFY_SEC", "1800"))  # re-remind a still-broken thing
MIN_SIDE = int(os.environ.get("MON_MIN_SIDE", "30"))          # MEXC order-count floor
MIN_DEPTH = float(os.environ.get("MON_MIN_DEPTH", "1000"))    # $ depth within +/-2%
MAX_SPREAD = float(os.environ.get("MON_MAX_SPREAD", "1.5"))   # % positive spread ceiling

# Markets: name, panel container host (on the docker network), exchange, ticker
TARGETS = [
    {"name": "market4", "label": "MEXC", "url": os.environ.get("MON_MEXC_URL", "http://exuno-market-panel4:8787/api/state"),
     "exchange": "mexc"},
    {"name": "market5", "label": "CoinW", "url": os.environ.get("MON_COINW_URL", "http://exuno-market-coinw-mm:8787/api/state"),
     "exchange": "coinw"},
]

MEXC_TICKER = os.environ.get("MON_MEXC_TICKER", "https://api.mexc.com/api/v3/ticker/24hr?symbol=UNPUSDT")
COINW_TICKER = os.environ.get("MON_COINW_TICKER", "https://api.coinw.com/api/v1/public?command=returnTicker")
COINW_SYMBOL = os.environ.get("MON_COINW_SYMBOL", "UNP_USDT")

# FixTheProblem incident console — mint a one-time link on alerts (optional)
FP_INCIDENT_URL = os.environ.get("FP_INCIDENT_URL", "http://exuno-fixproblem:8790/internal/incident")
FP_INTERNAL_TOKEN = os.environ.get("FP_INTERNAL_TOKEN", "")

_acc = os.environ.get("CF_ACCOUNT_ID", "")
EMAIL = {
    "endpoint": os.environ.get("CF_EMAIL_ENDPOINT")
    or (f"https://api.cloudflare.com/client/v4/accounts/{_acc}/email/sending/send" if _acc else ""),
    "token": os.environ.get("CF_EMAIL_TOKEN", ""),
    "from": os.environ.get("EMAIL_FROM", "info@exuno.io"),
    "to": os.environ.get("EMAIL_TO", "info@botify.trade"),
}
UA = "mm-monitor/1.0"


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


# ---- email ----
def send_email(subject, html, text=None):
    if not EMAIL["endpoint"] or not EMAIL["token"]:
        log("email NOT configured (endpoint/token missing)")
        return False
    if text is None:
        text = subject
    payload = json.dumps({
        "to": EMAIL["to"], "from": EMAIL["from"],
        "subject": subject, "html": html, "text": text,
    }).encode()
    req = urllib.request.Request(
        EMAIL["endpoint"], data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {EMAIL['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode()
        ok = '"success":true' in body.replace(" ", "")
        log(f"email {'sent' if ok else 'FAILED'}: {subject}")
        return ok
    except Exception as e:
        log(f"email error: {type(e).__name__}: {str(e)[:200]}")
        return False


# ---- data fetch ----
def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_state(t):
    """Return the maker's /api/state dict, or {'_err': ...} if unreachable."""
    try:
        return _get_json(t["url"])
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {str(e)[:160]}"}


def fetch_market24h(exchange):
    """Return {'vol_base','vol_quote','avg','last'} 24h stats or {} on failure."""
    try:
        if exchange == "mexc":
            d = _get_json(MEXC_TICKER)
            vb = float(d.get("volume") or 0)
            vq = float(d.get("quoteVolume") or 0)
            avg = float(d.get("weightedAvgPrice") or 0) or (
                (float(d.get("highPrice") or 0) + float(d.get("lowPrice") or 0)) / 2)
            return {"vol_base": vb, "vol_quote": vq, "avg": avg, "last": float(d.get("lastPrice") or 0)}
        else:  # coinw — returnTicker has baseVolume (UNP) + high/low, no VWAP/quoteVolume
            d = _get_json(COINW_TICKER)
            t = (d.get("data") or {}).get(COINW_SYMBOL, {})
            vb = float(t.get("baseVolume") or 0)
            hi = float(t.get("high24hr") or 0)
            lo = float(t.get("low24hr") or 0)
            avg = ((hi + lo) / 2) if (hi and lo) else float(t.get("last") or 0)
            vq = vb * avg  # estimated USDT volume (no native quoteVolume field)
            return {"vol_base": vb, "vol_quote": vq, "avg": avg, "last": float(t.get("last") or 0)}
    except Exception as e:
        log(f"{exchange} ticker fetch failed: {type(e).__name__}: {str(e)[:120]}")
        return {}


# ---- interpret one market ----
VALUE_DROP_PCT = float(os.environ.get("MON_VALUE_DROP_PCT", "4"))   # alert if combined value falls > this %
VALUE_DROP_USD = float(os.environ.get("MON_VALUE_DROP_USD", "60"))  # ...or > this many $ vs last check
_last_value = {}   # market -> {"total": usd, "accts": {id: usd}}


def account_values(st):
    """Extract per-account USDT/UNP totals + a reference mid from a maker state.
    Returns dict {accts:{id:{usdt,unp}}, usdt, unp, mid, value} or None."""
    bals = (st or {}).get("balances") or {}
    accts = bals.get("accounts") if isinstance(bals, dict) else None
    if not accts:
        return None
    mid = st.get("mid")
    try:
        mid = float(mid)
    except (TypeError, ValueError):
        mid = None
    if not mid or mid <= 0:
        c = st.get("compliance", {}) or {}
        bb, ba = c.get("best_bid"), c.get("best_ask")
        mid = (float(bb) + float(ba)) / 2 if bb and ba else 0.11
    per = {}
    tu = tn = 0.0
    for aid, a in accts.items():
        assets = a.get("assets", a)
        def amt(sym, fld):
            v = assets.get(sym, {})
            try:
                return float(v.get(fld, 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        usdt = amt("USDT", "free") + amt("USDT", "locked")
        unp = amt("UNP", "free") + amt("UNP", "locked")
        per[aid] = {"usdt": round(usdt, 2), "unp": round(unp, 1)}
        tu += usdt; tn += unp
    return {"accts": per, "usdt": round(tu, 2), "unp": round(tn, 1),
            "mid": mid, "value": round(tu + tn * mid, 2)}


def value_drain(name, cur):
    """Compare current balances to last seen. Returns an alert string if real
    ASSETS left the wallet (drain), else None. Ignores USDT<->UNP conversion and
    UNP price swings by valuing the QUANTITY change at the current mid."""
    prev = _last_value.get(name)
    _last_value[name] = cur
    if not prev or cur is None:
        return None
    # value of the net quantity change, priced at NOW's mid (so a pure
    # conversion nets ~0 and a UNP price move doesn't count):
    d_usdt = cur["usdt"] - prev["usdt"]
    d_unp = cur["unp"] - prev["unp"]
    drain = d_usdt + d_unp * cur["mid"]          # negative => assets left
    if drain <= -VALUE_DROP_USD:
        return (f"ASSET DRAIN: ${-drain:.0f} of value left the wallet since last check "
                f"(USDT {prev['usdt']:.0f}->{cur['usdt']:.0f}, UNP {prev['unp']:.0f}->{cur['unp']:.0f}) "
                f"— NOT a normal conversion; possible extraction")
    return None


def snapshot(t):
    """Build a flat stats dict for one market and a list of problems."""
    st = fetch_state(t)
    s = {"name": t["name"], "label": t["label"], "exchange": t["exchange"]}
    problems = []
    if "_err" in st:
        s["reachable"] = False
        s["err"] = st["_err"]
        problems.append(f"UNREACHABLE — {st['_err']}")
        s["problems"] = problems
        s["m24"] = fetch_market24h(t["exchange"])
        return s
    s["reachable"] = True
    c = st.get("compliance", {}) or {}
    s["running"] = bool(st.get("running"))
    s["round"] = st.get("round")
    s["bids"] = c.get("bid_count")
    s["asks"] = c.get("ask_count")
    s["spread"] = c.get("spread_pct")
    s["depth"] = c.get("depth_usd_within_2pct")
    bb, ba = c.get("best_bid"), c.get("best_ask")
    s["best_bid"] = bb
    s["best_ask"] = ba
    mid = st.get("mid")
    if mid is None and bb and ba:
        mid = (float(bb) + float(ba)) / 2
    s["mid"] = mid
    s["levels"] = st.get("levels_now")
    sk = st.get("active_skew")
    s["skew"] = (sk * 100) if isinstance(sk, (int, float)) else None
    s["vol"] = st.get("vol")
    a = st.get("actions", {}) or {}
    s["placed"] = a.get("placed")
    s["cancelled"] = a.get("cancelled")
    s["last_error"] = st.get("last_error")
    s["m24"] = fetch_market24h(t["exchange"])
    s["value"] = account_values(st)
    drain = value_drain(s["name"], s["value"])
    if drain:
        problems.append(drain)

    # ---- health rules (negative spread is EXPECTED here from the co-running
    # old bots market1/market2, so we do NOT alarm on crossing) ----
    if not s["running"]:
        problems.append("BOT NOT RUNNING")
    if s["last_error"]:
        problems.append(f"LAST ERROR: {s['last_error']}")
    if isinstance(s["bids"], int) and s["bids"] < MIN_SIDE:
        problems.append(f"BIDS {s['bids']} < {MIN_SIDE} floor")
    if isinstance(s["asks"], int) and s["asks"] < MIN_SIDE:
        problems.append(f"ASKS {s['asks']} < {MIN_SIDE} floor")
    if isinstance(s["depth"], (int, float)) and s["depth"] < MIN_DEPTH:
        problems.append(f"DEPTH ${s['depth']:.0f} < ${MIN_DEPTH:.0f}")
    if isinstance(s["spread"], (int, float)) and s["spread"] > MAX_SPREAD:
        problems.append(f"SPREAD +{s['spread']:.2f}% > {MAX_SPREAD}% ceiling")
    s["problems"] = problems
    return s


# ---- formatting ----
def _f(v, nd=None, dash="-"):
    if v is None:
        return dash
    if isinstance(v, bool):
        return "YES" if v else "NO"
    if isinstance(v, (int, float)) and nd is not None:
        return f"{v:.{nd}f}"
    return str(v)


ROWS = [
    ("RUNNING", lambda s: _f(s.get("running"))),
    ("BIDS LIVE", lambda s: _f(s.get("bids"))),
    ("ASKS LIVE", lambda s: _f(s.get("asks"))),
    ("SPREAD %", lambda s: _f(s.get("spread"), 3)),
    ("DEPTH ±2% $", lambda s: _f(s.get("depth"), 2)),
    ("MID", lambda s: _f(s.get("mid"), 5)),
    ("BEST BID", lambda s: _f(s.get("best_bid"))),
    ("BEST ASK", lambda s: _f(s.get("best_ask"))),
    ("ROUND", lambda s: _f(s.get("round"))),
    ("LEVELS/SIDE", lambda s: _f(s.get("levels"))),
    ("INV SKEW %", lambda s: _f(s.get("skew"), 3)),
    ("VOLATILITY %", lambda s: _f(s.get("vol"))),
    ("PLACED/CXL", lambda s: f"{_f(s.get('placed'))} / {_f(s.get('cancelled'))}"),
    ("LAST ERROR", lambda s: _f(s.get("last_error"), dash="none")),
    ("24h VOL (base)", lambda s: _f(s.get("m24", {}).get("vol_base"), 0)),
    ("24h VOL ($)", lambda s: _f(s.get("m24", {}).get("vol_quote"), 2)),
    ("24h AVG PRICE", lambda s: _f(s.get("m24", {}).get("avg"), 5)),
    ("WALLET USDT", lambda s: _f((s.get("value") or {}).get("usdt"), 2)),
    ("WALLET UNP", lambda s: _f((s.get("value") or {}).get("unp"), 0)),
    ("WALLET VALUE $", lambda s: _f((s.get("value") or {}).get("value"), 0)),
]


def build_html(snaps, title):
    cols = "".join(f"<th style='padding:8px 14px;text-align:right;color:#7dd3fc'>{s['label']}<br><span style='color:#64748b;font-weight:400'>{s['name']}</span></th>" for s in snaps)
    body = ""
    for label, fn in ROWS:
        cells = ""
        for s in snaps:
            val = fn(s) if s.get("reachable") else ("-" if label != "RUNNING" else "UNREACHABLE")
            color = "#e2e8f0"
            if label == "RUNNING":
                color = "#4ade80" if s.get("running") else "#f87171"
            if label == "LAST ERROR" and s.get("last_error"):
                color = "#f87171"
            cells += f"<td style='padding:8px 14px;text-align:right;color:{color};font-variant-numeric:tabular-nums'>{val}</td>"
        body += f"<tr><td style='padding:8px 14px;color:#94a3b8'>{label}</td>{cells}</tr>"

    return f"""<div style="font-family:ui-sans-serif,system-ui,Arial;background:#0b1220;color:#e2e8f0;padding:20px">
<h2 style="margin:0 0 4px">{title}</h2>
<div style="color:#64748b;margin-bottom:16px">{now()}</div>
<table style="border-collapse:collapse;background:#111a2e;border-radius:10px;overflow:hidden">
<tr style="background:#0f1729"><th style="padding:8px 14px;text-align:left;color:#94a3b8">METRIC</th>{cols}</tr>
{body}
</table>
<div style="color:#475569;margin-top:16px;font-size:12px">Passive monitor · read-only · negative spread on a market = its co-running old bot (market1/market2) crossing, not a fault.</div>
</div>"""


def _severity(problem):
    """CRITICAL = maker down / erroring / unreachable; HIGH = compliance breach."""
    p = problem.upper()
    if ("NOT RUNNING" in p) or ("UNREACHABLE" in p) or ("LAST ERROR" in p):
        return "CRITICAL"
    return "HIGH"


def mint_incident_link(new_problems, snaps):
    """Ask the FixTheProblem app for a single-use 30-min magic link, pre-loaded
    with the problem context. Returns the URL or None (never blocks the alert)."""
    if not FP_INTERNAL_TOKEN:
        return None
    problem = {
        "detected": now(),
        "problems": [{"market": s["name"], "exchange": s["label"], "issue": p,
                      "severity": _severity(p)} for s, p in new_problems],
        "snapshot": [{k: s.get(k) for k in ("name", "label", "running", "bids", "asks",
                      "spread", "depth", "mid", "last_error", "round")} for s in snaps],
    }
    try:
        req = urllib.request.Request(
            FP_INCIDENT_URL, data=json.dumps({"problem": problem}).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {FP_INTERNAL_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("link")
    except Exception as e:
        log(f"incident link mint failed: {type(e).__name__}: {str(e)[:120]}")
        return None


def build_alert(new_problems, snaps, fix_link=None):
    """Return (subject, html, text) for a deliberately ALARMING problem email —
    designed so the admin cannot ignore it."""
    worst = "CRITICAL" if any(_severity(p) == "CRITICAL" for _, p in new_problems) else "HIGH"
    markets = sorted({s["name"] for s, _ in new_problems})
    banner_bg = "#7f1d1d" if worst == "CRITICAL" else "#9a3412"
    tag = "🚨 CRITICAL" if worst == "CRITICAL" else "🚨 URGENT"
    headline = ("MARKET MAKER DOWN — TRADING AT RISK" if worst == "CRITICAL"
                else "MARKET MAKER COMPLIANCE BREACH")

    # subject — short, loud, and specific
    short = "; ".join(f"{s['name']}: {p.split('—')[0].split(':')[0].strip()}"
                      for s, p in new_problems)
    subject = f"{tag} 🚨 {headline} [{', '.join(markets)}] — ACTION REQUIRED NOW — {short}"[:180]

    items = "".join(
        f"<li style='margin:6px 0;font-size:15px'>"
        f"<span style='background:#000;color:#fff;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:700'>{_severity(p)}</span> "
        f"<b style='color:#fff'>{s['label']} ({s['name']})</b> — <span style='color:#fecaca'>{p}</span></li>"
        for s, p in new_problems)

    fix_btn = ""
    fix_txt = ""
    if fix_link:
        fix_btn = (
            f"<div style='margin-top:16px'>"
            f"<a href='{fix_link}' style='display:inline-block;background:#16a34a;color:#fff;font-size:17px;"
            f"font-weight:900;text-decoration:none;padding:14px 26px;border-radius:10px'>🔧 OPEN INCIDENT CONSOLE &amp; FIX NOW</a>"
            f"<div style='color:#fca5a5;font-size:12px;margin-top:6px'>One-time link · expires in 30 min · no login needed. "
            f"Opens the live console (charts + indexes) where you dispatch a scoped fixer agent.</div></div>")
        fix_txt = f"\n🔧 FIX NOW (one-time link, 30 min, no login):\n{fix_link}\n"

    html = (
        f"<div style='font-family:ui-sans-serif,system-ui,Arial;background:#120303;padding:0;margin:0'>"
        f"<div style='background:{banner_bg};border:3px solid #ef4444;border-radius:12px;padding:22px;margin:16px'>"
        f"<div style='font-size:30px;font-weight:900;color:#fff;letter-spacing:.5px'>{tag} — DO NOT IGNORE</div>"
        f"<div style='font-size:19px;font-weight:800;color:#fecaca;margin-top:6px'>{headline}</div>"
        f"<div style='font-size:13px;color:#fca5a5;margin-top:4px'>Detected {now()} · markets: {', '.join(markets)}</div>"
        f"<ul style='margin:16px 0 6px;padding-left:20px'>{items}</ul>"
        f"<div style='margin-top:14px;padding:12px;background:#000;border-radius:8px;color:#fca5a5;font-size:13px'>"
        f"⛔ A live market maker is failing its exchange liquidity rules. Left unattended this can trigger "
        f"MEXC's ST-warning / delisting. <b style='color:#fff'>Open the console and act immediately.</b></div>"
        f"{fix_btn}"
        f"</div>"
        + build_html(snaps, "Full current status")
        + "</div>")

    text = (
        f"{tag} — {headline} — DO NOT IGNORE\n"
        f"Detected {now()} · markets: {', '.join(markets)}\n\n"
        "PROBLEM(S):\n"
        + "\n".join(f"  [{_severity(p)}] {s['label']} ({s['name']}): {p}" for s, p in new_problems)
        + "\n\n⛔ A live market maker is failing its exchange rules — risk of MEXC ST-warning/delisting.\n"
        + fix_txt
        + "\n" + build_text(snaps, "Full current status"))
    return subject, html, text


def build_text(snaps, title):
    lines = [title, now(), ""]
    for s in snaps:
        lines.append(f"[{s['label']} / {s['name']}]")
        if not s.get("reachable"):
            lines.append(f"  UNREACHABLE: {s.get('err')}")
        else:
            for label, fn in ROWS:
                lines.append(f"  {label}: {fn(s)}")
        if s.get("problems"):
            lines.append("  PROBLEMS: " + "; ".join(s["problems"]))
        lines.append("")
    return "\n".join(lines)


# ---- main loop ----
def main():
    log(f"mm_monitor start — poll {POLL_SEC}s, summary {SUMMARY_SEC}s, email->{EMAIL['to']} (from {EMAIL['from']})")
    last_summary = 0.0
    alerted = {}   # problem-key -> last-notified epoch (for re-notify)
    active = set()  # currently-bad keys

    # startup ping so you know it's live
    snaps = [snapshot(t) for t in TARGETS]
    send_email("[MM-Monitor] started — status summary",
               build_html(snaps, "Market-Maker Monitor — STARTED"),
               build_text(snaps, "Market-Maker Monitor — STARTED"))
    last_summary = time.time()

    while True:
        try:
            snaps = [snapshot(t) for t in TARGETS]
            tnow = time.time()

            # ---- immediate problem alerts (edge-triggered + re-notify) ----
            cur = set()
            new_problems = []
            for s in snaps:
                for p in s.get("problems", []):
                    key = f"{s['name']}:{p.split('—')[0][:24]}"
                    cur.add(key)
                    if key not in active or (tnow - alerted.get(key, 0) >= RENOTIFY_SEC):
                        new_problems.append((s, p))
                        alerted[key] = tnow

            recovered = active - cur
            active.clear(); active.update(cur)

            if new_problems:
                fix_link = mint_incident_link(new_problems, snaps)
                send_email(*build_alert(new_problems, snaps, fix_link))

            if recovered:
                send_email("[MM-Monitor] ✅ recovered: " + ", ".join(sorted(recovered))[:120],
                           build_html(snaps, "Recovered — current status"),
                           build_text(snaps, "Recovered — current status"))

            # ---- hourly summary ----
            if tnow - last_summary >= SUMMARY_SEC:
                send_email("[MM-Monitor] hourly status — MEXC + CoinW",
                           build_html(snaps, "Market-Maker Hourly Status"),
                           build_text(snaps, "Market-Maker Hourly Status"))
                last_summary = tnow

            status = " | ".join(
                f"{s['name']} {'OK' if not s.get('problems') else 'BAD:' + str(len(s['problems']))}"
                for s in snaps)
            log(f"poll: {status}")
        except Exception as e:
            log(f"loop error: {type(e).__name__}: {str(e)[:200]}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
