"""Morning executor: submit queued orders at the open and update CSVs.

Reads Start Your Own\orders_queue.json, maps MOO/LOO to Alpaca OPG orders,
submits them, polls for fills for a short window, then writes executions to
Start Your Own CSVs using existing helpers from trading_script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import json
import os
import sys
import time

from dotenv import load_dotenv
import pytz
from datetime import datetime

from adapters.alpaca import AlpacaAdapter
from adapters.adapter import BrokerAdapter
from trading_script import (
    set_data_dir,
    load_latest_portfolio_state,
    log_manual_buy,
    log_manual_sell,
)


def _root() -> Path:
    return Path(__file__).resolve().parent


def _syo() -> Path:
    return _root() / "Start Your Own"


def _orders_path() -> Path:
    return _syo() / "orders_queue.json"


def _read_queue() -> List[Dict[str, Any]]:
    p = _orders_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_queue(data: List[Dict[str, Any]]) -> None:
    p = _orders_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, p)


def _select_adapter() -> BrokerAdapter:
    # For Phase 1 we only support Alpaca; broker switch comes later via config
    return AlpacaAdapter()


def _submit_one(adapter: BrokerAdapter, o: Dict[str, Any]) -> Dict[str, Any]:
    tif = "opg"  # opening auction
    if o.get("order_type") == "MOO":
        order = {
            "ticker": o["ticker"],
            "side": o["side"],
            "quantity": int(o["quantity"]),
            "type": "market",
            "time_in_force": tif,
            "limit_price": None,
        }
    else:  # LOO
        order = {
            "ticker": o["ticker"],
            "side": o["side"],
            "quantity": int(o["quantity"]),
            "type": "limit",
            "time_in_force": tif,
            "limit_price": float(o["limit_price"]),
        }
    return adapter.submit_order(order)


def _is_opg_window_now() -> bool:
    """Return True if current US/Eastern time is within Alpaca OPG window.

    Alpaca accepts OPG orders between 7:00pm and 9:28am ET.
    We treat the window as:
      - (19:00:00 <= time < 24:00:00) OR (00:00:00 <= time <= 09:28:00)
    """
    tz = pytz.timezone("US/Eastern")
    now_et = datetime.now(tz)
    h, m = now_et.hour, now_et.minute
    evening_ok = (h >= 19)  # 7pm–midnight
    morning_ok = (h < 9) or (h == 9 and m <= 28)  # midnight–9:28am
    return bool(evening_ok or morning_ok)


def main() -> None:
    load_dotenv()
    set_data_dir(_syo())

    # Load current portfolio/cash for logging updates
    portfolio_csv = _syo() / "chatgpt_portfolio_update.csv"
    portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))

    # Guard: skip cleanly if outside OPG window to avoid broker errors
    if not _is_opg_window_now():
        print("Outside OPG window (ET 7:00pm–9:28am). Skipping submit and keeping orders queued.")
        print("Schedule this script around 8:25am CT for automatic submission.")
        return

    adapter = _select_adapter()
    queue = _read_queue()
    if not queue:
        print("No queued orders.")
        return

    print(f"Submitting {len(queue)} queued orders (OPG)...")
    # Submit all orders; store order_ids back into queue for traceability
    for o in queue:
        status = str(o.get("status") or "").strip().lower()
        # Skip anything already acknowledged or final to avoid duplicates
        if status in {"accepted", "new", "submitted", "filled", "cancelled", "canceled"}:
            continue
        try:
            resp = _submit_one(adapter, o)
            o["order_id"] = resp.get("order_id")
            o["status"] = resp.get("status") or "submitted"
            print(f"Submitted {o['side']} {o['ticker']} x{o['quantity']} -> {o['order_id']}")
        except Exception as e:
            o["status"] = f"error: {e}"
    _write_queue(queue)

    # Poll for fills for ~10 minutes (short loop)
    end_time = time.time() + 10 * 60
    known_ids = {str(o.get("order_id")) for o in queue if o.get("order_id")}
    while time.time() < end_time and known_ids:
        time.sleep(10)
        orders = adapter.list_orders(status=None)
        by_id = {str(x.get("order_id")): x for x in orders}
        all_filled = True
        for o in queue:
            oid = str(o.get("order_id")) if o.get("order_id") else None
            if not oid:
                continue
            info = by_id.get(oid)
            if not info:
                all_filled = False
                continue
            status = str(info.get("status", "")).lower()
            if status in {"filled", "canceled", "cancelled"}:
                o["status"] = status
            else:
                all_filled = False
        _write_queue(queue)
        if all_filled:
            break

    # Update CSVs based on final states (simple approach):
    # For buys: log_manual_buy with exec price approximated by limit/market
    # For sells: log_manual_sell similarly. We do not attempt price discovery here.
    for o in queue:
        status = str(o.get("status", "")).lower()
        if status != "filled":
            continue
        side = str(o.get("side"))
        ticker = str(o.get("ticker"))
        qty = float(o.get("quantity") or 0)
        if qty <= 0:
            continue
        # Use limit price if provided; otherwise store as 0 and let later pricing show actual close/open
        px = float(o.get("limit_price") or 0.0)
        if side == "buy":
            cash, portfolio = log_manual_buy(
                buy_price=px if px > 0 else 0.0,
                shares=qty,
                ticker=ticker,
                stoploss=0.0,
                cash=cash,
                chatgpt_portfolio=portfolio if hasattr(portfolio, "copy") else portfolio,  # type: ignore[arg-type]
                interactive=False,
            )
        else:
            cash, portfolio = log_manual_sell(
                sell_price=px if px > 0 else 0.0,
                shares_sold=qty,
                ticker=ticker,
                cash=cash,
                chatgpt_portfolio=portfolio if hasattr(portfolio, "copy") else portfolio,  # type: ignore[arg-type]
                reason="AUTO EXECUTOR",
                interactive=False,
            )

    print("Executor complete. CSVs updated in 'Start Your Own'.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)


