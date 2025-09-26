from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import base64
import json
import os

import requests
from dotenv import load_dotenv


class AuthState:
    """Handles Schwab OAuth token persistence and refresh."""

    def __init__(self) -> None:
        load_dotenv()
        self.client_id = os.getenv("SCHWAB_CLIENT_ID") or ""
        self.client_secret = os.getenv("SCHWAB_CLIENT_SECRET") or ""
        token_path = os.getenv("SCHWAB_OAUTH_TOKEN_PATH") or "tokens/schwab_token.json"
        self.token_path = Path(token_path)
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Missing SCHWAB_CLIENT_ID/SECRET in environment")

        if not self.token_path.exists():
            json_env = os.getenv("SCHWAB_TOKEN_JSON")
            if json_env:
                parsed = json.loads(json_env)
                tokens_obj = parsed.get("tokens") if isinstance(parsed, dict) else None
                tokens = tokens_obj if isinstance(tokens_obj, dict) else (parsed if isinstance(parsed, dict) else {})
                if not tokens.get("access_token") or not tokens.get("refresh_token"):
                    raise RuntimeError("token JSON missing access_token/refresh_token")
                self._write_tokens(tokens)

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
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("ascii")).decode("ascii")
        headers = {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}
        body = {"grant_type": "refresh_token", "refresh_token": str(refresh)}
        resp = requests.post("https://api.schwabapi.com/v1/oauth/token", headers=headers, data=body, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"refresh_token failed: {resp.status_code} {resp.text}")
        new_tokens = resp.json()
        self._write_tokens(new_tokens)
        nt = new_tokens.get("access_token")
        if not nt:
            raise RuntimeError("Token refresh did not return access_token")
        return str(nt)


