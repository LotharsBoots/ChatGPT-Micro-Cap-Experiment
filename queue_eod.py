r"""End-of-day (EOD) automation: call OpenAI, produce orders_queue.json, email summary.

Behavior:
- Loads portfolio state from Start Your Own CSVs using existing trading_script helpers
- Calls OpenAI with a strict-JSON prompt (no prose) to get buy/sell intents
- Validates & normalizes into a unified order list for the next market open
- Writes Start Your Own\orders_queue.json with idempotency (no duplicates)
- Sends an email summary via Mailgun if configured

This keeps code simple and relies on your existing CSV formats and helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import hashlib
import json
import os
import re
import sys

from dotenv import load_dotenv

from trading_script import (
    load_latest_portfolio_state,
    set_data_dir,
)


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


def _normalize_orders(model_json: Dict[str, Any], portfolio_cash: float) -> List[Dict[str, Any]]:
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
            # very rough placeholder sizing using cash only; execution calc is done later
            # ensure at least 1 share attempt if any positive pct
            approx_price = 1.0  # unknown now; executor will validate final qty
            qty = max(1, int((portfolio_cash * pct) // approx_price))
        if qty is None or qty <= 0:
            raise ValueError("quantity/percent must produce positive qty")
        if order_type not in {"MOO", "LOO"}:
            raise ValueError("order_type must be MOO or LOO for next_open flow")
        if order_type == "LOO" and (limit_price is None or float(limit_price) <= 0):
            raise ValueError("LOO requires limit_price > 0")
        oid = _hash_id([ticker, side, str(qty), order_type, str(limit_price or "" )])
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
        raw = model_json.get(side_key) or []
        if not isinstance(raw, list):
            continue
        for it in raw:
            if not isinstance(it, dict):
                continue
            add("buy" if side_key == "buy" else "sell", it)
    return orders


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


def _resolve_model_and_base() -> tuple[str, str | None]:
    """Determine the OpenAI model and optional base URL from env.

    Prefers OPENAI_MODEL, then LLM_MODEL, defaults to 'gpt-5'.
    """
    load_dotenv()
    model_name = os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or "gpt-5"
    base_url = os.getenv("OPENAI_BASE_URL")
    return model_name, base_url


def _call_openai_strict(prompt_payload: Dict[str, Any]) -> Dict[str, Any]:
    from openai import OpenAI
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY missing in .env")
    # Allow model/base URL configuration via env for flexibility
    model_name, base_url = _resolve_model_and_base()
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    print(f"Using OpenAI model: {model_name}{' via ' + base_url if base_url else ''}")
    # Load system prompt from file or env override for long-term maintainability
    prompt_path = os.getenv("EOD_SYSTEM_PROMPT_FILE") or str(_project_root() / "templates" / "eod_system.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as fh:
            system = fh.read().strip()
    except Exception:
        system = (
            "You are a trading assistant. Return ONLY valid JSON with keys 'buy' and 'sell'. "
            "Schema: {buy:[{ticker,percent|quantity,order_type(MOO|LOO),limit_price?}], "
            "sell:[{ticker,percent|quantity,order_type(MOO|LOO),limit_price?}]}. No prose."
        )
    # Determine reasoning setting (default high), only used for GPT-5 path
    reasoning_effort = (os.getenv("LLM_REASONING") or "high").strip().lower()
    is_gpt5 = model_name.lower().startswith("gpt-5")

    try:
        if is_gpt5:
            # Use Responses API with reasoning for GPT-5
            resp = client.responses.create(
                model=model_name,
                reasoning={"effort": reasoning_effort},
                input=[
                    {"role": "developer", "content": [{"type": "input_text", "text": system}]},
                    {"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt_payload)}]},
                ],
            )
            # Prefer robust accessor
            try:
                content = resp.output_text  # type: ignore[attr-defined]
            except Exception:
                try:
                    content = resp.output[-1].content[0].text  # type: ignore[index]
                except Exception:
                    content = str(resp)
        else:
            # Legacy chat.completions path for non-GPT-5 models
            resp = client.chat.completions.create(
                model=model_name,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(prompt_payload)},
                ],
            )
            content = resp.choices[0].message.content if (resp and getattr(resp, "choices", None)) else "{}"
    except Exception as exc:
        # Safe fallback to a widely-available model to keep automation running
        fallback_model = "gpt-4o-mini"
        print(f"Warning: model '{model_name}' failed ({exc}). Falling back to '{fallback_model}'.")
        resp = client.chat.completions.create(
            model=fallback_model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt_payload)},
            ],
        )
        content = resp.choices[0].message.content if (resp and getattr(resp, "choices", None)) else "{}"
    # strip code fences if present
    content = content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content or "{}")


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


def main() -> None:
    root = _project_root()
    syo = _start_your_own_dir()
    set_data_dir(syo)

    # Load current state (Start Your Own CSVs)
    portfolio_csv = syo / "chatgpt_portfolio_update.csv"
    portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))

    # Build a compact payload the LLM can use (optionally include research context and guidance)
    payload = {
        "portfolio": portfolio,
        "cash": cash,
        "instruction": "Return ONLY JSON per schema with your buy/sell for next_open.",
    }
    # Optional: include text context from a folder if RESEARCH_DIR is set
    research_dir = os.getenv("RESEARCH_DIR")
    if research_dir:
        try:
            ctx_parts: List[str] = []
            p = Path(research_dir)
            for fp in sorted(p.glob("**/*.md"))[:20]:  # bound for safety
                try:
                    ctx_parts.append(fp.read_text(encoding="utf-8")[:8000])
                except Exception:
                    pass
            if ctx_parts:
                payload["research_context"] = "\n\n".join(ctx_parts)
        except Exception:
            pass

    # Optional: load a guidance prompt with preferences and order spec details
    guidance_path = os.getenv("GUIDANCE_FILE") or str(_project_root() / "templates" / "guidance.txt")
    try:
        with open(guidance_path, "r", encoding="utf-8") as fh:
            payload["guidance"] = fh.read().strip()
    except Exception:
        pass

    model_json = _call_openai_strict(payload)
    new_orders = _normalize_orders(model_json, cash)

    # Merge with existing queue, clean, and coalesce duplicates
    qpath = _orders_path()
    existing = _read_json(qpath)
    if not isinstance(existing, list):
        existing = []
    # Remove final states from existing to keep queue tidy
    carry: List[Dict[str, Any]] = []
    pending_existing: List[Dict[str, Any]] = []
    for it in existing:
        if not isinstance(it, dict):
            continue
        status = _norm_status(it.get("status"))
        if _is_final_status(status):
            # drop filled/cancelled
            continue
        # Keep items that were already sent to broker (have order_id)
        if it.get("order_id"):
            carry.append(it)
        else:
            pending_existing.append(it)

    # Coalesce all pending intents (existing + new)
    pending_merged = _coalesce_pending(pending_existing + new_orders)
    merged = carry + pending_merged
    _write_json(qpath, merged)

    # Email full merged queue for complete visibility
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
    _email_mailgun("EOD orders queued (full queue)", "\n".join(lines))

    print("Wrote", qpath)
    for line in lines:
        print(line)


if __name__ == "__main__":
    # Lightweight config inspection without triggering an API call
    if "--print-model" in sys.argv or "--config" in sys.argv:
        name, base = _resolve_model_and_base()
        print(f"Model: {name}")
        if base:
            print(f"Base URL: {base}")
        sys.exit(0)
    if "--print-prompt" in sys.argv:
        prompt_path = os.getenv("EOD_SYSTEM_PROMPT_FILE") or str(_project_root() / "templates" / "eod_system.txt")
        try:
            print(Path(prompt_path).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read prompt at {prompt_path}: {exc}")
        sys.exit(0)
    if "--print-reasoning" in sys.argv:
        name, _ = _resolve_model_and_base()
        effort = (os.getenv("LLM_REASONING") or "high").strip().lower()
        print(f"Model: {name}")
        print(f"Reasoning: {effort}")
        sys.exit(0)
    if "--print-guidance" in sys.argv:
        gpath = os.getenv("GUIDANCE_FILE") or str(_project_root() / "templates" / "guidance.txt")
        try:
            print(Path(gpath).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read guidance at {gpath}: {exc}")
        sys.exit(0)
    if "--dry-run" in sys.argv:
        # Build the SAME payload as main() (no API call) and print it fully
        syo = _start_your_own_dir()
        portfolio_csv = syo / "chatgpt_portfolio_update.csv"
        portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))
        payload = {
            "portfolio": portfolio,
            "cash": cash,
            "instruction": "Return ONLY JSON per schema with your buy/sell for next_open.",
        }
        # Optional research context
        research_dir = os.getenv("RESEARCH_DIR")
        if research_dir:
            try:
                ctx_parts: List[str] = []
                p = Path(research_dir)
                for fp in sorted(p.glob("**/*.md"))[:20]:
                    try:
                        ctx_parts.append(fp.read_text(encoding="utf-8")[:8000])
                    except Exception:
                        pass
                if ctx_parts:
                    payload["research_context"] = "\n\n".join(ctx_parts)
            except Exception:
                pass
        # Guidance text
        guidance_path = os.getenv("GUIDANCE_FILE") or str(_project_root() / "templates" / "guidance.txt")
        try:
            with open(guidance_path, "r", encoding="utf-8") as fh:
                payload["guidance"] = fh.read().strip()
        except Exception:
            pass
        print(json.dumps(payload))
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)


