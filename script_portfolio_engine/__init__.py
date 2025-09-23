"""Portfolio Engine: dataframe normalization and pricing + CSV snapshot.

Exports:
- _ensure_df: normalize any portfolio-like input into a canonical DataFrame
- price_and_update_csv: price positions, append daily snapshot CSV, and return
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from script_dates import last_trading_date, check_weekend
from script_market_data import download_price_data
from script_csv_store import _write_csv_idempotent
from script_data_paths import PORTFOLIO_CSV


CANON_COLS = ["ticker", "shares", "stop_loss", "buy_price", "cost_basis"]


def _ensure_df(
    portfolio: pd.DataFrame | Dict[str, List[object]] | List[Dict[str, object]]
) -> pd.DataFrame:
    """Return a DataFrame with canonical columns.

    Accepts a DataFrame, a list[dict], or a columnar dict.
    Missing columns are created with zeros.
    """
    if isinstance(portfolio, pd.DataFrame):
        df = portfolio.copy()
    elif isinstance(portfolio, list):
        df = pd.DataFrame(portfolio)
    elif isinstance(portfolio, dict):
        df = pd.DataFrame(portfolio)
    else:
        df = pd.DataFrame()

    # Normalize expected columns
    cols_map = {
        "Ticker": "ticker",
        "Shares": "shares",
        "Stop Loss": "stop_loss",
        "Buy Price": "buy_price",
        "Cost Basis": "cost_basis",
    }
    for src, dst in cols_map.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    for c in CANON_COLS:
        if c not in df.columns:
            df[c] = 0.0

    # Types
    df["ticker"] = df["ticker"].astype(str).str.upper()
    for c in ["shares", "stop_loss", "buy_price", "cost_basis"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).astype(float)

    # Remove empty rows
    df = df[df["ticker"].astype(str).str.len() > 0].reset_index(drop=True)
    return df[CANON_COLS]


def _fetch_last_close(ticker: str) -> float:
    end_d = last_trading_date()
    start_d = (end_d - pd.Timedelta(days=5)).normalize()
    fr = download_price_data(ticker, start=start_d, end=(end_d + pd.Timedelta(days=1)), progress=False)
    if isinstance(fr.df, pd.DataFrame) and not fr.df.empty:
        try:
            return float(fr.df["Close"].astype(float).iloc[-1])
        except Exception:
            return float(fr.df["Adj Close"].astype(float).iloc[-1]) if "Adj Close" in fr.df.columns else float("nan")
    return float("nan")


def _build_snapshot_rows(portfolio_df: pd.DataFrame, cash: float) -> pd.DataFrame:
    today = check_weekend()
    rows: List[Dict[str, Any]] = []

    total_value = 0.0
    total_pnl = 0.0

    for _, r in portfolio_df.iterrows():
        ticker = str(r["ticker"]).upper()
        shares = float(r["shares"]) or 0.0
        buy_price = float(r["buy_price"]) or 0.0
        cost_basis = float(r["cost_basis"]) or (buy_price * shares)

        px = _fetch_last_close(ticker)
        if not np.isfinite(px):
            # Fallback to buy price if price unavailable
            px = buy_price

        position_value = float(px * shares)
        pnl = float(position_value - cost_basis)

        total_value += position_value
        total_pnl += pnl

        rows.append({
            "Date": today,
            "Ticker": ticker,
            "Shares": float(shares),
            "Buy Price": float(round(buy_price, 2)) if buy_price else 0.0,
            "Cost Basis": float(round(cost_basis, 2)),
            "Stop Loss": float(round(float(r.get("stop_loss", 0.0)), 2)),
            "Current Price": float(round(px, 2)) if np.isfinite(px) else 0.0,
            "Total Value": float(round(position_value, 2)),
            "PnL": float(round(pnl, 2)),
            "Action": "HOLD",
            "Cash Balance": "",
            "Total Equity": "",
        })

    total_equity = float(round(total_value + float(cash), 2))

    rows.append({
        "Date": today,
        "Ticker": "TOTAL",
        "Shares": "",
        "Buy Price": "",
        "Cost Basis": "",
        "Stop Loss": "",
        "Current Price": "",
        "Total Value": float(round(total_value, 2)),
        "PnL": float(round(total_pnl, 2)),
        "Action": "",
        "Cash Balance": float(round(float(cash), 2)),
        "Total Equity": total_equity,
    })

    return pd.DataFrame(rows)


def price_and_update_csv(portfolio_df: pd.DataFrame, cash: float) -> Tuple[pd.DataFrame, float]:
    """Price positions, append snapshot to CSV, and return portfolio & cash.

    Returns (priced_portfolio_df, cash). Cash is unchanged here.
    """
    df = _ensure_df(portfolio_df)
    snapshot = _build_snapshot_rows(df, cash)

    # Ensure directory exists and write idempotently by Date+Ticker
    csv_path = Path(PORTFOLIO_CSV)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv_idempotent(csv_path, snapshot, subset_cols=["Date", "Ticker"])

    return df, cash

 