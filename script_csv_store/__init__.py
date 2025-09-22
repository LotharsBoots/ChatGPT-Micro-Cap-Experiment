"""CSV Store: idempotent CSV writes with a simple file lock."""

from __future__ import annotations

from pathlib import Path
import os
import pandas as pd

def _lock_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(csv_path.suffix + ".lock")

def _acquire_lock(csv_path: Path, timeout_s: float = 10.0, poll_s: float = 0.2) -> bool:
    import time
    lock = _lock_path(csv_path)
    start = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if (time.time() - start) > timeout_s:
                return False
            time.sleep(poll_s)

def _release_lock(csv_path: Path) -> None:
    try:
        os.remove(_lock_path(csv_path))
    except FileNotFoundError:
        pass
    except Exception:
        pass

def _write_csv_idempotent(csv_path: Path, df_new: pd.DataFrame, subset_cols: list[str] | None = None) -> None:
    ok = _acquire_lock(csv_path)
    try:
        if csv_path.exists():
            try:
                existing = pd.read_csv(csv_path)
            except Exception:
                existing = pd.DataFrame()
            merged = pd.concat([existing, df_new], ignore_index=True)
        else:
            merged = df_new.copy()

        if subset_cols and all(col in merged.columns for col in subset_cols):
            merged = merged.drop_duplicates(subset=subset_cols, keep="last")
        else:
            merged = merged.drop_duplicates(keep="last")

        tmp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
        merged.to_csv(tmp_path, index=False)
        os.replace(tmp_path, csv_path)
    finally:
        if ok:
            _release_lock(csv_path)


