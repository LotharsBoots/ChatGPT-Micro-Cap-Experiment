"""Portfolio Ops: high-level wrapper that prompts, prices, and persists.

Exports:
- process_portfolio: optional manual prompt → pricing/stop-loss → CSV write
- log_manual_buy, log_manual_sell: re-exported convenience wrappers
"""

from __future__ import annotations

import pandas as pd

from script_portfolio_engine import _ensure_df, price_and_update_csv
from script_portfolio_input import prompt_and_apply_manual_trades, log_manual_buy, log_manual_sell


def process_portfolio(
    portfolio: pd.DataFrame | dict[str, list[object]] | list[dict[str, object]],
    cash: float,
    interactive: bool = True,
) -> tuple[pd.DataFrame, float]:
    portfolio_df = _ensure_df(portfolio)
    if interactive:
        portfolio_df, cash = prompt_and_apply_manual_trades(portfolio_df, cash)
    return price_and_update_csv(portfolio_df, cash)


__all__ = [
    "process_portfolio",
    "log_manual_buy",
    "log_manual_sell",
]


