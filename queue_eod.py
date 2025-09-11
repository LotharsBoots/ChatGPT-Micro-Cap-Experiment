r"""End-of-day (EOD) automation: call OpenAI (Prompt ID), produce orders_queue.json, email summary.

Behavior:
- Loads portfolio state from Start Your Own CSVs using existing trading_script helpers
- Calls your saved OpenAI Platform Prompt by ID with ONLY the user-side variables
- Requires STRICT JSON {buy:[], sell:[]} and validates/normalizes for next market open
- Writes Start Your Own\orders_queue.json with idempotency (no duplicates)
- Sends an email summary via Mailgun if configured
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple
import hashlib
import json
import os
import re
import sys
from datetime import datetime, UTC
from dotenv import load_dotenv


from trading_script import (
    load_latest_portfolio_state,
    set_data_dir,
)

# ----------------------------
# Paths, IO, helpers (unchanged)
# ----------------------------

def _project_root() -> Path:
    return Path(__file__).resolve().parent

def _start_your_own_dir() -> Path:
    return _project_root() / "Start Your Own"

def _orders_path() -> Path:
    return _start_your_own_dir() / "orders_queue.json"

def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None

def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)

def _hash_id(parts: List[str]) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return h

def _strict_upper_ticker(t: str) -> str:
    up = str(t).strip().upper()
    if not re.fullmatch(r"[A-Z.\-]+", up):
        raise ValueError(f"Invalid ticker: {t}")
    return up

def _norm_status(value: object) -> str:
    """Normalize a status string for case-insensitive comparisons."""
    return str(value or "").strip().lower()

def _is_final_status(status: str) -> bool:
    return _norm_status(status) in {"filled", "cancelled", "canceled"}

def _coalesce_pending(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Combine pending intents with identical keys into single orders.

    Key for coalescing: (ticker, side, order_type, limit_price).
    Quantity is summed. New compact ids are generated from the combined parts.
    """
    buckets: Dict[tuple, Dict[str, Any]] = {}
    for it in items:
        ticker = str(it.get("ticker")).upper()
        side = str(it.get("side")).lower()
        otype = str(it.get("order_type")).upper()
        limit_price = it.get("limit_price")
        qty = int(float(it.get("quantity") or 0))
        if qty <= 0:
            continue
        key = (ticker, side, otype, float(limit_price) if limit_price is not None else None)
        if key not in buckets:
            buckets[key] = {
                "ticker": ticker,
                "side": side,
                "quantity": 0,
                "order_type": otype,
                "limit_price": (float(limit_price) if limit_price is not None else None),
                "validity": "next_open",
                "rationale": it.get("rationale") or "LLM decision",
            }
        buckets[key]["quantity"] += qty

    out: List[Dict[str, Any]] = []
    for b in buckets.values():
        oid = _hash_id([
            b["ticker"],
            b["side"],
            str(int(b["quantity"])),
            b["order_type"],
            str(b["limit_price"] or ""),
        ])
        item = {"id": oid, **b}
        out.append(item)
    return out

