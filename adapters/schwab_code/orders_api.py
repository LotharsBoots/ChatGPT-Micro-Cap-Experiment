from __future__ import annotations

from typing import Any, Dict, List, Optional
import os
import json

from .http_client import request_with_refresh
from .time_windows import orders_time_window_iso


def submit_order(account_id: str, order: Dict[str, Any]) -> Dict[str, Any]:
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
        tif = "DAY"

    body: Dict[str, Any] = {
        "session": "NORMAL",
        "duration": tif,
        "orderType": "MARKET" if otype == "MARKET" else "LIMIT",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {"instruction": side, "quantity": qty, "instrument": {"symbol": symbol, "assetType": "EQUITY"}}
        ],
    }
    if otype == "LIMIT":
        body["price"] = float(limit_price)

    p = f"/trader/v1/accounts/{account_id}/orders"
    # Optional payload logging gated by env (no secrets)
    try:
        if str(os.environ.get("LOG_ORDER_PAYLOAD") or "").strip().lower() in {"1", "true", "yes", "on"}:
            print("[order_payload] " + json.dumps(body, separators=(",", ":")))
    except Exception:
        pass
    resp = request_with_refresh("POST", p, json_body=body)
    if resp.status_code not in {200, 201}:
        raise RuntimeError(f"Schwab submit_order failed: {resp.status_code} {resp.text}")
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


def list_orders(account_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    p = f"/trader/v1/accounts/{account_id}/orders"
    start_iso, end_iso = orders_time_window_iso(days_back=2)
    params: Dict[str, Any] = {"fromEnteredTime": start_iso, "toEnteredTime": end_iso}
    resp = request_with_refresh("GET", p, params=params)
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


def cancel_order(account_id: str, order_id: str) -> None:
    if not order_id:
        return
    p = f"/trader/v1/accounts/{account_id}/orders/{order_id}"
    resp = request_with_refresh("DELETE", p)
    if resp.status_code not in {200, 202, 204}:
        raise RuntimeError(f"Schwab cancel_order failed: {resp.status_code} {resp.text}")


