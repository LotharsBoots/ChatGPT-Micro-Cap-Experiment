"""Alpaca adapter (paper/live) using environment variables.

Requires the following environment variables (already used elsewhere in the repo):
  - ALPACA_BASE_URL (e.g., https://paper-api.alpaca.markets)
  - ALPACA_KEY_ID
  - ALPACA_SECRET_KEY

This adapter intentionally returns simple dicts for clarity and easy logging.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import os

from dotenv import load_dotenv

try:
    from alpaca_trade_api import REST
except Exception as exc:  # pragma: no cover - optional import guard
    REST = None  # type: ignore


class AlpacaAdapter:
    def __init__(self) -> None:
        load_dotenv()
        base_url = os.getenv("ALPACA_BASE_URL")
        key = os.getenv("ALPACA_KEY_ID")
        secret = os.getenv("ALPACA_SECRET_KEY")
        if not all([base_url, key, secret]):
            raise RuntimeError("Missing Alpaca env vars (ALPACA_BASE_URL/KEY_ID/SECRET_KEY)")
        if REST is None:
            raise RuntimeError("alpaca-trade-api is not installed. Run: pip install alpaca-trade-api")
        self._api = REST(key, secret, base_url)

    def get_account(self) -> Dict[str, Any]:
        a = self._api.get_account()
        return {
            "id": getattr(a, "id", None),
            "status": getattr(a, "status", None),
            "currency": getattr(a, "currency", None),
            "cash": float(getattr(a, "cash", 0.0)),
            "buying_power": float(getattr(a, "buying_power", 0.0)),
            "paper": str(getattr(a, "account_number", "")).startswith("PA"),
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for p in self._api.list_positions() or []:
            out.append({
                "symbol": getattr(p, "symbol", None),
                "qty": int(float(getattr(p, "qty", 0))),
                "avg_entry_price": float(getattr(p, "avg_entry_price", 0.0)),
                "market_value": float(getattr(p, "market_value", 0.0)),
            })
        return out

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(order.get("ticker"))
        side = str(order.get("side")).lower()
        qty = int(order.get("quantity") or 0)
        otype = str(order.get("type", "market")).lower()
        tif = str(order.get("time_in_force", "day")).lower()
        limit_price = order.get("limit_price")

        if not symbol or side not in {"buy", "sell"} or qty <= 0:
            raise ValueError("Invalid order payload for Alpaca")
        if otype == "limit" and (limit_price is None or float(limit_price) <= 0):
            raise ValueError("Limit orders require a positive limit_price")

        resp = self._api.submit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            type=otype,
            time_in_force=tif,
            limit_price=float(limit_price) if limit_price is not None else None,
        )
        return {"order_id": getattr(resp, "id", None), "status": getattr(resp, "status", None)}

    def list_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        orders = self._api.list_orders(status=status) if status else self._api.list_orders()
        out: List[Dict[str, Any]] = []
        for o in orders or []:
            out.append({
                "order_id": getattr(o, "id", None),
                "symbol": getattr(o, "symbol", None),
                "side": getattr(o, "side", None),
                "qty": int(float(getattr(o, "qty", 0))),
                "type": getattr(o, "type", None),
                "time_in_force": getattr(o, "time_in_force", None),
                "status": getattr(o, "status", None),
                "filled_qty": int(float(getattr(o, "filled_qty", 0))),
                "limit_price": float(getattr(o, "limit_price", 0.0)) if getattr(o, "limit_price", None) else None,
            })
        return out

    def cancel_order(self, order_id: str) -> None:
        self._api.cancel_order(order_id)


