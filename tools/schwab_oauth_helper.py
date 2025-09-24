"""Minimal local OAuth helper for Charles Schwab.

- Redirect: uses SCHWAB_REDIRECT_URI from env; fallback http://127.0.0.1:5000/callback
- Starts local HTTP server on 127.0.0.1:5000 to capture /callback
- Exchanges code -> tokens immediately using Basic auth
- Saves tokens to tokens/schwab_token.json
- Fetches /accounts and prints discovered accountId values

Run:
  python tools/schwab_oauth_helper.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv


AUTH_BASE = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
ACCOUNTS_URL = "https://api.schwabapi.com/v1/accounts"


def _load_env_with_fallback() -> None:
    try:
        load_dotenv()
    except Exception:
        pass

    # Fallback: env.example.txt key=value lines
    example = Path(__file__).resolve().parent.parent / "env.example.txt"
    if example.exists():
        try:
            for line in example.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
                if not m:
                    continue
                k, v = m.group(1), m.group(2)
                os.environ.setdefault(k, v)
        except Exception:
            pass


class _CodeCatcher(BaseHTTPRequestHandler):
    server_version = "SchwabAuthHelper/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if code:
            self.wfile.write(b"<html><body><h3>Authorization received. You can close this window.</h3></body></html>")
        else:
            self.wfile.write(b"<html><body><h3>No code found. Please try again.</h3></body></html>")

        self.server.auth_code = code  # type: ignore[attr-defined]
        self.server.got_code.set()  # type: ignore[attr-defined]


def _start_server(host: str, port: int) -> Tuple[HTTPServer, threading.Thread, threading.Event]:
    evt = threading.Event()
    httpd: HTTPServer = HTTPServer((host, port), _CodeCatcher)
    httpd.got_code = evt  # type: ignore[attr-defined]
    t = threading.Thread(target=httpd.serve_forever, name="oauth-httpd", daemon=True)
    t.start()
    return httpd, t, evt


def _basic_header(client_id: str, client_secret: str) -> dict[str, str]:
    token = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _open(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        print("Open this URL in your browser:")
        print(url)


def main() -> None:
    _load_env_with_fallback()
    client_id = (os.environ.get("SCHWAB_CLIENT_ID") or input("SCHWAB_CLIENT_ID: ")).strip()
    client_secret = (os.environ.get("SCHWAB_CLIENT_SECRET") or input("SCHWAB_CLIENT_SECRET: ")).strip()
    redirect_uri = (os.environ.get("SCHWAB_REDIRECT_URI") or "http://127.0.0.1:5000/callback").strip()

    host, port = "127.0.0.1", 5000
    httpd, thread, evt = _start_server(host, port)
    print(f"Listening on http://{host}:{port}/callback ...")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    url = f"{AUTH_BASE}?{urlencode(params)}"
    _open(url)
    print("Waiting for authorization in your browser...")

    got = evt.wait(timeout=180)
    code: Optional[str] = getattr(httpd, "auth_code", None)  # type: ignore[attr-defined]
    httpd.shutdown()
    thread.join(timeout=2)

    if not got or not code:
        print("No authorization code received. Try again.")
        sys.exit(1)

    headers = _basic_header(client_id, client_secret)
    body = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    try:
        r = requests.post(TOKEN_URL, data=body, headers=headers, timeout=30)
    except Exception as e:
        print(f"Token request failed: {e}")
        sys.exit(1)
    if r.status_code != 200:
        print(f"Token exchange failed: {r.status_code} {r.text}")
        sys.exit(1)
    tokens = r.json()

    out = Path(os.environ.get("SCHWAB_OAUTH_TOKEN_PATH") or "tokens/schwab_token.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    print(f"Tokens saved to {out}")

    access = tokens.get("access_token")
    if access:
        try:
            ar = requests.get(ACCOUNTS_URL, headers={"Authorization": f"Bearer {access}", "Accept": "application/json"}, timeout=20)
            if ar.status_code == 200:
                data = ar.json()
                accounts = []
                if isinstance(data, dict) and isinstance(data.get("accounts"), list):
                    accounts = data["accounts"]
                if accounts:
                    print("Discovered accountId values:")
                    for it in accounts:
                        aid = it.get("accountId") or it.get("id")
                        if aid:
                            print(f" - {aid}")
                else:
                    print("Accounts call succeeded, but structure not recognized.")
            else:
                print(f"Accounts request failed: {ar.status_code} {ar.text}")
        except Exception as e:
            print(f"Accounts request error: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)


