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
- Purpose: Initialize a fresh portfolio snapshot and cash using `STARTING_CASH`; writes `chatgpt_portfolio_update.csv` and ensures CSVs exist.
- Behavior: Sync S3 → local (with delete), run `day_one_bootstrap.py`, then sync local → S3. Idempotent: if any positions already exist, it logs “already initialized.”
- Why it matters: Establishes the starting cash and baseline CSVs for the autonomous pipeline.
- Inputs: `BUCKET`, `STARTING_CASH` (set at run time). Uses public internet egress to price tickers.
- Run from AWS Console: ECS → Task definitions → `microcap-day-one` → Latest → Run task → Container overrides → Environment: set `STARTING_CASH` → Run. Verify S3 `Start Your Own/chatgpt_portfolio_update.csv` (today’s TOTAL row).

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
- Safety: Paper mode by default; executor never assumes fills; quantity accumulation across days is disabled in the daily queue.


