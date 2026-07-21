"""Configure nginx-proxy-manager for market.exuno.io via its admin API.

Creates (idempotently):
  * an Access List with HTTP Basic Auth (username/password),
  * a Proxy Host  market.exuno.io -> exuno-market-panel:8787,
  * a Let's Encrypt certificate, with SSL forced and the access list attached.

Reads credentials from the environment so nothing secret is committed:
  NPM_USER  (default info@botify.trade)  -- NPM admin login = basic-auth user
  NPM_PASS                               -- NPM admin password = basic-auth pass
  NPM_URL   (default http://127.0.0.1:81)
  DOMAIN    (default market.exuno.io)
  UPSTREAM_HOST (default exuno-market-panel)  UPSTREAM_PORT (default 8787)
"""
import json
import os
import urllib.request

NPM_URL = os.environ.get("NPM_URL", "http://127.0.0.1:81")
USER = os.environ.get("NPM_USER", "info@botify.trade")
PASS = os.environ["NPM_PASS"]
DOMAIN = os.environ.get("DOMAIN", "market.exuno.io")
UP_HOST = os.environ.get("UPSTREAM_HOST", "exuno-market-panel")
UP_PORT = int(os.environ.get("UPSTREAM_PORT", "8787"))


def call(method, path, token=None, body=None):
    req = urllib.request.Request(
        f"{NPM_URL}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"NPM {method} {path} failed: {e.code} {e.read().decode()}")


def main():
    token = call("POST", "/api/tokens", body={"identity": USER, "secret": PASS})["token"]
    print("Authenticated to NPM.")

    # --- Access List (basic auth) -----------------------------------------
    lists = call("GET", "/api/nginx/access-lists?expand=items", token) or []
    al = next((a for a in lists if a["name"] == "market-panel"), None)
    if al:
        print(f"Access list exists (id={al['id']}).")
    else:
        al = call("POST", "/api/nginx/access-lists", token, {
            "name": "market-panel",
            "satisfy_any": False,
            "pass_auth": False,
            "items": [{"username": USER, "password": PASS}],
            "clients": [],
        })
        print(f"Created access list (id={al['id']}).")
    access_list_id = al["id"]

    # --- Proxy Host --------------------------------------------------------
    hosts = call("GET", "/api/nginx/proxy-hosts", token) or []
    ph = next((h for h in hosts if DOMAIN in h.get("domain_names", [])), None)
    payload = {
        "domain_names": [DOMAIN],
        "forward_scheme": "http",
        "forward_host": UP_HOST,
        "forward_port": UP_PORT,
        "access_list_id": access_list_id,
        "caching_enabled": False,
        "block_exploits": True,
        "allow_websocket_upgrade": True,
        "http2_support": True,
        "hsts_enabled": False,
        "ssl_forced": True,
        "meta": {"letsencrypt_agree": True, "dns_challenge": False},
        "advanced_config": "",
        "locations": [],
    }
    if ph:
        print(f"Proxy host exists (id={ph['id']}), updating.")
        payload["certificate_id"] = ph.get("certificate_id", 0) or "new"
        ph = call("PUT", f"/api/nginx/proxy-hosts/{ph['id']}", token, payload)
    else:
        payload["certificate_id"] = "new"   # request a fresh Let's Encrypt cert
        ph = call("POST", "/api/nginx/proxy-hosts", token, payload)
        print(f"Created proxy host (id={ph['id']}).")

    print(f"OK: https://{DOMAIN} -> {UP_HOST}:{UP_PORT}, basic-auth + SSL forced.")


if __name__ == "__main__":
    main()
