## Microcap Runbook (A→Z)

### End-to-end flow (high level)
```mermaid
flowchart TD
    R[Reset (microcap-reset-only)] --> D1[Day One (microcap-day-one)]
    D1 --> QD[Queue Daily (Mon–Thu 16:00 ET)]
    QD --> EX[Executor Morning (~09:30 ET OPG window)]
    EX --> RC[Reconcile (~09:40 ET)]
    RC --> QD
    RC -->|Fri| QE[Queue EOD (Fri 16:00 ET)] --> QD
```

---

### 1) Reset — `microcap-reset-only`
- Purpose: Archive then clear S3 state under `Start Your Own/` to give a clean slate.
- What it touches: Only S3 state. No changes to task defs, images, secrets, or logs.
- Why it matters: Prevents old orders/CSV snapshots from influencing new runs; keeps audit trail in `Archive/`.
- How it runs: Fargate task with `sh -lc` command that `aws s3 sync` to `Archive/day-one-<timestamp>/Start Your Own/` then `aws s3 rm --recursive` on `Start Your Own/`.
- Inputs: `BUCKET` env (uppercase). Task role needs least‑privilege S3 List/Get/Put/Delete for the bucket/prefix.
- Run from AWS Console: ECS → Task definitions → `microcap-reset-only` → Latest → Run task (no overrides). Verify CloudWatch `/ecs/microcap` (prefix `reset`) and S3 contents.

---

### 2) Day One — `microcap-day-one`
- Purpose: Initialize a fresh portfolio snapshot with Schwab settled cash; writes `chatgpt_portfolio_update.csv` (TOTAL row) and ensures `chatgpt_trade_log.csv` exists.
- Behavior: Sync S3 → local, export `SCHWAB_ACCOUNT_ID` from `account_id.txt`, run `day_one_simple.py`, then sync local → S3 (per account folder).
- Why it matters: Establishes the baseline CSVs from broker truth (cash-only, no positions) for the autonomous pipeline.
- Inputs: `BUCKET`, `ACCOUNT` (alias). Secrets: `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_TOKEN_JSON`, `SCHWAB_REDIRECT_URI`.
- Run from AWS Console: ECS → Task definitions → `microcap-day-one` → Latest → Run task → Container overrides → Environment: set `ACCOUNT=<alias>` → Run. Verify S3 `Start Your Own/$ACCOUNT/chatgpt_portfolio_update.csv` (today’s TOTAL row).

---

### 3) Queue Daily — `microcap-queue-daily`
- Purpose: Mon–Thu, generate next‑open orders into `Start Your Own/orders_queue.json`.
- Behavior: Reads portfolio/cash from CSVs, calls the Daily Prompt (strict JSON), sizes orders with caps, writes/merges queue. Duplicate pending accumulation is disabled (only today’s intents kept; already‑submitted with `order_id` are carried).
- Why it matters: Produces the deterministic intent list for the next open.
- Inputs: `BUCKET`, `DAILY_PROMPT_ID` (and optional `ENABLE_MAIL`).
- Run: EventBridge rule (16:00 ET) or ECS → Run task on `microcap-queue-daily`. Logs at `/ecs/microcap` (prefix `queue-daily`).

---

### 4) Executor Morning — `microcap-executor-morning`
- Purpose: Submit OPG‑timed DAY orders during the 09:30:00 ET window and record fills.
- Behavior: Pre‑warms auth; submits MOO/LOO within `[OPENING_SUBMIT_WINDOW_START_SEC, OPENING_SUBMIT_WINDOW_END_SEC]`; polls ~10 minutes; applies fills to CSVs. Unfilled limits remain pending/canceled by broker; never assumed filled.
- Inputs: `BUCKET`, `SUBMISSION_MODE=timed`, `OPENING_SUBMIT_WINDOW_START_SEC`, `OPENING_SUBMIT_WINDOW_END_SEC`.
- Run: EventBridge (around 09:29:58–09:30:05 ET) or ECS → Run task on `microcap-executor-morning`. Logs at `/ecs/microcap` (prefix `executor`).

---

### 5) Reconcile — `microcap-reconcile`
- Purpose: Post‑open reconciliation to upgrade statuses and apply confirmed fills to CSVs idempotently.
- Behavior: Reads queue, fetches broker orders, updates `status` to `filled/canceled`, and writes buys/sells into CSVs for filled only.
- Inputs: `BUCKET`.
- Run: EventBridge (~09:40 ET) or ECS → Run task on `microcap-reconcile`. Logs at `/ecs/microcap` (prefix `reconcile`).

---

### 6) Queue EOD — `microcap-queue-eod`
- Purpose: Friday deep‑research generation of next‑open orders (weekly rebalance path).
- Behavior: Reads portfolio/cash; calls the Deep Research Prompt; normalizes and writes/merges queue. We recommend clearing `orders_queue.json` before the Friday run to avoid week‑to‑week accumulation.
- Inputs: `BUCKET`, `DEEP_RESEARCH_PROMPT_ID` (and optional `ENABLE_MAIL`).
- Run: EventBridge (Fri 16:00 ET) or ECS → Run task on `microcap-queue-eod`. Logs at `/ecs/microcap` (prefix `eod`).

