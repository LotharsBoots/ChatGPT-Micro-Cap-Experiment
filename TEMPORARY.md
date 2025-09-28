# Temporary / Work That Needs To Be Done

This document captures the current state, architecture, and step-by-step tasks for the Schwab migration and daily automation. Use it as the single source of truth for new sessions.

## Current State (Schwab)

- SchwabAdapter implemented (auto-refresh; account/positions; submit/list/cancel; token auto-loader from `SCHWAB_TOKEN_JSON`).
- Executor updated for Schwab:
  - Submits DAY MARKET/LIMIT during 09:30 ET window (configurable by env seconds).
  - Pre-warms auth, retries, 10‑min fill polling.
  - CSV fallback: seeds or creates minimal CSV inside container if missing.
- Reconcile script added: `reconcile_orders.py` (post‑open status + CSV updates, idempotent).
- Deployed and tested in ECS using image tag `microcap:Schwab`.

## Architecture (bones)

1) Queue generator(s):
   - `queue_daily.py` (Mon–Thu) → writes `Start Your Own/orders_queue.json`.
   - `queue_eod.py` (Fri) → same output; different cadence.
2) Morning executor (`executor_morning.py`):
   - Runs at ~09:29:58 ET; submits queued orders during the 09:30:00 opening window.
   - Polls fills for ~10 minutes; updates queue + CSVs for fills.
3) Reconcile (`reconcile_orders.py`):
   - Runs ~09:40 ET; upgrades statuses and applies late fills to CSVs.
4) Tokens/OAuth:
   - Weekly re‑auth via Cloudflare Worker callback; tokens stored to Secrets (SCHWAB_TOKEN_JSON) and auto‑loaded by adapter.

## How to re‑integrate Alpaca (rollback)

- Keep Alpaca code in repo (do not delete).
- Switch selection in the executor to `AlpacaAdapter` and rebuild the image.
- Restore Alpaca env vars and schedules (same three jobs) if needed. No business-logic changes required.

## REQUIRED Steps (do these first)

1) Executor schedule (EventBridge)
   - Time zone: US/Eastern; 09:29:58 Mon–Fri.
   - Target: ECS_RunTask → Task definition: latest `microcap-executor-morning` (image `microcap:Schwab`).
   - Task override JSON (container name is `app`):

```json
{
  "containerOverrides": [
    {
      "name": "app",
      "command": ["/usr/local/bin/python", "executor_morning.py"],
      "environment": [
        { "name": "BROKER", "value": "schwab" },
        { "name": "SCHWAB_ACCOUNT_ID", "value": "81718749" },
        { "name": "SCHWAB_REDIRECT_URI", "value": "https://schwab-oauth-worker.lotharsboots.workers.dev/callback" },
        { "name": "SCHWAB_OAUTH_TOKEN_PATH", "value": "tokens/schwab_token.json" },
        { "name": "SUBMISSION_MODE", "value": "timed" },
        { "name": "OPENING_SUBMIT_WINDOW_START_SEC", "value": "-5" },
        { "name": "OPENING_SUBMIT_WINDOW_END_SEC", "value": "5" },
        { "name": "ENABLE_MAIL", "value": "false" }
      ]
    }
  ]
}
```

2) Reconcile schedule (EventBridge)
   - Time zone: US/Eastern; ~09:40:00 Mon–Fri.
   - Target: ECS_RunTask → Task definition: latest `microcap-executor-morning` (image `microcap:Schwab`).
   - Task override JSON:

```json
{
  "containerOverrides": [
    { "name": "app", "command": ["/usr/local/bin/python", "reconcile_orders.py"] }
  ]
}
```

3) Keep both queue schedules enabled
   - `microcap-queue-daily` (Mon–Thu after close).
   - `microcap-queue-eod` (Fri after close).

