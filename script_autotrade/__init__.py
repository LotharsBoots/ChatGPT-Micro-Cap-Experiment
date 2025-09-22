"""Autotrade: simple rules engine (50d SMA) for auto buys/sells.

Exports:
- auto_trade_once: evaluates sell rules, then buys up to max positions
- autotrade.json: runtime config (universe, sizing, stop loss, rules)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import numpy as np
import pandas as pd

from script_dates import last_trading_date, check_weekend
from script_market_data import download_price_data
from script_portfolio_engine import _ensure_df
from script_portfolio_input import log_manual_sell
from script_csv_store import _write_csv_idempotent
from script_data_paths import SCRIPT_DIR, TRADE_LOG_CSV


def _default_autotrade_config() -> dict[str, object]:
    return {
        "universe": ["SPY", "IWM", "QQQ", "XBI"],
        "max_positions": 5,
        "per_trade_cash_pct": 0.2,
        "stop_loss_pct": 0.1,
        "entry_rule": "close_gt_sma50",
        "sell_rule": "close_lt_sma50",
        "take_profit_pct": 0.15,
    }


def _load_autotrade_config(base_dir: Path | None = None) -> dict[str, object]:
    cfg_path = (Path(base_dir) if base_dir else SCRIPT_DIR) / "autotrade.json"
    try:
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return {**_default_autotrade_config(), **data}
    except Exception:
        pass
    return _default_autotrade_config()


def _save_autotrade_config(cfg: dict[str, object], base_dir: Path | None = None) -> None:
    cfg_path = (Path(base_dir) if base_dir else SCRIPT_DIR) / "autotrade.json"
    try:
        with cfg_path.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception:
        pass


def auto_trade_once(
    portfolio: pd.DataFrame | dict[str, list[object]] | list[dict[str, object]],
    cash: float,
    base_dir: Path | None = None,
) -> tuple[pd.DataFrame, float, list[dict[str, object]]]:
    cfg = _load_autotrade_config(base_dir)
    universe = [str(t).upper() for t in (cfg.get("universe") or [])]
    max_positions = int(cfg.get("max_positions", 5))
    per_trade_cash_pct = float(cfg.get("per_trade_cash_pct", 0.2))
    stop_loss_pct = float(cfg.get("stop_loss_pct", 0.1))

    portfolio_df = _ensure_df(portfolio)
    portfolio_df = portfolio_df.copy()

    held = set(str(t).upper() for t in portfolio_df.get("ticker", pd.Series(dtype=str)).astype(str))
    executed: list[dict[str, object]] = []

    sell_rule = str(cfg.get("sell_rule", "close_lt_sma50")).lower()
    take_profit_pct = float(cfg.get("take_profit_pct", 0.15))

    if not portfolio_df.empty and len(held) > 0:
        for ticker in list(held):
            try:
                end_d = last_trading_date()
                start_d = end_d - pd.Timedelta(days=80)
                dfp = download_price_data(ticker, start=start_d, end=(end_d + pd.Timedelta(days=1)), progress=False).df
                if dfp.empty or len(dfp) < 50:
                    continue
                close_p = dfp["Close"].astype(float)
                sma50_p = close_p.rolling(50).mean()
                last_close_p = float(close_p.iloc[-1])
                last_sma50_p = float(sma50_p.iloc[-1])

                row = portfolio_df.loc[portfolio_df.get("ticker").astype(str).str.upper() == ticker]
                if row.empty:
                    continue
                total_shares = float(row.iloc[0].get("shares", 0))
                buy_price_row = float(row.iloc[0].get("buy_price", 0))
                if total_shares <= 0 or buy_price_row <= 0:
                    continue

                should_sell = False
                if sell_rule == "close_lt_sma50" and np.isfinite(last_sma50_p) and last_close_p < last_sma50_p:
                    should_sell = True
                if take_profit_pct and buy_price_row > 0 and ((last_close_p - buy_price_row) / buy_price_row) >= take_profit_pct:
                    should_sell = True

                if should_sell:
                    sell_price = max(0.01, round(last_close_p, 2))
                    cash, portfolio_df = log_manual_sell(
                        sell_price=sell_price,
                        shares_sold=total_shares,
                        ticker=ticker,
                        cash=cash,
                        chatgpt_portfolio=portfolio_df,
                        reason="AUTO SELL RULE",
                        interactive=False,
                    )
                    executed.append({"side": "SELL", "ticker": ticker, "shares": total_shares, "price": sell_price})
            except Exception:
                continue

    held = set(str(t).upper() for t in portfolio_df.get("ticker", pd.Series(dtype=str)).astype(str))
    remaining_slots = max(0, max_positions - len([t for t in held if t]))
    if remaining_slots <= 0 or cash <= 0 or not universe:
        return portfolio_df, cash, executed

    for ticker in universe:
        if remaining_slots <= 0 or cash <= 0:
            break
        if ticker in held:
            continue

        try:
            end_d = last_trading_date()
            start_d = end_d - pd.Timedelta(days=80)
            df = download_price_data(ticker, start=start_d, end=(end_d + pd.Timedelta(days=1)), progress=False).df
            if df.empty or len(df) < 50:
                continue
            close = df["Close"].astype(float)
            sma50 = close.rolling(50).mean()
            last_close = float(close.iloc[-1])
            last_sma50 = float(sma50.iloc[-1])
            if not (np.isfinite(last_close) and np.isfinite(last_sma50)):
                continue
            if last_close <= last_sma50:
                continue

            allocation = max(0.0, cash * per_trade_cash_pct)
            shares = int(allocation // last_close)
            if shares < 1:
                continue

            exec_price = round(last_close, 2)
            notional = round(exec_price * shares, 2)
            stop_loss = round(exec_price * (1.0 - stop_loss_pct), 2)
            if notional > cash:
                continue

            today = check_weekend()
            log = {
                "Date": today,
                "Ticker": ticker,
                "Shares Bought": float(shares),
                "Buy Price": float(exec_price),
                "Cost Basis": float(notional),
                "PnL": 0.0,
                "Reason": "AUTO BUY - close>50dSMA",
            }
            _write_csv_idempotent(TRADE_LOG_CSV, pd.DataFrame([log]), subset_cols=["Date", "Ticker", "Shares Bought", "Buy Price", "Reason"])

            new_pos = {
                "ticker": ticker,
                "shares": float(shares),
                "stop_loss": float(stop_loss),
                "buy_price": float(exec_price),
                "cost_basis": float(notional),
            }
            if portfolio_df.empty:
                portfolio_df = pd.DataFrame([new_pos])
            else:
                portfolio_df = pd.concat([portfolio_df, pd.DataFrame([new_pos])], ignore_index=True)

            cash -= notional
            remaining_slots -= 1
            executed.append({"ticker": ticker, "shares": shares, "price": exec_price})
        except Exception:
            continue

    return portfolio_df, cash, executed


