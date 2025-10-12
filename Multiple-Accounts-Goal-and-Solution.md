## Multiple Accounts: Goal and Solution

### Goal
Run many Schwab brokerage accounts under one autonomous pipeline, with:
- Safe state isolation per account
- One-click (later: two‑click UI) switching of the active account
- Concurrent runs across multiple accounts for prompt/risk experiments
- No code rebuilds; minimal ECS edits after initial setup

### Core Pattern — Profiles via ACCOUNT
- One Secrets Manager entry: `microcap/runtime-profile` with key `ACCOUNT` → value like `acct1` (switch to `acct2`, `acct3`, … later).
- Per‑account S3 prefix: `s3://$BUCKET/Start Your Own/$ACCOUNT/`
  - `account_id.txt` (single line Schwab accountId)
  - `prompt_daily.txt` (optional override)
  - `prompt_eod.txt` (optional override)
  - `autotrade.json` (optional risk knobs)
  - `schwab_token.json` (optional if accounts use different logins)
- Task commands sync only that folder, run, then sync back.

### One‑Time ECS Setup (all four families)
For each task family (`microcap-queue-daily`, `microcap-queue-eod`, `microcap-executor-morning`, `microcap-reconcile`):
1) Map Secrets
   - `ACCOUNT` (ValueFrom = `arn:...:secret:microcap/runtime-profile` key `ACCOUNT`)
   - Shared Schwab app creds: `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_REDIRECT_URI`, `SCHWAB_TOKEN_JSON` (if same login). If different logins per account, place `schwab_token.json` in each account’s S3 folder and remove the shared secret.
2) Entry point: `sh, -lc`
3) Commands (use $ACCOUNT; quoting is shell‑safe):

Daily
```
aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own \
&& export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" \
&& if [ -f Start\ Your\ Own/autotrade.json ]; then cp Start\ Your\ Own/autotrade.json ./autotrade.json; fi \
&& rm -f Start\ Your\ Own/orders_queue.json || true \
&& python queue_daily.py \
&& aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT
```

EOD
```
aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own \
&& export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" \
&& if [ -f Start\ Your\ Own/autotrade.json ]; then cp Start\ Your\ Own/autotrade.json ./autotrade.json; fi \
&& rm -f Start\ Your\ Own/orders_queue.json || true \
&& python queue_eod.py \
&& aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT
```

Executor
```
aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own \
&& export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" \
&& python executor_morning.py \
&& aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT
```

Reconcile
```
aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own \
&& export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" \
&& python reconcile_orders.py \
&& aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT
```

### Prompt A/B/C per Account
- Daily/EOD read overrides per profile:
  - `Start Your Own/$ACCOUNT/prompt_daily.txt`
  - `Start Your Own/$ACCOUNT/prompt_eod.txt`
- Precedence (after we add hooks): ENV (`*_PROMPT_INSTRUCTIONS`) > S3 file > baked default.
- Run multiple EventBridge rules (one per account) targeting the same task family with a container override `ACCOUNT=acctN` (or rely on the shared secret if you don’t need concurrent runs).

### State Isolation
- Each account’s CSVs, orders, logs (S3) live under `Start Your Own/$ACCOUNT/`.
- Tokens: if accounts share the same Schwab login/app, one shared token is OK; otherwise keep a `schwab_token.json` per profile folder.

### Switching Accounts
- Single change: edit Secrets Manager `microcap/runtime-profile` → `ACCOUNT=acct2` (or override `ACCOUNT=acctN` on a per‑rule basis to run concurrent profiles).
- No task definition edits required after initial setup.

### Security & IAM
- Task role needs S3 List/Get/Put/Delete limited to `Start Your Own/*` and (optionally) `Archive/*`.
- Execution role needs `secretsmanager:GetSecretValue` for the referenced secrets.
- Optional UI: Cognito Identity Pool restricted to `PutObject/GetObject` on:
  - `Start Your Own/*/prompt_daily.txt`, `prompt_eod.txt`, `autotrade.json`, `account_id.txt` (per current account or all profiles if you prefer).

