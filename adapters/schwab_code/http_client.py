from __future__ import annotations

from typing import Dict, Optional
import os

import requests

from .auth_tokens import AuthState


API_BASE = "https://api.schwabapi.com"


def request_with_refresh(method: str, path: str, *, params: Optional[Dict] = None, json_body: Optional[Dict] = None) -> requests.Response:
    url = f"{API_BASE}{path}"
    auth = AuthState()
    token = auth.get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=30)
    if resp.status_code == 401:
        disable_refresh = str(os.getenv("SCHWAB_DISABLE_REFRESH") or "").strip().lower() in {"1", "true", "yes", "on"}
        if disable_refresh:
            try:
                print("[schwab] refresh disabled; using access_token only")
            except Exception:
                pass
            return resp
        token = auth.refresh_and_get_access_token()
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=30)
    return resp


