## Temporary / Working Notes — Full Autonomy Track

This file is a live checklist. Canonical architecture is in `AWS-Autonomous-Trading-Scheduler-Context.md`; A→Z flow is in `MICROCAP-Runbook.md`.

### Completed (baseline)
- ECS roles standardized on all families: Task role `microcap-task-role`, Execution role `microcap-execution-role`.
- `queue_daily` fixed to avoid quantity accumulation; new revisions deployed; EventBridge targets updated.
- `reconcile` uses S3 token file (`SCHWAB_OAUTH_TOKEN_PATH=Start Your Own/schwab_token.json`) and same Secrets Manager ARNs; refresh works.
- S3 state bucket: Versioning ENABLED + lifecycle (delete noncurrent after 30d; abort MPU after 7d; delete expired delete markers).
- DLQ alarm: `microcap-scheduler-dlq` ≥ 1 → SNS `microcap-alerts` (email). Runbook updated.
- Reset + Day‑1 task families working and documented.

### Next actions (this week)
1) Image pinning
   - Pin all task definitions to a tagged or digest image (stop using `:latest`). Update schedules to “Latest revision” + pinned image.
2) Risk knobs from S3 (operator-configurable)
   - Make BOTH `queue_daily` and `queue_eod` copy `Start Your Own/autotrade.json` → `./autotrade.json` before run.
   - Operator edits S3 file to set `per_trade_cash_pct` and `max_positions`.
3) Pre‑open auth probe
   - Add 09:29 ET schedule to run a lightweight Schwab `/accounts` call (forces refresh) before executor window.
4) App error alarms (CloudWatch Logs)
   - Metric filter on `/ecs/microcap`: `invalid_client|refresh_token failed|Traceback` → alarm → `microcap-alerts`.
5) Kill switch + paper/live flag
   - Secrets Manager key `microcap/runtime-switches` with `{ "enabled": true, "mode": "paper" }`.
   - All tasks read it and exit 0 if `enabled=false`; executor refuses live unless `mode="live"`.

### Medium term (next 2–4 weeks)
- Durable audit: append non-sensitive order request/response metadata to S3 as daily JSONL (idempotent).
- Pre‑trade risk: exposure caps (portfolio % and cash), per‑order notional cap, allowlist/time windows; log rejects.
- Observability: dashboard with metrics (order_attempts/success/reject, latency_ms, throttle_events) + task success ratio.
- CI/CD hardening: GitHub OIDC → ECR build/push; release tags `vYYYYMMDD-<sha>`; image scanning; ECR lifecycle retention.
- Holiday gating: ensure both daily and EOD use market calendar (already in daily; mirror to EOD if missing).

### Longer term
- Optional risk UI: S3‑hosted single‑page form with Cognito identity to edit `Start Your Own/autotrade.json` (no API server).
- Image digests in task defs for immutable rollbacks; blue/green via pinned revisions.
- Optional dual‑broker support toggle; idempotency keys across brokers.

### SOPs
- Weekly token rotation
  1) Cloudflare Worker → authorize → copy tokens JSON.
  2) Secrets Manager → update `SCHWAB_TOKEN_JSON` (one‑line JSON).
  3) Delete `Start Your Own/schwab_token.json` in S3 to force reseed next run.
- Reset‑to‑Day‑1
  1) Run `microcap-reset-only` (archives `Start Your Own/` under `Archive/`).
  2) Run `microcap-day-one` with `STARTING_CASH`.
- DLQ triage
  1) SQS `microcap-scheduler-dlq` → view message → find `scheduleArn` and reason.
  2) EventBridge schedule target → verify cluster, task family/revision, subnets/SG.
  3) ECS task def roles and Secrets/S3 perms; Run now; check `/ecs/microcap` logs.

### Alarms in place / planned
- IN PLACE: DLQ alarm (SQS visible ≥1).
- PLANNED: Log alarm (invalid_client/refresh failures/Traceback). Task STOPPED non‑zero alarm on ECS service events.

### Operator knobs (S3 `Start Your Own/autotrade.json`)
```json
{
  "per_trade_cash_pct": 0.05,   // 1.0%..7.0% supported; 7% hard cap in code
  "max_positions": 20           // used by day-one allocator; optional for queues if enforced later
}
```

### Known pitfalls (and fixes)
- `invalid_client` during refresh: ensure reconcile/executor use the SAME Secrets ARNs and `SCHWAB_REDIRECT_URI`; delete S3 token file to reseed from secret.
- S3 path quoting: always wrap `"Start Your Own"` in quotes in shell commands.
- Role drift: all families must use `microcap-task-role` + `microcap-execution-role`.


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


