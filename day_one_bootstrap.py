"""Day-1 Autopilot: one-time portfolio seeding in AWS (no prompts).

Behavior (idempotent):
- Sync is handled by the ECS task command surrounding this script.
- Uses env STARTING_CASH. QuickStart is implicitly ON (no flag).
- If the portfolio CSV already contains any position rows, exits without changes.
- Otherwise runs auto_trade_once to allocate and writes CSVs.

Inputs (environment):
- STARTING_CASH (required numeric)
- BUCKET (provided on tasks; not read here)

Outputs (in Start Your Own/):
- chatgpt_portfolio_update.csv updated
- chatgpt_trade_log.csv appended if buys occurred
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import sys

import pandas as pd

from script_data_paths import set_data_dir
from script_autotrade import auto_trade_once
from script_portfolio_engine import price_and_update_csv
from trading_script import load_latest_portfolio_state


def _syo() -> Path:
    return Path(__file__).resolve().parent / "Start Your Own"


def _positions_count(portfolio: Any) -> int:
    if isinstance(portfolio, pd.DataFrame):
        df = portfolio.copy()
        return int(len(df.index))
    if isinstance(portfolio, list):
        return sum(1 for _ in portfolio)
    try:
        df = pd.DataFrame(portfolio)  # type: ignore[arg-type]
        return int(len(df.index))
    except Exception:
        return 0


def main() -> None:
    start_dir = _syo()
    set_data_dir(start_dir)

    portfolio_csv = start_dir / "chatgpt_portfolio_update.csv"

    # Load current state if present; if file is missing or unreadable, treat as empty (fresh Day-1)
    current_portfolio: Any
    try:
        if portfolio_csv.exists():
            current_portfolio, _ = load_latest_portfolio_state(str(portfolio_csv))
        else:
            current_portfolio = pd.DataFrame(columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"])  # empty
    except Exception:
        current_portfolio = pd.DataFrame(columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"])  # empty

    if _positions_count(current_portfolio) > 0:
        print("Day-1 already initialized; existing positions detected. No changes made.")
        return

    # STARTING_CASH must be provided at run time; defaulting would hide mistakes
    cash_env = os.getenv("STARTING_CASH")
    if cash_env is None:
        print("ERROR: STARTING_CASH env is required for Day-1 initialization.")
        sys.exit(2)
    try:
        starting_cash = float(cash_env)
    except Exception:
        print(f"ERROR: Invalid STARTING_CASH value: {cash_env}")
        sys.exit(2)
    if starting_cash <= 0:
        print(f"ERROR: STARTING_CASH must be > 0 (got {starting_cash}).")
        sys.exit(2)

    # Ensure an empty DataFrame seed for allocator
    empty_df = pd.DataFrame(columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"])

    # QuickStart implicitly ON: run allocation once
    try:
        allocated_portfolio, remaining_cash, executed = auto_trade_once(
            portfolio=empty_df, cash=starting_cash, base_dir=start_dir
        )
    except Exception as e:
        print(f"ERROR: auto_trade_once failed: {e}")
        sys.exit(1)

    # Write the priced snapshot and ensure CSVs exist
    try:
        priced_portfolio, priced_cash = price_and_update_csv(allocated_portfolio, remaining_cash)
    except Exception as e:
        print(f"ERROR: price_and_update_csv failed: {e}")
        sys.exit(1)

    # Minimal summary for logs
    print("Day-1 initialization complete.")
    print(f"Positions: {len(priced_portfolio.index) if isinstance(priced_portfolio, pd.DataFrame) else 'n/a'}")
    print(f"Cash remaining: {priced_cash}")
    if executed:
        try:
            first = executed[0]
            print(f"Executed sample: {first}")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)


