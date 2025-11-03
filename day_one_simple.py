import os
import csv
import datetime

# Uses SchwabAdapter via env SCHWAB_ACCOUNT_ID and Secrets-based auth
from adapters.schwab import SchwabAdapter

SYO_DIR = "Start Your Own"
PORTFOLIO_CSV = os.path.join(SYO_DIR, "chatgpt_portfolio_update.csv")
TRADE_CSV = os.path.join(SYO_DIR, "chatgpt_trade_log.csv")


def get_settled_cash() -> float:
    acct = SchwabAdapter().get_account()
    cash = float(acct.get("cash", 0.0) or 0.0)
    return max(cash, 0.0)


def write_fresh_portfolio_csv(cash: float) -> None:
    os.makedirs(SYO_DIR, exist_ok=True)
    with open(PORTFOLIO_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "Date",
            "Ticker",
            "Shares",
            "Buy Price",
            "Cost Basis",
            "Stop Loss",
            "Current Price",
            "Total Value",
            "PnL",
            "Action",
            "Cash Balance",
            "Total Equity",
        ])
        today = datetime.date.today().isoformat()
        w.writerow([
            today,
            "TOTAL",
            "",
            "",
            "",
            "",
            "",
            "0",
            "0",
            "",
            f"{cash:.2f}",
            f"{cash:.2f}",
        ])


def ensure_trade_log_exists() -> None:
    os.makedirs(SYO_DIR, exist_ok=True)
    if not os.path.exists(TRADE_CSV):
        with open(TRADE_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Ticker", "Side", "Quantity", "Price", "OrderId", "Note"])


def main() -> None:
    acct_id = os.environ.get("SCHWAB_ACCOUNT_ID", "").strip()
    if not acct_id:
        raise SystemExit("SCHWAB_ACCOUNT_ID not set")

    cash = get_settled_cash()
    write_fresh_portfolio_csv(cash)
    ensure_trade_log_exists()
    print(f"Day-One simple: wrote fresh CSVs with settled cash = {cash:.2f}")


if __name__ == "__main__":
    main()







