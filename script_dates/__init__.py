"""Dates: ASOF override, last trading date, and trading-day window helpers."""

from __future__ import annotations

from datetime import datetime
import os
import pandas as pd

ASOF_DATE: pd.Timestamp | None = None

def set_asof(date: str | datetime | pd.Timestamp | None) -> None:
    global ASOF_DATE
    if date is None:
        ASOF_DATE = None
        return
    ASOF_DATE = pd.Timestamp(date).normalize()

_env_asof = os.environ.get("ASOF_DATE")
if _env_asof:
    set_asof(_env_asof)

def _effective_now() -> datetime:
    return (ASOF_DATE.to_pydatetime() if ASOF_DATE is not None else datetime.now())

def last_trading_date(today: datetime | None = None) -> pd.Timestamp:
    dt = pd.Timestamp(today or _effective_now())
    if dt.weekday() == 5:
        return (dt - pd.Timedelta(days=1)).normalize()
    if dt.weekday() == 6:
        return (dt - pd.Timedelta(days=2)).normalize()
    return dt.normalize()

def check_weekend() -> str:
    return last_trading_date().date().isoformat()

def trading_day_window(target: datetime | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    d = last_trading_date(target)
    return d, (d + pd.Timedelta(days=1))


