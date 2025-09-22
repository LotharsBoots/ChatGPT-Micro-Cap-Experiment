"""Data Paths: central file locations and data-dir configuration."""

from __future__ import annotations

from pathlib import Path
import os

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SCRIPT_DIR / "Start Your Own"
PORTFOLIO_CSV = DATA_DIR / "chatgpt_portfolio_update.csv"
TRADE_LOG_CSV = DATA_DIR / "chatgpt_trade_log.csv"

def set_data_dir(data_dir: Path) -> None:
    global DATA_DIR, PORTFOLIO_CSV, TRADE_LOG_CSV
    DATA_DIR = Path(data_dir)
    os.makedirs(DATA_DIR, exist_ok=True)
    PORTFOLIO_CSV = DATA_DIR / "chatgpt_portfolio_update.csv"
    TRADE_LOG_CSV = DATA_DIR / "chatgpt_trade_log.csv"


