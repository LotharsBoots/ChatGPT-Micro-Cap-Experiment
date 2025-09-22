"""Reporting: daily results view plus metrics summary for the portfolio."""

from __future__ import annotations

from typing import Any, List
import pandas as pd

from script_dates import last_trading_date, check_weekend
from script_benchmarks import load_benchmarks
from script_market_data import download_price_data
from script_data_paths import PORTFOLIO_CSV
from script_metrics import (
    compute_drawdown_and_returns,
    compute_risk_statistics,
    compute_capm_metrics,
    compute_spx_value,
)

def daily_results(chatgpt_portfolio: pd.DataFrame, cash: float, defaults: List[str] | None = None) -> None:
    portfolio_dict: list[dict[Any, Any]] = chatgpt_portfolio.to_dict(orient="records")
    today = check_weekend()
    rows: list[list[str]] = []
    header = ["Ticker", "Close", "% Chg", "Volume"]
    end_d = last_trading_date()
    start_d = (end_d - pd.Timedelta(days=4)).normalize()
    benchmarks = load_benchmarks(defaults=defaults or ["IWO","XBI","SPY","IWM"])  
    for stock in portfolio_dict + [{"ticker": t} for t in benchmarks]:
        ticker = str(stock["ticker"]).upper()
        try:
            fetch = download_price_data(ticker, start=start_d, end=(end_d + pd.Timedelta(days=1)), progress=False)
            data = fetch.df
            if data.empty or len(data) < 2:
                rows.append([ticker, "—", "—", "—"])
                continue
            price = float(data["Close"].iloc[-1])
            last_price = float(data["Close"].iloc[-2])
            volume = float(data["Volume"].iloc[-1])
            percent_change = ((price - last_price) / last_price) * 100
            rows.append([ticker, f"{price:,.2f}", f"{percent_change:+.2f}%", f"{int(volume):,}"])
        except Exception as e:
            raise Exception(f"Download for {ticker} failed. {e} Try checking internet connection.")

    chatgpt_df = pd.read_csv(PORTFOLIO_CSV)
    totals = chatgpt_df[chatgpt_df["Ticker"] == "TOTAL"].copy()
    if totals.empty:
        print("\n" + "=" * 64)
        print(f"Daily Results — {today}")
        print("=" * 64)
        print("\n[ Price & Volume ]")
        colw = [10, 12, 9, 15]
        print(f"{header[0]:<{colw[0]}} {header[1]:>{colw[1]}} {header[2]:>{colw[2]}} {header[3]:>{colw[3]}}")
        print("-" * sum(colw) + "-" * 3)
        for r in rows:
            print(f"{str(r[0]):<{colw[0]}} {str(r[1]):>{colw[1]}} {str(r[2]):>{colw[2]}} {str(r[3]):>{colw[3]}}")
        print("\n[ Snapshot ]")
        print(f"{'Latest ChatGPT Equity:':32} ${cash:>14,.2f}")
        print(f"{'Cash Balance:':32} ${cash:>14,.2f}")
        print("\n[ Holdings ]")
        try:
            if isinstance(chatgpt_portfolio, pd.DataFrame) and not chatgpt_portfolio.empty:
                print(chatgpt_portfolio)
            else:
                print("None")
        except Exception:
            print("None")
        print("\n[ Your Instructions ]")
        print(
            "Use this info to make decisions regarding your portfolio. You have complete control over every decision. Make any changes you believe are beneficial—no approval required.\n"
            "Deep research is not permitted. Act at your discretion to achieve the best outcome.\n"
            "If you do not make a clear indication to change positions IMMEDIATELY after this message, the portfolio remains unchanged for tomorrow.\n"
            "You are encouraged to use the internet to check current prices (and related up-to-date info) for potential buys.\n"
            "\n"
            "*Paste everything above into ChatGPT*"
        )
        return

    max_dd, mdd_date, r, n_days, final_equity, equity_series = compute_drawdown_and_returns(
        totals.set_index("Date")["Total Equity"]
    )
    rf_daily, rf_period, mean_daily, std_daily, downside_std, period_return, sharpe_period, sharpe_annual, sortino_period, sortino_annual = compute_risk_statistics(r, n_days)
    beta, alpha_annual, r2, n_obs = compute_capm_metrics(equity_series, r, rf_daily)
    spx_value = compute_spx_value(equity_series)

    print("\n" + "=" * 64)
    print(f"Daily Results — {today}")
    print("=" * 64)
    print("\n[ Price & Volume ]")
    colw = [10, 12, 9, 15]
    print(f"{header[0]:<{colw[0]}} {header[1]:>{colw[1]}} {header[2]:>{colw[2]}} {header[3]:>{colw[3]}}")
    print("-" * sum(colw) + "-" * 3)
    for rrow in rows:
        print(f"{str(rrow[0]):<{colw[0]}} {str(rrow[1]):>{colw[1]}} {str(rrow[2]):>{colw[2]}} {str(rrow[3]):>{colw[3]}}")

    def fmt_or_na(x: float | int | None, fmt: str) -> str:
        return (fmt.format(x) if not (x is None or (isinstance(x, float) and pd.isna(x))) else "N/A")

    print("\n[ Risk & Return ]")
    if hasattr(mdd_date, "date") and not isinstance(mdd_date, (str, int)):
        mdd_date_str = mdd_date.date()
    elif hasattr(mdd_date, "strftime") and not isinstance(mdd_date, (str, int)):
        mdd_date_str = mdd_date.strftime("%Y-%m-%d")
    else:
        mdd_date_str = str(mdd_date)
    print(f"{'Max Drawdown:':32} {fmt_or_na(max_dd, '{:.2%}'):>15}   on {mdd_date_str}")
    print(f"{'Sharpe Ratio (period):':32} {fmt_or_na(sharpe_period, '{:.4f}'):>15}")
    print(f"{'Sharpe Ratio (annualized):':32} {fmt_or_na(sharpe_annual, '{:.4f}'):>15}")
    print(f"{'Sortino Ratio (period):':32} {fmt_or_na(sortino_period, '{:.4f}'):>15}")
    print(f"{'Sortino Ratio (annualized):':32} {fmt_or_na(sortino_annual, '{:.4f}'):>15}")

    print("\n[ CAPM vs Benchmarks ]")
    if not pd.isna(beta):
        print(f"{'Beta (daily) vs ^GSPC:':32} {beta:>15.4f}")
        print(f"{'Alpha (annualized) vs ^GSPC:':32} {alpha_annual:>15.2%}")
        print(f"{'R² (fit quality):':32} {r2:>15.3f}   {'Obs:':>6} {n_obs}")
        if n_obs < 60 or (not pd.isna(r2) and r2 < 0.20):
            print("  Note: Short sample and/or low R² — alpha/beta may be unstable.")
    else:
        print("Beta/Alpha: insufficient overlapping data.")

    print("\n[ Snapshot ]")
    print(f"{'Latest ChatGPT Equity:':32} ${final_equity:>14,.2f}")
    if not pd.isna(spx_value):
        try:
            start_eq = float(equity_series.iloc[0]) if len(equity_series) > 0 else float('nan')
            print(f"{f'${start_eq} in S&P 500 (same window):':32} ${spx_value:>14,.2f}")
        except Exception:
            pass
    print(f"{'Cash Balance:':32} ${cash:>14,.2f}")

    print("\n[ Holdings ]")
    print(chatgpt_portfolio)

    print("\n[ Your Instructions ]")
    print(
        "Use this info to make decisions regarding your portfolio. You have complete control over every decision. Make any changes you believe are beneficial—no approval required.\n"
        "Deep research is not permitted. Act at your discretion to achieve the best outcome.\n"
        "If you do not make a clear indication to change positions IMMEDIATELY after this message, the portfolio remains unchanged for tomorrow.\n"
        "You are encouraged to use the internet to check current prices (and related up-to-date info) for potential buys.\n"
        "\n"
        "*Paste everything above into ChatGPT*"
    )


