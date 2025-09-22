"""Trading Script Orchestrator: coordinates micro-modules for portfolio management.

This orchestrator imports and coordinates:
- script_dates: ASOF override, trading day helpers
- script_market_data: Yahoo→Stooq fallbacks
- script_portfolio_*: pricing, manual trades, autotrade
- script_benchmarks: ticker loading
- script_metrics: performance calculations
- script_reporting: daily results display
"""

from __future__ import annotations

from pathlib import Path
import os
import warnings

import pandas as pd
import logging
from script_dates import set_asof, last_trading_date, check_weekend, trading_day_window
from script_data_paths import SCRIPT_DIR, PORTFOLIO_CSV, TRADE_LOG_CSV, set_data_dir
from script_csv_store import _write_csv_idempotent
from script_market_data import download_price_data
from script_benchmarks import load_benchmarks
from script_metrics import (
    compute_drawdown_and_returns,
    compute_risk_statistics,
    compute_capm_metrics,
    compute_spx_value,
)
from script_portfolio_ops import process_portfolio
from script_autotrade import auto_trade_once
from script_reporting import daily_results

# Silence display-only deprecation chatter in console
warnings.filterwarnings("ignore", category=FutureWarning)

_env_asof = os.environ.get("ASOF_DATE")
if _env_asof:
    set_asof(_env_asof)

logger = logging.getLogger(__name__)


# ------------------------------
# Date helpers
# ------------------------------

## dates moved to script_dates


# ------------------------------
# Data access layer
# ------------------------------

## market data moved to script_market_data



# ------------------------------
# File I/O helpers (idempotent writes + simple lock)
# ------------------------------

## csv helpers moved to script_csv_store


# ------------------------------
# File path configuration
# ------------------------------

## file paths moved to script_data_paths


# ------------------------------
# Portfolio operations
# ------------------------------

## portfolio processing moved to script_portfolio_* modules



## trade logging and manual trade helpers moved to script_portfolio_* modules



# ------------------------------
# Auto-trading (rule-based)
# ------------------------------

## autotrade moved to script_autotrade


## reporting moved to script_reporting


# ------------------------------
# Orchestration
# ------------------------------

def load_latest_portfolio_state(
    file: str,
) -> tuple[pd.DataFrame | list[dict[str, Any]], float]:
    """Load the most recent portfolio snapshot and cash balance."""
    df = pd.read_csv(file)
    if df.empty:
        portfolio = pd.DataFrame(columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"])
        # Fully automated: use env STARTING_CASH or default to 10000.0
        env_cash = os.environ.get("STARTING_CASH", "10000")
        try:
            cash = float(env_cash)
        except Exception:
            cash = 10000.0
        print(f"Portfolio CSV is empty. Using starting cash ${cash:,.2f} (override with STARTING_CASH).")
        return portfolio, cash

    non_total = df[df["Ticker"] != "TOTAL"].copy()
    non_total["Date"] = pd.to_datetime(non_total["Date"])

    latest_date = non_total["Date"].max()
    latest_tickers = non_total[non_total["Date"] == latest_date].copy()
    sold_mask = latest_tickers["Action"].astype(str).str.startswith("SELL")
    latest_tickers = latest_tickers[~sold_mask].copy()
    latest_tickers.drop(
        columns=[
            "Date",
            "Cash Balance",
            "Total Equity",
            "Action",
            "Current Price",
            "PnL",
            "Total Value",
        ],
        inplace=True,
        errors="ignore",
    )
    latest_tickers.rename(
        columns={
            "Cost Basis": "cost_basis",
            "Buy Price": "buy_price",
            "Shares": "shares",
            "Ticker": "ticker",
            "Stop Loss": "stop_loss",
        },
        inplace=True,
    )
    latest_tickers = latest_tickers.reset_index(drop=True).to_dict(orient="records")

    df_total = df[df["Ticker"] == "TOTAL"].copy()
    df_total["Date"] = pd.to_datetime(df_total["Date"])
    latest = df_total.sort_values("Date").iloc[-1]
    cash = float(latest["Cash Balance"])
    return latest_tickers, cash


def main(file: str, data_dir: Path | None = None) -> None:
    """Check versions, then run the trading script."""
    chatgpt_portfolio, cash = load_latest_portfolio_state(file)
    print(file)
    # If no explicit data_dir provided, default to directory of the file path
    if data_dir is None:
        try:
            data_dir = Path(file).resolve().parent
        except Exception:
            data_dir = SCRIPT_DIR
    set_data_dir(data_dir)

    # ---- Day-1 friendly starting cash prompt (CLI only) ----
    try:
        is_first_day = isinstance(chatgpt_portfolio, pd.DataFrame) and chatgpt_portfolio.empty
    except Exception:
        is_first_day = False

    if is_first_day:
        try:
            user_cash = input(
                f"Please enter cash amount you would like invest. (press Enter to keep ${cash:,.2f}): "
            ).strip()
            if user_cash:
                new_cash = float(user_cash)
                if new_cash >= 0:
                    cash = new_cash
                    print(f"Starting cash set to ${cash:,.2f}.")
        except Exception:
            # Keep prior cash on any input error
            pass

        # ---- Optional Day-1 QuickStart auto-allocation (one-time) ----
        try:
            quickstart = input(
                "Would you like to start investing right away? (y/N): "
            ).strip().lower()
            if quickstart == "y":
                base_dir = Path(data_dir) if data_dir is not None else SCRIPT_DIR
                try:
                    allocated_portfolio, cash, executed = auto_trade_once(
                        chatgpt_portfolio, cash, base_dir=base_dir
                    )
                    chatgpt_portfolio = allocated_portfolio
                    if executed:
                        print("QuickStart executed the following trades:")
                        for t in executed:
                            side = t.get("side", "BUY") if isinstance(t.get("side"), str) else ("BUY" if "price" in t else "")
                            print(f" - {side or 'BUY'} {t.get('ticker')} {t.get('shares')}@{t.get('price')}")
                    else:
                        print("QuickStart found no eligible buys based on current rules.")
                except Exception as e:
                    print(f"QuickStart failed: {e}")
        except Exception:
            pass

    chatgpt_portfolio, cash = process_portfolio(chatgpt_portfolio, cash, interactive=True)
    daily_results(chatgpt_portfolio, cash)


if __name__ == "__main__":
    import argparse

    # Default CSV path resolution (keep your existing logic)
    csv_path = PORTFOLIO_CSV if PORTFOLIO_CSV.exists() else (SCRIPT_DIR / "chatgpt_portfolio_update.csv")

    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=str(csv_path), help="Path to chatgpt_portfolio_update.csv")
    parser.add_argument("--data-dir", default=None, help="Optional data directory")
    parser.add_argument("--asof", default=None, help="Treat this YYYY-MM-DD as 'today' (e.g., 2025-08-27)")
    args = parser.parse_args()

    if args.asof:
        set_asof(args.asof)

    if not Path(args.file).exists():
        print("No portfolio CSV found. Create one or run main() with your file path.")
    else:
        main(args.file, Path(args.data_dir) if args.data_dir else None)
