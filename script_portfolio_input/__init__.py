"""Portfolio Input: interactive manual buys/sells and trade logging helpers.

Exports:
- prompt_and_apply_manual_trades: REPL prompts to add manual trades
- log_manual_buy: limit/MOO buy logging and portfolio updates
- log_manual_sell: limit sell logging and position updates
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from script_dates import trading_day_window, check_weekend
from script_market_data import download_price_data
from script_csv_store import _write_csv_idempotent
from script_data_paths import TRADE_LOG_CSV


def prompt_and_apply_manual_trades(portfolio_df: pd.DataFrame, cash: float) -> tuple[pd.DataFrame, float]:
    today_iso = check_weekend()
    while True:
        print(portfolio_df)
        action = input(
            f""" You have {cash} in cash.
Would you like to log a manual trade? Enter 'b' for buy, 's' for sell, or press Enter to continue: """
        ).strip().lower()

        if action == "b":
            ticker = input("Enter ticker symbol: ").strip().upper()
            order_type = input("Order type? 'm' = market-on-open, 'l' = limit: ").strip().lower()

            try:
                shares = float(input("Enter number of shares: "))
                if shares <= 0:
                    raise ValueError
            except ValueError:
                print("Invalid share amount. Buy cancelled.")
                continue

            if order_type == "m":
                try:
                    stop_loss = float(input("Enter stop loss (or 0 to skip): "))
                    if stop_loss < 0:
                        raise ValueError
                except ValueError:
                    print("Invalid stop loss. Buy cancelled.")
                    continue

                s, e = trading_day_window()
                fetch = download_price_data(ticker, start=s, end=e, auto_adjust=False, progress=False)
                data = fetch.df
                if data.empty:
                    print(f"MOO buy for {ticker} failed: no market data available (source={fetch.source}).")
                    continue

                o = float(data["Open"].iloc[-1]) if "Open" in data else float(data["Close"].iloc[-1])
                exec_price = round(o, 2)
                notional = exec_price * shares
                if notional > cash:
                    print(f"MOO buy for {ticker} failed: cost {notional:.2f} exceeds cash {cash:.2f}.")
                    continue

                log = {
                    "Date": today_iso,
                    "Ticker": ticker,
                    "Shares Bought": shares,
                    "Buy Price": exec_price,
                    "Cost Basis": notional,
                    "PnL": 0.0,
                    "Reason": "MANUAL BUY MOO - Filled",
                }
                _write_csv_idempotent(TRADE_LOG_CSV, pd.DataFrame([log]), subset_cols=["Date", "Ticker", "Shares Bought", "Buy Price", "Reason"])

                rows = portfolio_df.loc[portfolio_df["ticker"].astype(str).str.upper() == ticker.upper()]
                if rows.empty:
                    new_trade = {
                        "ticker": ticker,
                        "shares": float(shares),
                        "stop_loss": float(stop_loss),
                        "buy_price": float(exec_price),
                        "cost_basis": float(notional),
                    }
                    if portfolio_df.empty:
                        portfolio_df = pd.DataFrame([new_trade])
                    else:
                        portfolio_df = pd.concat([portfolio_df, pd.DataFrame([new_trade])], ignore_index=True)
                else:
                    idx = rows.index[0]
                    cur_shares = float(portfolio_df.at[idx, "shares"])
                    cur_cost = float(portfolio_df.at[idx, "cost_basis"])
                    new_shares = cur_shares + float(shares)
                    new_cost = cur_cost + float(notional)
                    avg_price = new_cost / new_shares if new_shares else 0.0
                    portfolio_df.at[idx, "shares"] = new_shares
                    portfolio_df.at[idx, "cost_basis"] = new_cost
                    portfolio_df.at[idx, "buy_price"] = avg_price
                    portfolio_df.at[idx, "stop_loss"] = float(stop_loss)

                cash -= notional
                print(f"Manual BUY MOO for {ticker} filled at ${exec_price:.2f} ({fetch.source}).")
                continue

            elif order_type == "l":
                try:
                    buy_price = float(input("Enter buy LIMIT price: "))
                    stop_loss = float(input("Enter stop loss (or 0 to skip): "))
                    if buy_price <= 0 or stop_loss < 0:
                        raise ValueError
                except ValueError:
                    print("Invalid input. Limit buy cancelled.")
                    continue

                cash, portfolio_df = log_manual_buy(buy_price, shares, ticker, stop_loss, cash, portfolio_df)
                continue
            else:
                print("Unknown order type. Use 'm' or 'l'.")
                continue

        if action == "s":
            try:
                ticker = input("Enter ticker symbol: ").strip().upper()
                shares = float(input("Enter number of shares to sell (LIMIT): "))
                sell_price = float(input("Enter sell LIMIT price: "))
                if shares <= 0 or sell_price <= 0:
                    raise ValueError
            except ValueError:
                print("Invalid input. Manual sell cancelled.")
                continue

            cash, portfolio_df = log_manual_sell(sell_price, shares, ticker, cash, portfolio_df)
            continue

        break

    return portfolio_df, cash


def log_manual_buy(
    buy_price: float,
    shares: float,
    ticker: str,
    stoploss: float,
    cash: float,
    chatgpt_portfolio: pd.DataFrame,
    interactive: bool = True,
) -> tuple[float, pd.DataFrame]:
    today = check_weekend()
    if interactive:
        check = input(
            f"You are placing a BUY LIMIT for {shares} {ticker} at ${buy_price:.2f}.\nIf this is a mistake, type '1': "
        )
        if check == "1":
            print("Returning...")
            return cash, chatgpt_portfolio

    if not isinstance(chatgpt_portfolio, pd.DataFrame) or chatgpt_portfolio.empty:
        chatgpt_portfolio = pd.DataFrame(
            columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"]
        )

    s, e = trading_day_window()
    fetch = download_price_data(ticker, start=s, end=e, auto_adjust=False, progress=False)
    data = fetch.df
    if data.empty:
        print(f"Manual buy for {ticker} failed: no market data available (source={fetch.source}).")
        return cash, chatgpt_portfolio

    o = float(data.get("Open", [np.nan])[-1])
    h = float(data["High"].iloc[-1])
    l = float(data["Low"].iloc[-1])
    if np.isnan(o):
        o = float(data["Close"].iloc[-1])

    if o <= buy_price:
        exec_price = o
    elif l <= buy_price:
        exec_price = buy_price
    else:
        print(f"Buy limit ${buy_price:.2f} for {ticker} not reached today (range {l:.2f}-{h:.2f}). Order not filled.")
        return cash, chatgpt_portfolio

    cost_amt = exec_price * shares
    if cost_amt > cash:
        print(f"Manual buy for {ticker} failed: cost {cost_amt:.2f} exceeds cash balance {cash:.2f}.")
        return cash, chatgpt_portfolio

    log = {
        "Date": today,
        "Ticker": ticker,
        "Shares Bought": shares,
        "Buy Price": exec_price,
        "Cost Basis": cost_amt,
        "PnL": 0.0,
        "Reason": "MANUAL BUY LIMIT - Filled",
    }
    _write_csv_idempotent(
        TRADE_LOG_CSV,
        pd.DataFrame([log]),
        subset_cols=["Date", "Ticker", "Shares Bought", "Buy Price", "Reason"],
    )

    rows = chatgpt_portfolio.loc[chatgpt_portfolio["ticker"].str.upper() == ticker.upper()]
    if rows.empty:
        if chatgpt_portfolio.empty:
            chatgpt_portfolio = pd.DataFrame([{
                "ticker": ticker,
                "shares": float(shares),
                "stop_loss": float(stoploss),
                "buy_price": float(exec_price),
                "cost_basis": float(cost_amt),
            }])
        else:
            chatgpt_portfolio = pd.concat(
                [chatgpt_portfolio, pd.DataFrame([{
                    "ticker": ticker,
                    "shares": float(shares),
                    "stop_loss": float(stoploss),
                    "buy_price": float(exec_price),
                    "cost_basis": float(cost_amt),
                }])],
                ignore_index=True
            )
    else:
        idx = rows.index[0]
        cur_shares = float(chatgpt_portfolio.at[idx, "shares"])
        cur_cost = float(chatgpt_portfolio.at[idx, "cost_basis"])
        new_shares = cur_shares + float(shares)
        new_cost = cur_cost + float(cost_amt)
        chatgpt_portfolio.at[idx, "shares"] = new_shares
        chatgpt_portfolio.at[idx, "cost_basis"] = new_cost
        chatgpt_portfolio.at[idx, "buy_price"] = new_cost / new_shares if new_shares else 0.0
        chatgpt_portfolio.at[idx, "stop_loss"] = float(stoploss)

    cash -= cost_amt
    print(f"Manual BUY LIMIT for {ticker} filled at ${exec_price:.2f} ({fetch.source}).")
    return cash, chatgpt_portfolio


def log_manual_sell(
    sell_price: float,
    shares_sold: float,
    ticker: str,
    cash: float,
    chatgpt_portfolio: pd.DataFrame,
    reason: str | None = None,
    interactive: bool = True,
) -> tuple[float, pd.DataFrame]:
    today = check_weekend()
    if interactive:
        reason = input(
            f"""You are placing a SELL LIMIT for {shares_sold} {ticker} at ${sell_price:.2f}.
