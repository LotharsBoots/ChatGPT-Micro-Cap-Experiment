from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import base64
import json
import os

import requests
from dotenv import load_dotenv


def _normalize_secret(value: str) -> str:
    """Trim whitespace and surrounding quotes that often sneak into secrets."""
    if value is None:
        return ""
    v = str(value).strip()
    # Strip single or double quotes if the whole value is quoted
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    # Remove stray CR/LF characters
    return v.replace("\r", "").replace("\n", "")


class AuthState:
    """Handles Schwab OAuth token persistence and refresh."""

    def __init__(self) -> None:
        load_dotenv()
        self.client_id = _normalize_secret(os.getenv("SCHWAB_CLIENT_ID") or "")
        self.client_secret = _normalize_secret(os.getenv("SCHWAB_CLIENT_SECRET") or "")
        token_path = os.getenv("SCHWAB_OAUTH_TOKEN_PATH") or "tokens/schwab_token.json"
        self.token_path = Path(token_path)
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Missing SCHWAB_CLIENT_ID/SECRET in environment")

        # Always prefer SCHWAB_TOKEN_JSON when present; write it to the token file
        json_env = os.getenv("SCHWAB_TOKEN_JSON")
        if json_env:
            raw = str(json_env).strip()
            if raw.startswith("base64:"):
                try:
                    b64 = raw.split(":", 1)[1]
                    raw = base64.b64decode(b64).decode("utf-8")
                except Exception as exc:
                    raise RuntimeError(f"SCHWAB_TOKEN_JSON base64 decode failed: {exc}") from exc
            try:
                parsed = json.loads(raw)
            except Exception:
                sanitized = raw.replace("\r", "").replace("\n", "")
                parsed = json.loads(sanitized)
            tokens_obj = parsed.get("tokens") if isinstance(parsed, dict) else None
            tokens = tokens_obj if isinstance(tokens_obj, dict) else (parsed if isinstance(parsed, dict) else {})
            if not tokens.get("access_token") or not tokens.get("refresh_token"):
                raise RuntimeError("token JSON missing access_token/refresh_token")
            self._write_tokens(tokens)
        elif not self.token_path.exists():
            raise RuntimeError("Token file not found and SCHWAB_TOKEN_JSON not provided")

    # ----- token file helpers -----
    def _read_tokens(self) -> Dict[str, Any]:
        if not self.token_path.exists():
            raise RuntimeError(f"Token file not found: {self.token_path}")
        data = json.loads(self.token_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "tokens" in data and isinstance(data["tokens"], dict):
            return data["tokens"]
        return data if isinstance(data, dict) else {}

    def _write_tokens(self, tokens: Dict[str, Any]) -> None:
        out = {"tokens": tokens}
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(self.token_path.suffix + ".tmp")
        tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
        os.replace(tmp, self.token_path)

    # ----- public helpers -----
    def get_access_token(self) -> str:
        tok = self._read_tokens().get("access_token")
        if not tok:
            raise RuntimeError("access_token missing in token file")
        return str(tok)

    def refresh_and_get_access_token(self) -> str:
        tokens = self._read_tokens()
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise RuntimeError("refresh_token missing in token file; re-auth required")
        cid = _normalize_secret(self.client_id)
        csec = _normalize_secret(self.client_secret)
        token_url = "https://api.schwabapi.com/v1/oauth/token"

        # Try 1: Basic authorization header (confidential client)
        route_used = None
        basic = base64.b64encode(f"{cid}:{csec}".encode("ascii")).decode("ascii")
        headers = {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}
        body = {"grant_type": "refresh_token", "refresh_token": str(refresh)}
        resp = requests.post(token_url, headers=headers, data=body, timeout=30)
        if resp.status_code == 200:
            route_used = "basic"
        else:
            # Try 2: Credentials in body plus redirect_uri (some environments require this)
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            redirect_uri = _normalize_secret(os.getenv("SCHWAB_REDIRECT_URI") or "")
            body = {
                "grant_type": "refresh_token",
                "refresh_token": str(refresh),
                "client_id": cid,
                "client_secret": csec,
            }
            if redirect_uri:
                body["redirect_uri"] = redirect_uri
            resp = requests.post(token_url, headers=headers, data=body, timeout=30)
            if resp.status_code == 200:
                route_used = "body"
            else:
                # Try 3: PKCE-style: client_id only (no secret)
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                body = {"grant_type": "refresh_token", "refresh_token": str(refresh), "client_id": cid}
                if redirect_uri:
                    body["redirect_uri"] = redirect_uri
                resp = requests.post(token_url, headers=headers, data=body, timeout=30)
                if resp.status_code == 200:
                    route_used = "pkce"

        if resp.status_code != 200:
            raise RuntimeError(f"refresh_token failed: {resp.status_code} {resp.text}")

        new_tokens = resp.json()
        self._write_tokens(new_tokens)
        nt = new_tokens.get("access_token")
        if not nt:
            raise RuntimeError("Token refresh did not return access_token")
        # Mask client id in logs: first/last 4
        try:
            masked = (cid[:4] + "..." + cid[-4:]) if cid else ""
            print(f"[schwab] refresh route={route_used} (ci={masked})")
        except Exception:
            pass
        return str(nt)


