Subject: URGENT — CoinW integration API (integrate1.exuno.io) is hung / unresponsive

Hi team,

The CoinW integration service at **integrate1.exuno.io** is currently
**unresponsive** — every endpoint hangs with no response. This has happened more
than once and it stalls our automated trading, which is a real risk (see Impact).

Measured just now (2026-07-29 ~07:24 UTC):

  integrate1.exuno.io  GET /health            -> NO RESPONSE (timed out >12s, http 000)
  integrate1.exuno.io  GET /balances          -> NO RESPONSE (timed out 30s)
  integrate1.exuno.io  GET /orders            -> NO RESPONSE (timed out 40s)
  integrate1.exuno.io  GET /orders?live=false -> NO RESPONSE (timed out 40s)

For comparison, at the same moment:

  integrate.exuno.io   GET /health  (MEXC twin)      -> 200 OK in 0.62s   (healthy)
  api.coinw.com        public ticker (UNP_USDT)      -> 200 OK in 0.65s   (healthy)

So the problem is isolated to the **CoinW integration service itself** — it is not
CoinW (their public API is fast), not the network, and not the MEXC integration
(integrate.exuno.io is fine). Even /health, which needs no token, does not
respond, so the process appears wedged rather than just slow on one route.

IMPACT (why this is urgent)
- Our bot and panels cannot read balances or place/cancel orders while this is down.
- Worse, when it hangs mid-operation the bot can be left with open positions it
  cannot see or manage. An unmanaged/one-sided book is a financial risk.
- Affected accounts: uid 26712446 (account 1) and uid 26712447 (account 2).

REQUESTS
1. Restart / recover integrate1.exuno.io now, and confirm /health is green.
2. Root-cause the hang. It looks like the process gets stuck — common causes:
   - an upstream CoinW call with no timeout blocking the worker/event loop,
   - the live order reconciliation on GET /orders (it reconciles from CoinW on
     every read) piling up under load,
   - a connection/promise leak that eventually wedges the process.
3. Add hard timeouts on every upstream CoinW call so a slow CoinW response can
   never hang the whole service (fail fast with a 502/504 instead of hanging).
4. Add a container healthcheck + auto-restart and alerting, so a wedge self-heals
   and you're paged before we notice.
5. Please make /orders?live=false genuinely cheap (no live upstream calls) and
   consider a short-TTL cache for /balances, so read endpoints stay responsive
   even when CoinW is slow.
6. A status/uptime page or a notification channel for this service would let us
   pause our bot safely instead of discovering the outage through failures.

Happy to share exact request timestamps/logs from our side. Please treat as
high priority — it directly affects live funds.

Thanks,
[your name]
