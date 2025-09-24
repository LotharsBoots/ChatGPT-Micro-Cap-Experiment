"""Schwab adapter using saved OAuth tokens (confidential client flow).

Environment variables expected:
- SCHWAB_CLIENT_ID
- SCHWAB_CLIENT_SECRET
- SCHWAB_ACCOUNT_ID
- SCHWAB_OAUTH_TOKEN_PATH (default: tokens/schwab_token.json)

Notes
- Access tokens are short lived (~30m). Refresh tokens work about a week.
- We auto-refresh once on 401 and retry the request exactly once.
- Order support: MARKET and LIMIT, DAY only (session NORMAL). Opening-auction
  timing is handled by the surrounding executor logic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import base64
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .adapter import BrokerAdapter


_API_BASE = "https://api.schwabapi.com"


class _AuthState:
    def __init__(self) -> None:
        load_dotenv()
        self.client_id = os.getenv("SCHWAB_CLIENT_ID") or ""
        self.client_secret = os.getenv("SCHWAB_CLIENT_SECRET") or ""
        token_path = os.getenv("SCHWAB_OAUTH_TOKEN_PATH") or "tokens/schwab_token.json"
        self.token_path = Path(token_path)
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Missing SCHWAB_CLIENT_ID/SECRET in environment")

        # If token file doesn't exist but JSON is provided via env (ECS Secrets), write it now
        if not self.token_path.exists():
            json_env = os.getenv("SCHWAB_TOKEN_JSON")
            if json_env:
                try:
                    parsed = json.loads(json_env)
                    tokens_obj = parsed.get("tokens") if isinstance(parsed, dict) else None
                    tokens = tokens_obj if isinstance(tokens_obj, dict) else (parsed if isinstance(parsed, dict) else {})
                    if not tokens.get("access_token") or not tokens.get("refresh_token"):
                        raise ValueError("token JSON missing access_token/refresh_token")
                    self._write_tokens(tokens)
                    print("[schwab] Wrote tokens from SCHWAB_TOKEN_JSON to", str(self.token_path))
                except Exception as exc:  # pragma: no cover - defensive
                    raise RuntimeError(f"Invalid SCHWAB_TOKEN_JSON: {exc}") from exc

    # ----- token file helpers -----
    def _read_tokens(self) -> Dict[str, Any]:
        if not self.token_path.exists():
            raise RuntimeError(f"Token file not found: {self.token_path}")
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Unable to read token file: {exc}") from exc
        # Support both {tokens:{...}} and flat {...}
        if isinstance(data, dict) and "tokens" in data and isinstance(data["tokens"], dict):
            return data["tokens"]
        return data if isinstance(data, dict) else {}

    def _write_tokens(self, tokens: Dict[str, Any]) -> None:
        # Persist in the same nested shape we use elsewhere
        out = {"tokens": tokens}
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(self.token_path.suffix + ".tmp")
        tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
        os.replace(tmp, self.token_path)

    # ----- public helpers -----
    def get_access_token(self) -> str:
        tokens = self._read_tokens()
        tok = tokens.get("access_token")
        if not tok:
            raise RuntimeError("access_token missing in token file")
        return str(tok)

    def refresh_and_get_access_token(self) -> str:
        tokens = self._read_tokens()
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise RuntimeError("refresh_token missing in token file; re-auth required")

        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("ascii")).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh),
        }
        resp = requests.post(f"{_API_BASE}/v1/oauth/token", headers=headers, data=body, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"refresh_token failed: {resp.status_code} {resp.text}")
        new_tokens = resp.json()
        # Persist the latest tokens
        self._write_tokens(new_tokens)
        nt = new_tokens.get("access_token")
        if not nt:
            raise RuntimeError("Token refresh did not return access_token")
        return str(nt)


class SchwabAdapter(BrokerAdapter):
    def __init__(self) -> None:
        self._auth = _AuthState()
        self._account_id = (os.getenv("SCHWAB_ACCOUNT_ID") or "").strip()
        if not self._account_id:
            raise RuntimeError("SCHWAB_ACCOUNT_ID not set in environment")

    # ----- internal request helper with one-time auto refresh -----
    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 json_body: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = f"{_API_BASE}{path}"
        token = self._auth.get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=30)
        if resp.status_code == 401:
            # try refresh once
            token = self._auth.refresh_and_get_access_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=30)
        return resp

    # ----- interface methods -----
    def get_account(self) -> Dict[str, Any]:
        # Use list endpoint and filter by accountNumber to avoid ID mismatches
        p = "/trader/v1/accounts"
        resp = self._request("GET", p, params={"fields": "positions"})
        if resp.status_code != 200:
            raise RuntimeError(f"Schwab get_account failed: {resp.status_code} {resp.text}")
        data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else []
        raw = {}
        if isinstance(data, list):
            for item in data:
                sa = (item or {}).get("securitiesAccount", {})
                if str(sa.get("accountNumber")) == self._account_id:
                    raw = item
                    break
        if not raw:
            raise RuntimeError("Account not found in list response")
        sa = raw.get("securitiesAccount", {}) if isinstance(raw, dict) else {}
        initial = sa.get("initialBalances", {}) if isinstance(sa, dict) else {}
        return {
            "account_id": sa.get("accountNumber"),
            "type": sa.get("type"),
            "cash": float(initial.get("cashBalance", 0.0) or 0.0),
            "buying_power": float(initial.get("buyingPower", 0.0) or 0.0),
            "raw": raw,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        p = "/trader/v1/accounts"
        resp = self._request("GET", p, params={"fields": "positions"})
        if resp.status_code != 200:
            raise RuntimeError(f"Schwab get_positions failed: {resp.status_code} {resp.text}")
        data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else []
        positions: List[Dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                sa = (item or {}).get("securitiesAccount", {})
                if str(sa.get("accountNumber")) == self._account_id:
                    positions = sa.get("positions", []) if isinstance(sa, dict) else []
                    break
        out: List[Dict[str, Any]] = []
        for pos in positions or []:
            ins = (pos or {}).get("instrument", {})
            symbol = ins.get("symbol")
            qty = int(float((pos or {}).get("longQuantity", 0) or 0))
            avg_px = float((pos or {}).get("averagePrice", 0.0) or 0.0)
            mv = float((pos or {}).get("marketValue", 0.0) or 0.0)
            out.append({
                "symbol": symbol,
                "qty": qty,
                "avg_entry_price": avg_px,
                "market_value": mv,
            })
        return out

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(order.get("ticker"))
        side = str(order.get("side")).upper()
        qty = int(order.get("quantity") or 0)
        otype = str(order.get("type", "market")).upper()
        tif = str(order.get("time_in_force", "DAY")).upper()
        limit_price = order.get("limit_price")

        if not symbol or side not in {"BUY", "SELL"} or qty <= 0:
            raise ValueError("Invalid order payload for Schwab")
        if otype == "LIMIT" and (limit_price is None or float(limit_price) <= 0):
            raise ValueError("Limit orders require a positive limit_price")
        if tif not in {"DAY"}:
            # keep mapping minimal for now; extend as needed
            tif = "DAY"

        # Schwab order payload (single equity leg)
        body: Dict[str, Any] = {
            "session": "NORMAL",
            "duration": tif,
            "orderType": "MARKET" if otype == "MARKET" else "LIMIT",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": side,
                    "quantity": qty,
                    "instrument": {"symbol": symbol, "assetType": "EQUITY"},
                }
            ],
        }
        if otype == "LIMIT":
            body["price"] = float(limit_price)

        p = f"/trader/v1/accounts/{self._account_id}/orders"
        resp = self._request("POST", p, json_body=body)
        if resp.status_code not in {200, 201}:
            raise RuntimeError(f"Schwab submit_order failed: {resp.status_code} {resp.text}")
        # Schwab often returns Location header with order id
        oid = None
        try:
            loc = resp.headers.get("Location")
            if loc:
                oid = loc.rsplit("/", 1)[-1]
        except Exception:
            pass
        try:
            data = resp.json()
            oid = oid or data.get("orderId") or data.get("id")
            status = data.get("status")
        except Exception:
            data = None
            status = None
        return {"order_id": oid, "status": status or "submitted", "raw": data}

    def list_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        p = f"/trader/v1/accounts/{self._account_id}/orders"
        params: Dict[str, Any] = {}
        resp = self._request("GET", p, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"Schwab list_orders failed: {resp.status_code} {resp.text}")
        arr = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else []
        out: List[Dict[str, Any]] = []
        if isinstance(arr, list):
            for o in arr:
                out.append({
                    "order_id": (o or {}).get("orderId") or (o or {}).get("id"),
                    "symbol": (((o or {}).get("orderLegCollection") or [{}])[0].get("instrument") or {}).get("symbol"),
                    "side": (((o or {}).get("orderLegCollection") or [{}])[0]).get("instruction"),
                    "qty": int(float((((o or {}).get("orderLegCollection") or [{}])[0]).get("quantity") or 0)),
                    "type": (o or {}).get("orderType"),
                    "time_in_force": (o or {}).get("duration"),
                    "status": (o or {}).get("status"),
                    "limit_price": float((o or {}).get("price") or 0) if (o or {}).get("price") else None,
                })
        return out

    def cancel_order(self, order_id: str) -> None:
        if not order_id:
            return
        p = f"/trader/v1/accounts/{self._account_id}/orders/{order_id}"
        resp = self._request("DELETE", p)
        if resp.status_code not in {200, 202, 204}:
            raise RuntimeError(f"Schwab cancel_order failed: {resp.status_code} {resp.text}")


