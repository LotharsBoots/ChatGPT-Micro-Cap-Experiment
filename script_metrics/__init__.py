"""Metrics: drawdown, Sharpe/Sortino, and CAPM calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd
from script_market_data import download_price_data

def compute_drawdown_and_returns(equity_series: pd.Series) -> tuple[float, object, pd.Series, int, float, pd.Series]:
    s = equity_series.astype(float).sort_index()
    running_max = s.cummax()
    drawdowns = (s / running_max) - 1.0
    max_dd = float(drawdowns.min()) if len(drawdowns) else float('nan')
    mdd_date = drawdowns.idxmin() if len(drawdowns) else None
    r = s.pct_change().dropna()
    return max_dd, mdd_date, r, len(r), float(s.iloc[-1]) if len(s) else float('nan'), s

def compute_risk_statistics(r: pd.Series, n_days: int, rf_annual: float = 0.045) -> tuple[float, float, float, float, float, float, float, float, float, float]:
    rf_daily = (1 + rf_annual) ** (1 / 252) - 1
    rf_period = (1 + rf_daily) ** n_days - 1 if n_days > 0 else float('nan')
    mean_daily = float(r.mean()) if n_days > 0 else float('nan')
    std_daily = float(r.std(ddof=1)) if n_days > 1 else float('nan')
    downside = (r - rf_daily).clip(upper=0)
    downside_std = float((downside.pow(2).mean()) ** 0.5) if not downside.empty else float('nan')
    r_numeric = pd.to_numeric(r, errors='coerce').dropna().astype(float)
    r_numeric = r_numeric[np.isfinite(r_numeric)]
    period_return = float(np.prod(1 + r_numeric.values) - 1) if len(r_numeric) else float('nan')
    sharpe_period = (period_return - rf_period) / (std_daily * np.sqrt(n_days)) if (n_days > 1 and std_daily and std_daily > 0) else float('nan')
    sharpe_annual = ((mean_daily - rf_daily) / std_daily) * np.sqrt(252) if (std_daily and std_daily > 0) else float('nan')
    sortino_period = (period_return - rf_period) / (downside_std * np.sqrt(n_days)) if (n_days > 1 and downside_std and downside_std > 0) else float('nan')
    sortino_annual = ((mean_daily - rf_daily) / downside_std) * np.sqrt(252) if (downside_std and downside_std > 0) else float('nan')
    return rf_daily, rf_period, mean_daily, std_daily, downside_std, period_return, sharpe_period, sharpe_annual, sortino_period, sortino_annual

def compute_capm_metrics(equity_series: pd.Series, r: pd.Series, rf_daily: float) -> tuple[float, float, float, int]:
    start_date = equity_series.index.min() - pd.Timedelta(days=1)
    end_date = equity_series.index.max() + pd.Timedelta(days=1)
    spx_fetch = download_price_data("^GSPC", start=start_date, end=end_date, progress=False)
    spx = spx_fetch.df
    beta = float('nan'); alpha_annual = float('nan'); r2 = float('nan'); n_obs = 0
    if not spx.empty and len(spx) >= 2:
        spx = spx.reset_index().set_index("Date").sort_index()
        mkt_ret = spx["Close"].astype(float).pct_change().dropna()
        common = r.index.intersection(mkt_ret.index)
        if len(common) >= 2:
            rp = (r.reindex(common).astype(float) - rf_daily)
            rm = (mkt_ret.reindex(common).astype(float) - rf_daily)
            x = np.asarray(rm.values, dtype=float).ravel()
            y = np.asarray(rp.values, dtype=float).ravel()
            n_obs = x.size
            if n_obs > 1 and np.std(x, ddof=1) > 0:
                beta, alpha_daily = np.polyfit(x, y, 1)
                alpha_annual = (1 + float(alpha_daily)) ** 252 - 1
                corr = np.corrcoef(x, y)[0, 1]
                r2 = float(corr ** 2)
    return (float(beta) if not pd.isna(beta) else float('nan')), alpha_annual, r2, n_obs

def compute_spx_value(equity_series: pd.Series) -> float:
    spx = download_price_data("^GSPC", start=equity_series.index.min(), end=equity_series.index.max() + pd.Timedelta(days=1), progress=False).df
    if spx.empty or len(equity_series) == 0:
        return float('nan')
    start_equity = float(equity_series.iloc[0])
    init = float(spx["Close"].iloc[0])
    now = float(spx["Close"].iloc[-1])
    return (start_equity / init) * now if start_equity == start_equity else float('nan')


