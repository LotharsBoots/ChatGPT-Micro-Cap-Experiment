from __future__ import annotations

from typing import Any, Dict, List

from .http_client import request_with_refresh


def get_account(account_id: str) -> Dict[str, Any]:
    resp = request_with_refresh("GET", "/trader/v1/accounts", params={"fields": "positions"})
    if resp.status_code != 200:
        raise RuntimeError(f"Schwab get_account failed: {resp.status_code} {resp.text}")
    data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else []
    raw = {}
    if isinstance(data, list):
        for item in data:
            sa = (item or {}).get("securitiesAccount", {})
            if str(sa.get("accountNumber")) == account_id:
                raw = item
                break
    if not raw:
        raise RuntimeError("Account not found in list response")

    sa = raw.get("securitiesAccount", {}) if isinstance(raw, dict) else {}
    current = sa.get("currentBalances", {}) if isinstance(sa, dict) else {}
    initial = sa.get("initialBalances", {}) if isinstance(sa, dict) else {}

    # Prefer settled cash from currentBalances (late-day deposits show here);
    # fall back to cashAvailableForWithdrawal, then initialBalances.cashBalance.
    cash_val = 0.0
    try:
        cash_val = float(current.get("cashBalance", 0.0) or 0.0)
    except Exception:
        cash_val = 0.0
    if cash_val == 0.0:
        try:
            cash_val = float(current.get("cashAvailableForWithdrawal", 0.0) or 0.0)
        except Exception:
            pass
    if cash_val == 0.0:
        try:
            cash_val = float(initial.get("cashBalance", 0.0) or 0.0)
        except Exception:
            pass

    return {
        "account_id": sa.get("accountNumber"),
        "type": sa.get("type"),
        "cash": cash_val,
        "buying_power": float(initial.get("buyingPower", 0.0) or 0.0),
        "raw": raw,
    }


def get_positions(account_id: str) -> List[Dict[str, Any]]:
    resp = request_with_refresh("GET", "/trader/v1/accounts", params={"fields": "positions"})
    if resp.status_code != 200:
        raise RuntimeError(f"Schwab get_positions failed: {resp.status_code} {resp.text}")
    data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else []
    positions: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            sa = (item or {}).get("securitiesAccount", {})
            if str(sa.get("accountNumber")) == account_id:
                positions = sa.get("positions", []) if isinstance(sa, dict) else []
                break
    out: List[Dict[str, Any]] = []
    for pos in positions or []:
        ins = (pos or {}).get("instrument", {})
        out.append({
            "symbol": ins.get("symbol"),
            "qty": int(float((pos or {}).get("longQuantity", 0) or 0)),
            "avg_entry_price": float((pos or {}).get("averagePrice", 0.0) or 0.0),
            "market_value": float((pos or {}).get("marketValue", 0.0) or 0.0),
        })
    return out