---

### Operational notes
- Least privilege: Task role must allow `s3:ListBucket` on the bucket with `s3:prefix` limited to `Start Your Own/*` and `Archive/*`, plus `GetObject/PutObject/DeleteObject` on those prefixes.
- Logging: All tasks write to CloudWatch group `/ecs/microcap` with stream prefixes matching the task.
- Schedules: EventBridge rules in America/New_York timezone; DLQ attached for start failures.
- Safety: Paper mode by default; executor never assumes fills; quantity accumulation across days is disabled in the daily queue. We always clear and rewrite `orders_queue.json` each run.

### Current implementation decisions (do not revert)
- Auth for Schwab callers (executor_morning, reconcile):
  - Use `SCHWAB_TOKEN_JSON` from Secrets Manager.
  - Do NOT set `SCHWAB_OAUTH_TOKEN_PATH` anywhere.
  - Ensure `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET` (ValueFrom secrets) and `SCHWAB_REDIRECT_URI=https://schwab-oauth-worker.lotharsboots.workers.dev/callback` are present.
- Profiles / ACCOUNT handling:
  - `ACCOUNT` in task definitions is `ValueFrom` the secret `microcap/runtime-profile:ACCOUNT::`.
  - Per-account isolation is `Start Your Own/$ACCOUNT/` with `account_id.txt` (short brokerage account number) in each folder.
  - Schedules (or rules) may override `ACCOUNT` with a plain string `acctN` to run multiple accounts concurrently.
- Queue generation:
  - `queue_daily` and `queue_eod` clear and rewrite `orders_queue.json` each run (no accumulation).
  - If a prior error status exists in the queue, regenerate the file; executor won’t resubmit non‑pending items.
- Testing executor outside 09:30 ET:
  - Temporarily widen the window via command override (inline env on python) for a one‑off run only.

### Schedules vs Rules
- You can use EventBridge Scheduler (schedules) or EventBridge Rules; both work.
- For multi‑account via schedules: create one schedule per job per account with `ACCOUNT=acctN` override.
- For fewer schedules (5 total), use the Lambda launcher (see Multiple-Accounts-Goal-and-Solution.md) to fan out `ecs:RunTask` for each account.

### Optional: Pre‑open probe (future)
- Purpose: Warm Schwab auth/HTTP a minute before open to reduce cold refresh risk at 09:30.
- Approach: Add a `PROBE=1` fast path to `reconcile_orders.py` that only calls `accounts/list` (no S3 writes) and exits.
- Scheduling: Keep a separate 09:29 ET schedule targeting the launcher with `{"taskFamily":"microcap-reconcile"}` plus container override `PROBE=1` (launcher can be extended to pass env on this schedule only).
- Status: Not required today; executor already pre‑warms auth. Enable later if you observe token refresh at 09:30 causing delays.

### Canonical task commands (use these, not older variants)
All commands are run with Entry point: `sh, -lc` and rely on `ACCOUNT` being set (either from the ValueFrom secret or a schedule override). For executor/reconcile, ensure `SCHWAB_TOKEN_JSON` is configured (Secrets Manager) and there is **no** `SCHWAB_OAUTH_TOKEN_PATH` anywhere.

- queue_daily
```
aws s3 sync "s3://$BUCKET/Start Your Own/$ACCOUNT" "Start Your Own" \
&& if [ -f "Start Your Own/autotrade.json" ]; then cp "Start Your Own/autotrade.json" ./autotrade.json; fi \
&& rm -f "Start Your Own/orders_queue.json" || true \
&& python queue_daily.py \
&& aws s3 sync "Start Your Own" "s3://$BUCKET/Start Your Own/$ACCOUNT"
```

- queue_eod
```
aws s3 sync "s3://$BUCKET/Start Your Own/$ACCOUNT" "Start Your Own" \
&& if [ -f "Start Your Own/autotrade.json" ]; then cp "Start Your Own/autotrade.json" ./autotrade.json; fi \
&& rm -f "Start Your Own/orders_queue.json" || true \
&& python queue_eod.py \
&& aws s3 sync "Start Your Own" "s3://$BUCKET/Start Your Own/$ACCOUNT"
```

- executor_morning (Schwab caller)
```
aws s3 sync "s3://$BUCKET/Start Your Own/$ACCOUNT" "Start Your Own" \
&& export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" \
&& python executor_morning.py \
&& aws s3 sync "Start Your Own" "s3://$BUCKET/Start Your Own/$ACCOUNT"
```

- reconcile (Schwab caller)
```
aws s3 sync "s3://$BUCKET/Start Your Own/$ACCOUNT" "Start Your Own" \
&& export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" \
&& python reconcile_orders.py \
&& aws s3 sync "Start Your Own" "s3://$BUCKET/Start Your Own/$ACCOUNT"
```