def _normalize_orders(model_json: Dict[str, Any], portfolio_cash: float) -> List[Dict[str, Any]]:
    """Validate and normalize model JSON {buy:[], sell:[]} into our order schema."""
    if not isinstance(model_json, dict):
        raise ValueError("Model output must be a JSON object")
    for k in ("buy", "sell"):
        if k not in model_json or not isinstance(model_json[k], list):
            raise ValueError(f"Model output missing '{k}' list")

    orders: List[Dict[str, Any]] = []

    def add(side: str, item: Dict[str, Any]) -> None:
        ticker = _strict_upper_ticker(item.get("ticker", ""))
        order_type = str(item.get("order_type", "")).upper()
        limit_price = item.get("limit_price")
        qty = None

        if "quantity" in item:
            qty = int(float(item["quantity"]))
        elif "percent" in item:
            pct = float(item["percent"])  # 0..1
            # Rough placeholder sizing using cash only; executor will finalize qty with prices
            approx_price = 1.0
            qty = max(1, int((portfolio_cash * pct) // approx_price))

        if qty is None or qty <= 0:
            raise ValueError("quantity/percent must produce positive qty")
        if order_type not in {"MOO", "LOO"}:
            raise ValueError("order_type must be MOO or LOO for next_open flow")
        if order_type == "LOO" and (limit_price is None or float(limit_price) <= 0):
            raise ValueError("LOO requires limit_price > 0")

        oid = _hash_id([ticker, side, str(qty), order_type, str(limit_price or "")])
        orders.append({
            "id": oid,
            "ticker": ticker,
            "side": side.lower(),
            "quantity": int(qty),
            "order_type": order_type,
            "limit_price": (float(limit_price) if limit_price is not None else None),
            "validity": "next_open",
            "rationale": "LLM decision",
        })

    for side_key in ("buy", "sell"):
        for it in model_json.get(side_key, []):
            if isinstance(it, dict):
                add("buy" if side_key == "buy" else "sell", it)

    return orders

# ----------------------------
# OpenAI: Prompt ID call ONLY
# ----------------------------

def _extract_holdings(portfolio: Any) -> List[Dict[str, Any]]:
    """Derive a simple holdings list [{ticker, shares}] from generic portfolio rows."""
    out: List[Dict[str, Any]] = []
    if not isinstance(portfolio, list):
        return out
    for row in portfolio:
        if not isinstance(row, dict):
            continue
        ticker = (
            row.get("ticker") or row.get("symbol") or row.get("Ticker") or row.get("SYMBOL")
        )
        shares = (
            row.get("shares") or row.get("qty") or row.get("quantity") or row.get("Shares") or 0
        )
        if ticker:
            try:
                qty = int(float(shares))
            except Exception:
                qty = 0
            out.append({"ticker": str(ticker).upper(), "shares": qty})
    return out

def _call_deep_research_via_prompt_id(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Call your saved Platform Prompt by ID with ONLY user-side variables."""
    from openai import OpenAI

    load_dotenv()
    pid = os.getenv("DEEP_RESEARCH_PROMPT_ID")
    if not pid:
        raise SystemExit("DEEP_RESEARCH_PROMPT_ID missing in .env")
    pver = os.getenv("DEEP_RESEARCH_PROMPT_VERSION")  # optional
    reasoning_effort = (os.getenv("LLM_REASONING") or "high").strip().lower()

    client = OpenAI()  # uses OPENAI_API_KEY from env

    prompt_obj: Dict[str, Any] = {"id": pid}
    if pver:
        prompt_obj["version"] = pver

    # Send ONLY a user message containing our JSON payload; developer/tools live in the Prompt
    resp = client.responses.create(
        prompt=prompt_obj,
        input=[{
            "role": "user",
            "content": [{"type": "input_text", "text": json.dumps(payload)}]
        }],
        reasoning={"effort": reasoning_effort},
        # Intentionally omit 'model' to use the prompt’s saved model (keeps Platform in sync)
    )

    # Robust text extraction
    try:
        text = resp.output_text  # preferred accessor
    except Exception:
        try:
            text = resp.output[-1].content[0].text
        except Exception as e:
            raise RuntimeError(f"Could not extract text from response: {e}")

    # Strip any code fences and parse JSON
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        nl = t.find("\n")
        if nl != -1 and "json" in t[:nl].lower():
            t = t[nl + 1 :].strip()

    try:
        obj = json.loads(t or "{}")
    except Exception as e:
        raise ValueError(f"Deep Research returned non-JSON: {e}\nRAW:\n{text[:1200]}")

    # Basic top-level validation here; deeper checks in _normalize_orders
    if not isinstance(obj, dict) or "buy" not in obj or "sell" not in obj:
        raise ValueError("Deep Research output must be an object with 'buy' and 'sell' lists")
    return obj

# ----------------------------
# Email summary (unchanged)
# ----------------------------

def _email_mailgun(subject: str, text: str) -> None:
    import requests
    load_dotenv()
    domain = os.getenv("MAILGUN_DOMAIN")
    key = os.getenv("MAILGUN_API_KEY")
    sender = os.getenv("MAILGUN_FROM")
    recipient = os.getenv("MAILGUN_TO")
    if not all([domain, key, sender, recipient]):
        return
    try:
        requests.post(
            f"https://api.mailgun.net/v3/{domain}/messages",
            auth=("api", key),
            data={"from": sender, "to": [recipient], "subject": subject, "text": text},
            timeout=15,
        )
    except Exception:
        pass

# ----------------------------
# Main: Friday automation path
# ----------------------------

CONSTANT_INSTRUCTIONS = (
    "Last analysis thesis for current holdings. Use this info to make decisions regarding your portfolio. "
    "You have complete control over every decision. Make any changes you believe are beneficial. No approval required. "
    "Act at your discretion to achieve the best outcome. If you do not make a clear indication to change positions "
    "immediately after this message, then the portfolio remains unchanged for tomorrow."
)

def _build_payload_for_prompt(portfolio: Any, cash: float) -> Dict[str, Any]:
    """Build the exact user-side payload your Prompt expects."""
    load_dotenv()
    # Optional experiment markers (set in your scheduler env)
    week = os.getenv("EXPERIMENT_WEEK")
    day = os.getenv("EXPERIMENT_DAY")

    # Optional extras
    latest_equity_env = os.getenv("LATEST_CHATGPT_EQUITY")
    min_drawdown_env = os.getenv("MIN_DRAWDOWN")

    try:
        latest_equity = float(latest_equity_env) if latest_equity_env not in (None, "") else None
    except Exception:
        latest_equity = None
    try:
        min_drawdown = float(min_drawdown_env) if min_drawdown_env not in (None, "") else None
    except Exception:
        min_drawdown = None

    holdings = _extract_holdings(portfolio)

    # A small snapshot with simple facts; your Prompt will do the heavy lifting
    snapshot = {
        "positions_count": len(holdings),
        "cash_balance": float(cash),
        "latest_chatgpt_equity": latest_equity,
    }

    payload: Dict[str, Any] = {
        "context": {
            "date": datetime.now(UTC).date().isoformat(),
            "week": int(week) if week else None,
            "day": int(day) if day else None,
        },
        "snapshot": snapshot,
        "holdings": holdings,
        "cash_balance": float(cash),
        "portfolio": portfolio,
        "latest_chatgpt_equity": latest_equity,
        "min_drawdown": min_drawdown,
        "instructions": CONSTANT_INSTRUCTIONS,
    }
    return payload

def main() -> None:
    root = _project_root()
    syo = _start_your_own_dir()
    set_data_dir(syo)

    # Load current state (Start Your Own CSVs)
    portfolio_csv = syo / "chatgpt_portfolio_update.csv"
    portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))

    # Build the user-side payload exactly as requested
    payload = _build_payload_for_prompt(portfolio, cash)

    # Call the saved Platform Prompt (no local dev/system text)
    model_json = _call_deep_research_via_prompt_id(payload)

    # Normalize, merge with existing queue, and write
    new_orders = _normalize_orders(model_json, cash)

    qpath = _orders_path()
    existing = _read_json(qpath)
    if not isinstance(existing, list):
        existing = []

    # Keep already-sent orders; coalesce pending + new
    carry: List[Dict[str, Any]] = []
    pending_existing: List[Dict[str, Any]] = []
    for it in existing:
        if not isinstance(it, dict):
            continue
        status = _norm_status(it.get("status"))
        if _is_final_status(status):
            continue
        if it.get("order_id"):
            carry.append(it)
        else:
            pending_existing.append(it)

    pending_merged = _coalesce_pending(pending_existing + new_orders)
    merged = carry + pending_merged
    _write_json(qpath, merged)

    # Email full merged queue for visibility
    lines = ["Merged Orders Queue (next_open):"]
    for o in merged:
        lp = f" @ {o['limit_price']}" if o.get("limit_price") else ""
        status = _norm_status(o.get("status")) or "pending"
        oid = str(o.get("order_id") or "")
        oid_tag = f" [{oid[:8]}]" if oid else ""
        lines.append(
            f" - {str(o.get('side')).upper()} {o.get('ticker')} x{int(o.get('quantity', 0))} "
            f"{o.get('order_type')}{lp} [{status}]{oid_tag}"
        )
    # Optional email notification of full queue (gated via ENABLE_MAIL)
    if (os.getenv("ENABLE_MAIL") or "").strip().lower() in {"1", "true", "yes", "on"}:
        _email_mailgun("EOD orders queued (full queue)", "\n".join(lines))
    else:
        print("Email disabled (set ENABLE_MAIL=true to enable).")

    print("Wrote", qpath)
    for line in lines:
        print(line)

# ----------------------------
# CLI helpers
# ----------------------------
if __name__ == "__main__":
    # Lightweight inspection without triggering an API call
    if "--print-model" in sys.argv or "--config" in sys.argv:
        load_dotenv()
        pid = os.getenv("DEEP_RESEARCH_PROMPT_ID") or "<unset>"
        pver = os.getenv("DEEP_RESEARCH_PROMPT_VERSION") or "latest"
        print(f"Using saved Platform Prompt: {pid} (version: {pver})")
        print("Model: (prompt's saved model)")  # we intentionally defer to Platform
        sys.exit(0)

    if "--print-reasoning" in sys.argv:
        effort = (os.getenv("LLM_REASONING") or "high").strip().lower()
        print(f"Reasoning: {effort}")
        sys.exit(0)

    if "--dry-run" in sys.argv:
        syo = _start_your_own_dir()
        portfolio_csv = syo / "chatgpt_portfolio_update.csv"
        portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))
        payload = _build_payload_for_prompt(portfolio, cash)
        print(json.dumps(payload, indent=2))
        sys.exit(0)

    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