4) Secrets and env (Task Definition level)
   - Secrets (ValueFrom): `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_TOKEN_JSON`.
   - Env (Value): `BROKER=schwab`, `SCHWAB_ACCOUNT_ID=81718749`, `SCHWAB_REDIRECT_URI`, `SCHWAB_OAUTH_TOKEN_PATH=tokens/schwab_token.json`, `SUBMISSION_MODE=timed`, `OPENING_SUBMIT_WINDOW_START_SEC=-5`, `OPENING_SUBMIT_WINDOW_END_SEC=5`.

5) Image
   - ECR: `780372467371.dkr.ecr.us-east-1.amazonaws.com/microcap:Schwab` (fresh build/push when code changes).

### Status checklist (today)

- [x] Executor schedule updated to latest task rev and tested (outside-window message confirmed).
- [ ] Reconcile schedule created and Enabled (target `reconcile_orders.py`).
- [x] Keep queue_daily and queue_eod Enabled.
- [x] ECR image `microcap:Schwab` built and in use by ECS.

## RECOMMENDED Steps (next)

1) Market-hours/holidays
   - Add `pandas_market_calendars` and a helper `is_market_day_today()`; use it in queue/executor to skip holidays.

2) CI/CD (GitHub Actions)
   - Fix OIDC trust policy for role `microcap-github-oidc-role` (branch `schwab`), set repo variables `AWS_REGION`, `AWS_ACCOUNT_ID`, `ECR_REPO`.
   - Build/push on branch `schwab` to avoid manual ECR pushes.

3) Pre‑refresh just before open
   - Tiny schedule at ~09:29 ET calling `adapter.get_account()` to rotate access_token if near expiry.

4) Optional pause guard
   - Add code: if `BROKER=disabled` then executor exits early (plus scheduler switch).

## Roadmap (long‑term autonomy & resilience)

1) Monitoring & alerts
   - CloudWatch alarms on task failures and error logs; optional SNS/email.

2) Durable order audit
   - Append broker request/response metadata (sans secrets) to a rolling S3 log for traceability.

3) Metrics
   - Daily fills count, submitted vs filled ratio, average slippage, error rates.

4) Token lifecycle
   - Weekly re‑auth reminder (24h before expiry); secure rotation SOP; automate update of `schwab-token-json` secret via a small internal admin page if desired.

5) Market data parity
   - Standardize symbol normalization and split/CA handling in positions reconciliation.

6) CI/CD hardening
   - Branch protections; build provenance; image scanning; tag with date+sha (e.g., `Schwab-YYYYMMDD-<shortsha>`); pin ECS to immutable tags.

7) Rollback plan
   - Keep `:Schwab-prev` image tag; one-click revert of task definition to previous digest.

8) Optional dual‑broker route (future)
   - Per‑order `broker` tag; aggregated positions; idempotency across brokers. Only if you decide to split flow in the future.

## Weekly Re‑Auth (summary)

1) Ensure Schwab Developer Portal callback = your Worker `/callback`.
2) Open `https://schwab-oauth-worker.<your-subdomain>.workers.dev/` → Start authorization → approve.
3) Worker exchanges code; shows tokens JSON.
4) Update Secrets Manager `schwab-token-json` with the new JSON.
5) Executor auto‑loads tokens via `SCHWAB_TOKEN_JSON` on next run.

## How to Pause Automation

- Disable EventBridge schedules (executor, reconcile, daily/eod); or
- Temporarily set `BROKER=disabled` env for executor (if guard is added); or
- Point ECS task definition to a safe image tag (e.g., `:hold`).

## Quick Runbook

1) Change code → `docker build --no-cache -t <acct>.dkr.ecr.<region>.amazonaws.com/microcap:Schwab .` → push.
2) Create new task definition revision referencing `:Schwab`.
3) Confirm executor schedule target uses latest task revision; same for reconcile.
4) Verify logs in CloudWatch:
   - Executor: “Submitting X queued orders (opening window)…” or “Outside opening submission window…”.
   - Reconcile: “Reconcile complete. Updated queue: … Applied fills: …”.


