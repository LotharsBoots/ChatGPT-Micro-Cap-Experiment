"""Market Data: Yahoo primary with Stooq fallbacks and weekend-safe ranges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
import pandas as pd
import numpy as np

from script_dates import last_trading_date

try:
    import pandas_datareader.data as pdr  # noqa: F401
    _HAS_PDR = True
except Exception:
    _HAS_PDR = False

STOOQ_MAP = {"^GSPC": "^SPX", "^DJI": "^DJI", "^IXIC": "^IXIC"}
STOOQ_BLOCKLIST = {"^RUT"}

@dataclass
class FetchResult:
    df: pd.DataFrame
    source: str

def _to_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception:
            pass
    return df

def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        if c not in df.columns:
            df[c] = np.nan
    if "Adj Close" not in df.columns:
        df["Adj Close"] = df["Close"]
    return df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]

def _yahoo_download(ticker: str, **kwargs: Any) -> pd.DataFrame:
    import io, requests, logging
    from contextlib import redirect_stdout, redirect_stderr
    import yfinance as yf
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    kwargs.setdefault("progress", False)
    kwargs.setdefault("threads", False)
    kwargs.setdefault("session", s)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            df = cast(pd.DataFrame, yf.download(ticker, **kwargs))
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

def _stooq_download(ticker: str, start: datetime | pd.Timestamp, end: datetime | pd.Timestamp) -> pd.DataFrame:
    if not _HAS_PDR or ticker in STOOQ_BLOCKLIST:
        return pd.DataFrame()
    t = STOOQ_MAP.get(ticker, ticker)
    if not t.startswith("^"):
        t = t.lower()
    try:
        import pandas_datareader.data as pdr_local
        df = cast(pd.DataFrame, pdr_local.DataReader(t, "stooq", start=start, end=end))
        df.sort_index(inplace=True)
        return df
    except Exception:
        return pd.DataFrame()

def _stooq_csv_download(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    import requests, io
    if ticker in STOOQ_BLOCKLIST:
        return pd.DataFrame()
    t = STOOQ_MAP.get(ticker, ticker)
    if not t.startswith("^"):
        sym = t.lower()
        if not sym.endswith(".us"):
            sym = f"{sym}.us"
    else:
        sym = t.lower()
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200 or not r.text.strip():
            return pd.DataFrame()
        df = pd.read_csv(io.StringIO(r.text))
        if df.empty:
            return pd.DataFrame()
        df["Date"] = pd.to_datetime(df["Date"])
        df.set_index("Date", inplace=True)
        df.sort_index(inplace=True)
        df = df.loc[(df.index >= start.normalize()) & (df.index < end.normalize())]
        if "Adj Close" not in df.columns:
            df["Adj Close"] = df["Close"]
        return df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    except Exception:
        return pd.DataFrame()

def _weekend_safe_range(period: str | None, start: Any, end: Any) -> tuple[pd.Timestamp, pd.Timestamp]:
    if start or end:
        e = pd.Timestamp(end) if end else last_trading_date() + pd.Timedelta(days=1)
        s = pd.Timestamp(start) if start else (e - pd.Timedelta(days=5))
        return s.normalize(), e.normalize()
    days = int(period[:-1]) if isinstance(period, str) and period.endswith("d") else 1
    end_tr = last_trading_date()
    return (end_tr - pd.Timedelta(days=days)).normalize(), (end_tr + pd.Timedelta(days=1)).normalize()

def download_price_data(ticker: str, **kwargs: Any) -> FetchResult:
    period = kwargs.pop("period", None)
    start = kwargs.pop("start", None)
    end = kwargs.pop("end", None)
    kwargs.setdefault("progress", False)
    kwargs.setdefault("threads", False)
    s, e = _weekend_safe_range(period, start, end)

    df = _yahoo_download(ticker, start=s, end=e, **kwargs)
    if isinstance(df, pd.DataFrame) and not df.empty:
        return FetchResult(_normalize_ohlcv(_to_datetime_index(df)), "yahoo")
    df = _stooq_download(ticker, start=s, end=e)
    if isinstance(df, pd.DataFrame) and not df.empty:
        return FetchResult(_normalize_ohlcv(_to_datetime_index(df)), "stooq-pdr")
    df = _stooq_csv_download(ticker, s, e)
    if isinstance(df, pd.DataFrame) and not df.empty:
        return FetchResult(_normalize_ohlcv(_to_datetime_index(df)), "stooq-csv")
    proxy = {"^GSPC": "SPY", "^RUT": "IWM"}.get(ticker)
    if proxy:
        dfp = _yahoo_download(proxy, start=s, end=e, **kwargs)
        if isinstance(dfp, pd.DataFrame) and not dfp.empty:
            return FetchResult(_normalize_ohlcv(_to_datetime_index(dfp)), f"yahoo:{proxy}-proxy")
    return FetchResult(pd.DataFrame(columns=["Open","High","Low","Close","Adj Close","Volume"]), "empty")