### Scheduling Patterns
- Single active profile: rules target families; `ACCOUNT` secret controls which profile all runs use.
- Concurrent profiles (experiments): duplicate each rule per account; add container override `ACCOUNT=acctN` in the target. We always clear and rewrite `orders_queue.json` each run (no merge behavior).

### Launcher Option (Single set of schedules for N accounts)
If you prefer 5 schedules total (not 5×N), add a tiny Lambda “launcher” that fans out to all accounts on each run.

Goal
- Keep task families as-is. Replace per-account schedules with 5 schedules total (daily, eod, executor, reconcile, probe). Each schedule invokes its launcher, which calls `ecs:RunTask` once per account with `ACCOUNT=acctN` override.

How it works
1) Store the account list in S3: `s3://<bucket>/config/accounts.json`, e.g. `["acct1","acct2"]`.
2) Lambda launcher (Python 3.11) reads that list and calls `ecs.run_task(...)` for each account:
   - `taskDefinition = microcap-<job>` (your existing families)
   - `containerOverrides: ACCOUNT=acctN`
   - `awsvpcConfiguration` uses your Fargate subnets/SGs.
3) Keep only 5 EventBridge schedules (one per job) → Target = the corresponding launcher Lambda.
4) Onboard/remove an account by updating `accounts.json` and `Start Your Own/acctN/account_id.txt` (no new schedules).

IAM for the launcher role
- `s3:GetObject` on `config/accounts.json`
- `ecs:RunTask`
- `iam:PassRole` for your ECS task/execution roles
- CloudWatch Logs permissions

Launcher environment (per job)
- `CLUSTER_ARN`, `SUBNETS`, `SECURITY_GROUPS`
- `TASK_FAMILY` (e.g., `microcap-queue-daily`)
- `CONTAINER_NAME=app`, `ACCOUNTS_S3=s3://.../config/accounts.json`

Cost/ops
- Lambda runs for milliseconds per schedule (pennies/month). No servers to manage. All in AWS.

Runbook (launcher pattern)
1) Create `config/accounts.json` with all account keys.
2) Ensure `Start Your Own/$ACCOUNT/account_id.txt` exists per account.
3) Deploy 5 launcher Lambdas (or one with `JOB` env) and wire the 5 schedules to them.
4) Add/remove accounts by editing `accounts.json`; the next run fans out automatically.

### Observability
- CloudWatch logs: `/ecs/microcap` (family prefixes). Keep explicit `[daily_llm_input]` / `[eod_llm_input]` gated logs to view exact payloads.
- DLQ alarm (already created) for “failed to start”; add ECS task‑failed EventBridge rule for tasks that start but crash.

### Rollback & Safety
- S3 versioning enabled with lifecycle (noncurrent delete after 30d). Restore any object or delete‑marker to revert state.
- Change `ACCOUNT` back to prior value to revert profile.
- Planned: kill switch in Secrets (`enabled=false`) read at start of each task to abort safely.

### Testing Checklist (per account)
1) Upload `account_id.txt` to `Start Your Own/$ACCOUNT/` (single line).
2) (Optional) Upload `prompt_daily.txt`, `prompt_eod.txt`, `autotrade.json`.
3) Set `ACCOUNT=$ACCOUNT` secret value.
4) Run `queue_daily` (or `--dry-run`): verify payload and `orders_queue.json` under that profile.
5) Run `executor_morning` (inside opening window) and `reconcile`: verify fills written to that profile’s CSV.

### Front‑End (Later)
- Two‑click account switch: UI updates the `ACCOUNT` secret or sets per‑rule overrides.
- Prompt editor: UI writes `prompt_daily.txt` / `prompt_eod.txt` to the active profile (or to all profiles).
- Risk knobs: UI writes `autotrade.json` per profile.


