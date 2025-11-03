"""Post-open reconciliation: update order statuses and CSVs idempotently.

Runs after the opening window (e.g., ~09:40 ET). It reads Start Your Own\orders_queue.json,
loads the current Schwab orders, upgrades queue statuses (submitted->filled/canceled),
and applies filled executions to the CSVs using existing helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import json
import os
import sys

from dotenv import load_dotenv

from adapters.schwab import SchwabAdapter
from adapters.adapter import BrokerAdapter
from trading_script import (
    set_data_dir,
    load_latest_portfolio_state,
)
from script_portfolio_input import (
    log_manual_buy,
    log_manual_sell,
)


# Paths
ROOT = Path(__file__).resolve().parent
START_YOUR_OWN = ROOT / "Start Your Own"
ORDERS_PATH = START_YOUR_OWN / "orders_queue.json"


def _read_queue() -> List[Dict[str, Any]]:
    try:
        with ORDERS_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_queue(data: List[Dict[str, Any]]) -> None:
    tmp = ORDERS_PATH.with_suffix(ORDERS_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, ORDERS_PATH)


def _select_adapter() -> BrokerAdapter:
    return SchwabAdapter()


def _ensure_portfolio_csv() -> Path:
    START_YOUR_OWN.mkdir(parents=True, exist_ok=True)
    dst = START_YOUR_OWN / "chatgpt_portfolio_update.csv"
    if dst.exists():
        return dst
    # Fallback: create empty headers
    headers = [
        "Date",
        "Ticker",
        "Shares",
        "Buy Price",
        "Stop Loss",
        "Cost Basis",
        "Cash Balance",
        "Total Equity",
        "Action",
        "Current Price",
        "PnL",
        "Total Value",
    ]
    dst.write_text(",".join(headers) + "\n", encoding="utf-8")
    print("Initialized empty portfolio CSV at", str(dst))
    return dst


def _norm_status(value: object) -> str:
    return str(value or "").strip().lower()


def reconcile_once() -> None:
    load_dotenv()
    set_data_dir(START_YOUR_OWN)

    portfolio_csv = _ensure_portfolio_csv()
    portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))

    adapter = _select_adapter()
    queue = _read_queue()
    if not queue:
        print("No queued orders to reconcile.")
        return

    # Fetch all orders; build id->info
    orders = adapter.list_orders(status=None)
    by_id = {str(x.get("order_id")): x for x in orders}

    updated = False
    filled_count = 0

    for o in queue:
        oid = str(o.get("order_id")) if o.get("order_id") else None
        if not oid:
            continue
        info = by_id.get(oid)
        if not info:
            continue
        status = _norm_status(info.get("status"))
        if status in {"filled", "canceled", "cancelled"}:
            if _norm_status(o.get("status")) != status:
                o["status"] = status
                updated = True
        # Apply executions to CSVs for filled orders (idempotent)
        if status == "filled":
            side = str(o.get("side"))
            ticker = str(o.get("ticker"))
            qty = float(o.get("quantity") or 0)
            px = float(o.get("limit_price") or 0.0)
            if qty > 0:
                if side == "buy":
                    cash2, portfolio2 = log_manual_buy(
                        buy_price=px if px > 0 else 0.0,
                        shares=qty,
                        ticker=ticker,
                        stoploss=0.0,
                        cash=cash,
                        chatgpt_portfolio=portfolio if hasattr(portfolio, "copy") else portfolio,  # type: ignore[arg-type]
                        interactive=False,
                    )
                else:
                    cash2, portfolio2 = log_manual_sell(
                        sell_price=px if px > 0 else 0.0,
                        shares_sold=qty,
                        ticker=ticker,
                        cash=cash,
                        chatgpt_portfolio=portfolio if hasattr(portfolio, "copy") else portfolio,  # type: ignore[arg-type]
                        reason="AUTO RECONCILE",
                        interactive=False,
                    )
                # Update in-memory references so subsequent fills apply correctly
                cash, portfolio = cash2, portfolio2
                filled_count += 1

    if updated:
        _write_queue(queue)

    print(f"Reconcile complete. Updated queue: {updated}. Applied fills: {filled_count}.")


if __name__ == "__main__":
    try:
        reconcile_once()
    except KeyboardInterrupt:
        sys.exit(130)


