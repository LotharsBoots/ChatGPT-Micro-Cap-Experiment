from __future__ import annotations

from typing import Any, Dict, List, Optional
import os

from ..adapter import BrokerAdapter
from .accounts_api import get_account as _get_account, get_positions as _get_positions
from .orders_api import submit_order as _submit_order, list_orders as _list_orders, cancel_order as _cancel_order


class SchwabAdapter(BrokerAdapter):
    def __init__(self) -> None:
        self._account_id = (os.getenv("SCHWAB_ACCOUNT_ID") or "").strip()
        if not self._account_id:
            raise RuntimeError("SCHWAB_ACCOUNT_ID not set in environment")

    def get_account(self) -> Dict[str, Any]:
        return _get_account(self._account_id)

    def get_positions(self) -> List[Dict[str, Any]]:
        return _get_positions(self._account_id)

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return _submit_order(self._account_id, order)

    def list_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return _list_orders(self._account_id, status=status)

    def cancel_order(self, order_id: str) -> None:
        _cancel_order(self._account_id, order_id)


