from __future__ import annotations

from datetime import datetime, timedelta
import pytz


def now_et() -> datetime:
    return datetime.now(pytz.timezone("US/Eastern")).replace(microsecond=0)


def orders_time_window_iso(days_back: int = 2) -> tuple[str, str]:
    now = now_et()
    start = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0)
    return start.isoformat(), now.isoformat()


