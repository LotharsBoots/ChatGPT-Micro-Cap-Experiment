"""Morning executor: submit queued orders at the opening and update CSVs.

Reads Start Your Own\orders_queue.json, submits MARKET/LIMIT (DAY) orders
in a tight 09:30:00 ET window (configurable), polls fills for ~10 minutes,
then writes executions to Start Your Own CSVs using trading_script helpers.
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
from datetime import datetime, timedelta

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


def _write_status(payload: Dict[str, Any]) -> None:
    """Persist a small execution status JSON under Start Your Own/status/.

    File name is timestamped so each run is distinct and will be synced to S3
    by the surrounding task command.
    """
    try:
        status_dir = START_YOUR_OWN / "status"
        status_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%SZ")
        path = status_dir / f"executor_{ts}.json"
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        # Best-effort only; never fail the run due to status write
        pass


def _read_queue() -> List[Dict[str, Any]]:
    p = ORDERS_PATH
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_queue(data: List[Dict[str, Any]]) -> None:
    p = ORDERS_PATH
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, p)


def _ensure_portfolio_csv() -> Path:
    """Ensure Start Your Own/chatgpt_portfolio_update.csv exists inside the container.

    If missing, create an empty CSV with standard headers. Day-One will
    later overwrite it with a fresh snapshot from Schwab.
    """
    syo = START_YOUR_OWN
    syo.mkdir(parents=True, exist_ok=True)
    dst = syo / "chatgpt_portfolio_update.csv"
    if dst.exists():
        return dst
    # If we couldn't seed, create an empty CSV with expected headers so
    # downstream code treats it as empty portfolio and initializes cash.
    try:
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
        dst.write_text(
            ",".join(headers) + "\n",
            encoding="utf-8",
        )
        print("Initialized empty portfolio CSV at", str(dst))
    except Exception:
        pass
    return dst


def _select_adapter() -> BrokerAdapter:
    # Switch to Schwab-only mode per configuration
    return SchwabAdapter()


def _submit_one(adapter: BrokerAdapter, o: Dict[str, Any]) -> Dict[str, Any]:
    # For Schwab: submit DAY orders during the opening window
    tif = "day"
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


def _opening_submit_window_bounds() -> tuple[datetime, datetime]:
    """Return (start_et, end_et) window around 09:30:00 ET using env seconds."""
    tz = pytz.timezone("US/Eastern")
    now_et = datetime.now(tz)
    try:
        start_off = float(os.environ.get("OPENING_SUBMIT_WINDOW_START_SEC", "-5"))
    except Exception:
        start_off = -5.0
    try:
        end_off = float(os.environ.get("OPENING_SUBMIT_WINDOW_END_SEC", "5"))
    except Exception:
        end_off = 5.0
    target = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    start = target + timedelta(seconds=start_off)
    end = target + timedelta(seconds=end_off)
    return start, end


def _is_opening_window_now() -> bool:
    tz = pytz.timezone("US/Eastern")
    now_et = datetime.now(tz)
    start, end = _opening_submit_window_bounds()
    return start <= now_et <= end


def main() -> None:
    load_dotenv()
    set_data_dir(START_YOUR_OWN)

    # Load current portfolio/cash for logging updates
    portfolio_csv = _ensure_portfolio_csv()
    portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))

    # Guard: precise opening submission window for Schwab timed flow
    sub_mode = (os.environ.get("SUBMISSION_MODE") or "timed").strip().lower()
    window_start, window_end = _opening_submit_window_bounds()
    status_base: Dict[str, Any] = {
        "started_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_start_et": window_start.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end_et": window_end.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": sub_mode,
    }
    # Optional: wait until the window opens (never submit early)
    wait_for_window = (os.environ.get("WAIT_FOR_WINDOW") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if sub_mode == "timed" and wait_for_window:
        tz = pytz.timezone("US/Eastern")
        now_et = datetime.now(tz)
        if now_et < window_start:
            # Block until the opening window begins
            time.sleep((window_start - now_et).total_seconds())

    if sub_mode == "timed":
        if not _is_opening_window_now():
            print("Outside opening submission window. Skipping submit and keeping orders queued.")
            print(f"Window (ET): {window_start.strftime('%H:%M:%S')}–{window_end.strftime('%H:%M:%S')} around 09:30:00")
            _write_status({**status_base, "result": "skipped_outside_window"})
            return
    else:
        if not _is_opening_window_now():
            print("Outside opening submission window. Skipping.")
            _write_status({**status_base, "result": "skipped_outside_window"})
            return

    adapter = _select_adapter()
    queue = _read_queue()
    if not queue:
        print("No queued orders.")
        _write_status({**status_base, "result": "no_queue"})
        return

    print(f"Submitting {len(queue)} queued orders (opening window)...")
    # Submit all orders; store order_ids back into queue for traceability
    # Pre-warm auth once (avoid token refresh inside tight window)
    try:
        _ = adapter.get_account()
    except Exception:
        pass

    submitted = 0
    errors = 0
    for o in queue:
        status = str(o.get("status") or "").strip().lower()
        # Skip anything already acknowledged or final to avoid duplicates
        if status in {"accepted", "new", "submitted", "filled", "cancelled", "canceled"}:
            continue
        try:
            # Retry transient errors quickly within window
            backoff = 0.2
            attempts = 0
            while True:
                attempts += 1
                try:
                    resp = _submit_one(adapter, o)
                    break
                except Exception as e:
                    if attempts >= 4:
                        raise
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 1.0)
            o["order_id"] = resp.get("order_id")
            o["status"] = resp.get("status") or "submitted"
            print(f"Submitted {o['side']} {o['ticker']} x{o['quantity']} -> {o['order_id']}")
            submitted += 1
        except Exception as e:
            o["status"] = f"error: {e}"
            errors += 1
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
    _write_status({
        **status_base,
        "result": "completed",
        "queue_len": len(queue),
        "submitted": submitted,
        "errors": errors,
    })


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)