If this is a mistake, enter 1. """
        )
    if reason == "1":
        print("Returning...")
        return cash, chatgpt_portfolio
    elif reason is None:
        reason = ""

    if ticker not in chatgpt_portfolio["ticker"].values:
        print(f"Manual sell for {ticker} failed: ticker not in portfolio.")
        return cash, chatgpt_portfolio

    ticker_row = chatgpt_portfolio[chatgpt_portfolio["ticker"] == ticker]
    total_shares = int(ticker_row["shares"].item())
    if shares_sold > total_shares:
        print(f"Manual sell for {ticker} failed: trying to sell {shares_sold} shares but only own {total_shares}.")
        return cash, chatgpt_portfolio

    s, e = trading_day_window()
    fetch = download_price_data(ticker, start=s, end=e, auto_adjust=False, progress=False)
    data = fetch.df
    if data.empty:
        print(f"Manual sell for {ticker} failed: no market data available (source={fetch.source}).")
        return cash, chatgpt_portfolio

    o = float(data["Open"].iloc[-1]) if "Open" in data else np.nan
    h = float(data["High"].iloc[-1])
    l = float(data["Low"].iloc[-1])
    if np.isnan(o):
        o = float(data["Close"].iloc[-1])

    if o >= sell_price:
        exec_price = o
    elif h >= sell_price:
        exec_price = sell_price
    else:
        print(f"Sell limit ${sell_price:.2f} for {ticker} not reached today (range {l:.2f}-{h:.2f}). Order not filled.")
        return cash, chatgpt_portfolio

    buy_price = float(ticker_row["buy_price"].item())
    cost_basis = buy_price * shares_sold
    pnl = exec_price * shares_sold - cost_basis

    log = {
        "Date": today, "Ticker": ticker,
        "Shares Bought": "", "Buy Price": "",
        "Cost Basis": cost_basis, "PnL": pnl,
        "Reason": f"MANUAL SELL LIMIT - {reason}", "Shares Sold": shares_sold,
        "Sell Price": exec_price,
    }
    _write_csv_idempotent(
        TRADE_LOG_CSV,
        pd.DataFrame([log]),
        subset_cols=["Date", "Ticker", "Shares Bought", "Buy Price", "Reason", "Shares Sold", "Sell Price"],
    )

    if total_shares == shares_sold:
        chatgpt_portfolio = chatgpt_portfolio[chatgpt_portfolio["ticker"] != ticker]
    else:
        row_index = ticker_row.index[0]
        chatgpt_portfolio.at[row_index, "shares"] = total_shares - shares_sold
        chatgpt_portfolio.at[row_index, "cost_basis"] = (
            chatgpt_portfolio.at[row_index, "shares"] * chatgpt_portfolio.at[row_index, "buy_price"]
        )

    cash += shares_sold * exec_price
    print(f"Manual SELL LIMIT for {ticker} filled at ${exec_price:.2f} ({fetch.source}).")
    return cash, chatgpt_portfolio
