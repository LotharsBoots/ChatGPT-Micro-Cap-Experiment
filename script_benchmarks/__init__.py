"""Benchmarks: load benchmark tickers from tickers.json with sane defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional
import json
import logging

from script_data_paths import SCRIPT_DIR

logger = logging.getLogger(__name__)

def _read_json_file(path: Path) -> Optional[Dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        logger.warning("tickers.json present but malformed: %s -> %s. Falling back to defaults.", path, exc)
        return None
    except Exception as exc:
        logger.warning("Unable to read tickers.json (%s): %s. Falling back to defaults.", path, exc)
        return None

def load_benchmarks(script_dir: Path | None = None, defaults: List[str] | None = None) -> List[str]:
    base = Path(script_dir) if script_dir else SCRIPT_DIR
    for p in [base / "tickers.json", base.parent / "tickers.json"]:
        data = _read_json_file(p)
        if data is not None:
            arr = data.get("benchmarks") if isinstance(data, dict) else None
            if isinstance(arr, list):
                seen = set()
                out: List[str] = []
                for t in arr:
                    if isinstance(t, str):
                        up = t.strip().upper()
                        if up and up not in seen:
                            seen.add(up)
                            out.append(up)
                if out:
                    return out
            logger.warning("tickers.json missing 'benchmarks' array. Using defaults.")
            break
    return list(defaults or [])


