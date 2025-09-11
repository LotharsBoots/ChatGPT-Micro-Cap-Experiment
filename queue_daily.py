r"""Daily (Mon–Thu) automation: call OpenAI (Daily Prompt ID),
produce/merge orders_queue.json for next open, optional email.

Behavior:
- Loads portfolio state from Start Your Own CSVs using existing trading_script helpers
- Calls your saved OpenAI Platform Prompt by DAILY_PROMPT_ID with ONLY the user-side variables
- Requires STRICT JSON {buy:[], sell:[]} and validates/normalizes for next market open
- Writes/merges Start Your Own\orders_queue.json (idempotent, coalesces duplicates)
"""

from __future__ import annotations

from typing import Any, Dict, List
from datetime import datetime, UTC
import json
import os
import sys

from dotenv import load_dotenv

from trading_script import (
    load_latest_portfolio_state,
    set_data_dir,
)

# Reuse helpers from weekly queue script
from queue_eod import (
    _start_your_own_dir,  # type: ignore
    _orders_path,         # type: ignore
    _read_json,           # type: ignore
    _write_json,          # type: ignore
    _normalize_orders,    # type: ignore
    _coalesce_pending,    # type: ignore
    _norm_status,         # type: ignore
    _is_final_status,     # type: ignore
    _email_mailgun,       # type: ignore
)


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


def _call_daily_prompt(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Call saved Platform Prompt (Daily) by ID with ONLY a user message containing our JSON payload."""
    from openai import OpenAI

    load_dotenv()

    pid = os.getenv("DAILY_PROMPT_ID")
    if not pid:
        raise SystemExit("DAILY_PROMPT_ID missing in environment")

    pver = os.getenv("DAILY_PROMPT_VERSION")  # optional
    reasoning_effort = (os.getenv("LLM_REASONING") or "high").strip().lower()

    client = OpenAI()  # uses OPENAI_API_KEY from env

    prompt_obj: Dict[str, Any] = {"id": pid}
    if pver:
        prompt_obj["version"] = pver  # pins a specific version if you set it

    resp = client.responses.create(
        prompt=prompt_obj,
        input=[{
            "role": "user",
            "content": [{"type": "input_text", "text": json.dumps(payload)}]
        }],
        reasoning={"effort": reasoning_effort},
    )

    # Extract text robustly
    try:
        text = resp.output_text
    except Exception:
        try:
            text = resp.output[-1].content[0].text
        except Exception as e:
            raise RuntimeError(f"Could not extract text from response: {e}")

    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        nl = t.find("\n")
        if nl != -1 and "json" in t[:nl].lower():
            t = t[nl + 1:].strip()

    try:
        obj = json.loads(t or "{}")
    except Exception as e:
        raise ValueError(f"Daily Prompt returned non-JSON: {e}\nRAW:\n{(text or '')[:1200]}")

    if not isinstance(obj, dict) or "buy" not in obj or "sell" not in obj:
        raise ValueError("Daily Prompt output must be an object with 'buy' and 'sell' lists")
    return obj


def _build_daily_payload(portfolio: Any, cash: float) -> Dict[str, Any]:
    """Small, consistent payload for daily decisions."""
    holdings = _extract_holdings(portfolio)
    snapshot = {
        "positions_count": len(holdings),
        "cash_balance": float(cash),
    }
    return {
        "context": {
            "date": datetime.now(UTC).date().isoformat(),
            "window": "daily",
        },
        "snapshot": snapshot,
        "holdings": holdings,
        "cash_balance": float(cash),
        # Explicit instruction so the Prompt can enforce strict schema
        "instructions": (
            "Return ONLY strict JSON {buy:[], sell:[]}. Use MOO/LOO for next_open. "
            "Whole-share integers for quantity; or percent 0..1 to size from cash."
        ),
    }


def main() -> None:
    syo = _start_your_own_dir()
    set_data_dir(syo)

    portfolio_csv = syo / "chatgpt_portfolio_update.csv"
    portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))

    payload = _build_daily_payload(portfolio, cash)

    # Lightweight inspection mode
    if "--dry-run" in sys.argv:
        print(json.dumps(payload, indent=2))
        return

    model_json = _call_daily_prompt(payload)
    new_orders = _normalize_orders(model_json, cash)

    qpath = _orders_path()
    existing = _read_json(qpath)
    if not isinstance(existing, list):
        existing = []

    # Carry accepted/with order_id; coalesce pending + new
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

    # Optional email notification of full queue
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
    # Optional email notification of full queue (gated)
    if (os.getenv("ENABLE_MAIL") or "").strip().lower() in {"1", "true", "yes", "on"}:
        _email_mailgun("Daily orders queued (full queue)", "\n".join(lines))
    else:
        print("Email disabled (set ENABLE_MAIL=true to enable).")

    print("Wrote", qpath)
    for line in lines:
        print(line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)


