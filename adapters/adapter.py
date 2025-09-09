"""Broker adapter interface for submitting and managing orders.

This module defines a very small, explicit API surface so we can plug in
multiple brokers (Alpaca, Schwab, IBKR) without changing business logic.

All functions are synchronous and return plain Python dicts for simplicity.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class BrokerAdapter:
    """Minimal interface every broker adapter must implement."""

    def get_account(self) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def get_positions(self) -> List[Dict[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - interface
        """Submit a single order.

        Expected keys in `order`:
          - ticker: str (symbol)
          - side: str ("buy" | "sell")
          - quantity: int (>0)
          - type: str ("market" | "limit")
          - time_in_force: str (e.g., "opg", "day")
          - limit_price: float | None (required for type=="limit")
        """
        raise NotImplementedError

    def list_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


