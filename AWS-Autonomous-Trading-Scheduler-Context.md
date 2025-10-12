




## AWS Autonomous Trading Scheduler – Architecture and State (Bones Only)

### Goal
Fully autonomous, no-UI system that:
- Generates next‑session orders (Mon–Thu daily; Fri deep research).
- Submits OPG orders near market open.
- Persists portfolio/transaction state in the cloud.
- Runs without your PC, with retries and a DLQ for reliability.

---

## Repo and Key Scripts
- `queue_daily.py`: Mon–Thu order generation.
- `queue_eod.py`: Friday deep‑research order generation.
- `executor_morning.py`: Submits queued orders as OPG near the open.
- Data files (now in S3 under the same paths):
  - `Start Your Own/orders_queue.json`
  - `Start Your Own/chatgpt_trade_log.csv`
  - `Start Your Own/chatgpt_portfolio_update.csv`

No UI or endpoints; scheduled container runs that read → process → write.

---
## Quick Start (Zero Context)

1) One-time Day‑1 init (manual)
   - ECS → Clusters → `microcap-cluster` → Run new task
   - Task definition: `microcap-day-one` (latest), Launch type: FARGATE, Platform: LATEST
   - Container overrides → Environment → add `STARTING_CASH = <your amount>` (e.g., 3500)
   - Networking: default VPC, any two default subnets, default security group, Public IP = Enabled → Run task
   - Logs: CloudWatch `/ecs/microcap` prefix `day-one/`
   - Idempotent: if positions already exist, it logs “already initialized” and exits.

2) Autonomous schedules (no clicks after Day‑1)
   - Mon–Thu 4:00 PM ET: `microcap-queue-daily` → writes/merges `orders_queue.json`
   - Fri 4:00 PM ET: `microcap-queue-eod` → deep research merge
   - Weekdays 9:25 AM ET: `microcap-executor-morning` → submits OPG and updates CSVs

3) Where to look
   - S3 state: `s3://microcap-shared-state-780372467371/Start Your Own/`
   - Logs: CloudWatch group `/ecs/microcap` with prefixes `queue-daily`, `eod`, `executor`, `day-one`
   - Delivery failures: SQS `microcap-scheduler-dlq`

4) Reset to Day‑1 (optional)
   - Use the “Reset to Day‑1 (Archive + Rerun)” block in this doc (archives S3 state, clears it, prompts for `STARTING_CASH`, and runs Day‑1).

---

## Cloud Architecture (AWS)

- Container image
  - ECR repository: `microcap`
  - Tag used by tasks: `latest` (pinning recommended next)
  - Built via repo `Dockerfile` (`python:3.12-slim` base)

- Compute
  - ECS Fargate cluster: `microcap-cluster`
  - Task Definitions (one container each):
    - `microcap-queue-daily`
    - `microcap-executor-morning`
    - `microcap-queue-eod`
  - Task size: 0.25 vCPU, 0.5 GB
  - Public IP: Enabled
  - Networking: default VPC, 1–2 subnets

- Storage (shared state)
  - S3 bucket: `microcap-shared-state-<id>`
  - Prefix: `Start Your Own/`
  - Pattern each run:
    - Sync S3 → local folder
    - Run script
    - Sync local folder → S3

- Secrets
  - Secrets Manager secret: `microcap/runtime-env`
  - JSON keys (injected via ValueFrom in ECS):
    - `OPENAI_API_KEY`
    - `ALPACA_BASE_URL` (paper `https://paper-api.alpaca.markets` or live `https://api.alpaca.markets`)
    - `ALPACA_KEY_ID`
    - `ALPACA_SECRET_KEY`
  - Other env vars (plain values in ECS):
    - `BUCKET` = your S3 bucket
    - `DAILY_PROMPT_ID` (Mon–Thu)
    - `DEEP_RESEARCH_PROMPT_ID` (Fri)
    - Optional: `ENABLE_MAIL` (0/1) and `MAILGUN_*`

- Schedules (EventBridge Scheduler; Time zone: America/New_York; Flexible window: Off)
  - `microcap-queue-daily`: cron(0 16 ? * MON-THU *) → runs `queue_daily.py`
  - `microcap-queue-eod`:   cron(0 16 ? * FRI *)     → runs `queue_eod.py`
  - `microcap-executor-morning`: cron(25 9 ? * MON-FRI *) → runs `executor_morning.py`
  - Retry policy (all three): Max attempts = 3; Maximum event age = 2 hours
  - Dead‑letter queue (all three): SQS `microcap-scheduler-dlq`

- DLQ/Queue policy
  - SQS `microcap-scheduler-dlq` access policy allows:
    - Principal service `scheduler.amazonaws.com`
    - Action `sqs:SendMessage`
    - Condition `ArnLike` `aws:SourceArn = arn:aws:scheduler:us-east-1:<ACCOUNT_ID>:schedule/*`

- Logging
  - CloudWatch Logs group: `/ecs/microcap`
  - Stream prefixes: `queue-daily`, `executor`, `eod`
  - Tip: On a stream page, switch “UTC timezone” → “Local timezone”

- IAM (least‑privilege for tasks)
  - Task role: `microcap-task-role`
    - Managed: `AmazonECSTaskExecutionRolePolicy`
    - Inline: `secretsmanager:GetSecretValue` on your secret ARN `...:secret:microcap/runtime-env-XXXX*`
    - Custom S3 policy limited to your bucket/prefix:
      - `s3:ListBucket` with `s3:prefix = "Start Your Own/*"`
      - `s3:GetObject|PutObject|DeleteObject` on `.../Start Your Own/*`
  - Setup-time user had broader perms; for long‑term, keep human access narrow.

---

## Current Deployment (verified, bones only)

- Container image
  - ECR: `780372467371.dkr.ecr.us-east-1.amazonaws.com/microcap:latest`
- Compute
  - ECS Fargate cluster: `microcap-cluster`
  - Task Definitions (active)
    - `microcap-queue-daily` (rev 7) – includes `DAILY_PROMPT_ID`
    - `microcap-queue-eod` (rev 5) – includes `DEEP_RESEARCH_PROMPT_ID`
    - `microcap-executor-morning` (rev 1)
  - Networking: default VPC, two subnets, default security group, Public IP enabled
- Storage (shared state)
  - S3: `microcap-shared-state-780372467371/Start Your Own/`
  - Files: `orders_queue.json`, `chatgpt_trade_log.csv`, `chatgpt_portfolio_update.csv`
- Secrets (per-key secrets)
  - `microcap/openai-api-key`, `microcap/alpaca-base-url`, `microcap/alpaca-key-id`, `microcap/alpaca-secret-key`
  - Execution role `microcap-execution-role` allowed `secretsmanager:GetSecretValue` for those ARNs
- Scheduler (EventBridge, America/New_York)
  - `microcap-queue-daily`: cron(0 16 ? * MON-THU *)
  - `microcap-queue-eod`:   cron(0 16 ? * FRI *)
  - `microcap-executor-morning`: cron(25 9 ? * MON-FRI *)
  - Retry: 3 attempts; Max event age: 2 hours; Targets use latest task rev
  - DLQ attached: `arn:aws:sqs:us-east-1:780372467371:microcap-scheduler-dlq`
- Logs
  - CloudWatch group `/ecs/microcap` with prefixes `queue-daily`, `eod`, `executor`, `day-one`


---
## Durability & Retention (optional but helpful)

- CloudWatch Logs
  - The group `/ecs/microcap` can be left as “Never expire” for full history, or you can set a retention policy.
  - Example CLI to set 30‑day retention:
    - `aws logs put-retention-policy --log-group-name /ecs/microcap --retention-in-days 30`

- S3 State History (keep every update)
  - Enable Versioning on the state bucket to retain all historical versions of files under `Start Your Own/`:
    - `aws s3api put-bucket-versioning --bucket microcap-shared-state-780372467371 --versioning-configuration Status=Enabled`
    - Verify: `aws s3api get-bucket-versioning --bucket microcap-shared-state-780372467371`
  - Optional lifecycle to tier older versions (no delete), example skeleton:
    ```json
    {
      "Rules": [
        {
          "ID": "tier-old-versions",
          "Status": "Enabled",
          "NoncurrentVersionTransitions": [
            {"NoncurrentDays": 30, "StorageClass": "GLACIER"}
          ]
        }
      ]
    }
    ```
    - Apply: `aws s3api put-bucket-lifecycle-configuration --bucket microcap-shared-state-780372467371 --lifecycle-configuration file://lifecycle.json`

---
## Image Pinning (optional – release safety)

Tasks presently point to `:latest`. To pin a release:
1) Tag/push a version:
```
docker tag microcap:latest 780372467371.dkr.ecr.us-east-1.amazonaws.com/microcap:v1.0.0
docker push 780372467371.dkr.ecr.us-east-1.amazonaws.com/microcap:v1.0.0
```
2) Update task definitions to `.../microcap:v1.0.0` and re‑register.
3) Keep “Use latest revision” ON in schedules so new revisions take effect automatically.

---

## Data Flow (End-to-End)

1) Mon–Thu 4:00 PM ET (`microcap-queue-daily`)
   - Sync S3 → local
   - Run `queue_daily.py`
   - Write/merge `orders_queue.json` for next open
   - Sync local → S3

2) Fri 4:00 PM ET (`microcap-queue-eod`)
   - Sync S3 → local
   - Run `queue_eod.py` (deep research prompt)
   - Write/merge `orders_queue.json`
   - Sync local → S3

3) Weekdays 9:25 AM ET (`microcap-executor-morning`)
   - Sync S3 → local
   - Run `executor_morning.py` (submits OPG orders)
   - Update `chatgpt_trade_log.csv` and `chatgpt_portfolio_update.csv`
   - Sync local → S3
   - Outside OPG window (ET 7:00pm–9:28am) executor logs a skip and leaves queue intact

Idempotency:
- “accepted” items with `order_id` won’t re-submit.
- “pending” items are candidates for submission at next open.
- Unfilled LOO orders are logged; pipeline continues.

---

## Exact per-task Commands (array form)
- queue-daily
  - `sh`
  - `-lc`
  - `aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own && export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" && if [ -f Start\ Your\ Own/autotrade.json ]; then cp Start\ Your\ Own/autotrade.json ./autotrade.json; fi && rm -f Start\ Your\ Own/orders_queue.json || true && python queue_daily.py && aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT`
- queue-eod
  - `sh`
  - `-lc`
  - `aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own && export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" && if [ -f Start\ Your\ Own/autotrade.json ]; then cp Start\ Your\ Own/autotrade.json ./autotrade.json; fi && rm -f Start\ Your\ Own/orders_queue.json || true && python queue_eod.py && aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT`
- executor
  - `sh`
  - `-lc`
  - `aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own && export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" && python executor_morning.py && aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT`
- reconcile
  - `sh`
  - `-lc`
  - `aws s3 sync s3://$BUCKET/Start\ Your\ Own/$ACCOUNT Start\ Your\ Own && export SCHWAB_ACCOUNT_ID="$(tr -d '\r\n' < 'Start Your Own/account_id.txt')" && python reconcile_orders.py && aws s3 sync Start\ Your\ Own s3://$BUCKET/Start\ Your\ Own/$ACCOUNT`

---

## Secrets ValueFrom ARN Format (critical)
- Preferred (current): one secret per key; reference ARN directly (no `:KEY::` suffix):
  - `valueFrom = arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:microcap/openai-api-key-XXXX`
  - `valueFrom = arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:microcap/alpaca-base-url-XXXX`
  - `valueFrom = arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:microcap/alpaca-key-id-XXXX`
  - `valueFrom = arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:microcap/alpaca-secret-key-XXXX`
  - Execution role must allow `secretsmanager:GetSecretValue` for those ARNs

  Alternative (supported): single JSON secret with `...:KEY::` suffixes on `valueFrom`.

---

## Prompt Output Contracts (strict)
- Daily (Mon–Thu) and Deep‑Research (Fri) must output strict JSON only:
```json
{"buy":[{"ticker":"SPY","order_type":"MOO","quantity":1}], "sell":[]}
```
- Order rules:
  - `ticker`: `[A-Z.\-]+`
  - `order_type`: `"MOO"` or `"LOO"`
  - Either `quantity` (int > 0) OR `percent` (0..1)
  - `limit_price` required iff `order_type == "LOO"`
  - If none: `{"buy":[],"sell":[]}`

---

## Verification

- From ECS
  - Task Definitions → Actions → Run task (Fargate, pick subnets, Public IP = Enabled)
  - Task → Containers → “View in CloudWatch” to open the exact stream

- From S3
  - S3 → bucket → `Start Your Own/` → check “Last modified”
  - CLI: `aws s3api head-object --bucket <bucket> --key "Start Your Own/orders_queue.json" --query LastModified --output text`

- From Scheduler
  - Schedules list → checkbox → Actions → Run now (only visible on list view)

---

## Reliability Notes (retries/DLQ)
- Scheduler retries only the delivery of the invocation to ECS (the “start task” call).
- If the ECS task starts and your code fails, Scheduler won’t retry that run.
- That’s why CloudWatch alarms (task failures, log error patterns) are the next priority.
- DLQ triage: SQS `microcap-scheduler-dlq` → Messages → View; payload includes schedule ARN, error, and time.

---

## Networking (egress)
- Tasks must reach OpenAI/Alpaca.
- Either enable Auto‑assign Public IP = Enabled (public subnets), or run in private subnets with a NAT gateway.
- Symptoms of missing egress: DNS errors/timeouts in logs.

---

## Timezone
- Schedules use `America/New_York`.
- CloudWatch stream pages can switch to Local timezone; list views often show UTC.

---

## What’s Already Done
- Image built/pushed to ECR `microcap:latest`
- ECS cluster and three task definitions created
- Secrets in Secrets Manager; injected via ValueFrom
- S3 shared state wired with sync in/out
- Three schedules created (ET timezone)
- Scheduler reliability: retries (3) + DLQ (SQS)
- SQS policy allows Scheduler to send failures
- SNS topic created: microcap-alerts; email subscription confirmed
- EventBridge rule `microcap-ecs-task-fail`: ECS Task State Change STOPPED → SNS `microcap-alerts`
- CloudWatch alarm `microcap-dlq-has-messages`: SQS `ApproximateNumberOfMessagesVisible >= 1` on `microcap-scheduler-dlq` → SNS `microcap-alerts`
- Strict JSON output enforced in both prompts
- Verified end‑to‑end: daily/EOD queue wrote `orders_queue.json`; executor submitted OPG; CSVs updated

---

## Common Pitfalls (and fixes)
- No “Run now”: go to schedules list, select row → Actions → Run now
- Subnets list blank: paste subnet IDs from VPC; confirm region
- ECR push auth: login to ECR; ensure ECR access policy
- ECS cluster creation error: create `AWSServiceRoleForECS`
- Secrets AccessDenied: add inline policy on secret ARN to task role
- Non‑JSON model output: enforce prompt output contract

---

## Ops Runbook
- Check failures:
  - ECS → Tasks → “Stopped reason” and exit codes
  - CloudWatch logs `/ecs/microcap` by prefix (daily/executor/eod)
  - DLQ: SQS `microcap-scheduler-dlq`

- Force run:
  - ECS Task Definitions → Actions → Run task

- Update code:
  - Build/push a new image → update task definitions or pin a tag

---

## Recommended Next Actions (Architecture First)
1) Alerts on failures
   - SNS topic (e.g., `microcap-alerts`) + email subscription
   - CloudWatch alarms:
     - ECS task failures (exit code != 0 / stopped reason)
     - Log pattern alarms (e.g., “Deep Research returned non‑JSON”)
   - Route alarms to SNS

2) Image version pinning
   - Tag releases (e.g., `v1.0.0`) and update task defs/schedules to pinned tag

3) CI to ECR
   - GitHub Actions to build/push image on commit

4) Market‑clock gating
   - Pre-check Alpaca market clock; skip runs on holidays/closed days

5) Durability/housekeeping
   - S3 versioning + lifecycle
   - CloudWatch Logs retention (e.g., 14–30 days)
   - ECR lifecycle to keep last N images

6) IAM tightening for humans
   - Remove broad setup policies from CLI user; consider IAM Identity Center later

---

## Cron and Timezone Reference
- Daily: `0 16 ? * MON-THU *` (ET)
- EOD:   `0 16 ? * FRI *` (ET)
- Executor: `25 9 ? * MON-FRI *` (ET)

---

## Success Criteria
- 4:00 PM ET queues write/merge `orders_queue.json` in S3 (Mon–Fri)
- 9:25 AM ET executor submits OPG; CSVs update in S3
- Transient failures retried; persistent failures end in DLQ and (after alerts) notify you
- No manual PC involvement; secrets in Secrets Manager; logs in CloudWatch

---

## Recent Enhancements (Sep 2025)

- GitHub OIDC role created: `microcap-github-oidc-role` (trust restricted to repo `LotharsBoots/ChatGPT-Micro-Cap-Experiment`, branch `API-Brokerage`).
- GitHub Actions workflows in repo:
  - `build-push`: builds Docker image and pushes to ECR on `API-Brokerage` (and manual dispatch). Docker login targets the ECR REGISTRY host; image is pushed as `:latest`.
  - `run-day-one`: manually runs Day‑1 with `STARTING_CASH`, finds latest `microcap-day-one` task‑def, detects default VPC/subnets/SG, and runs one Fargate task.
  - `reset-day-one`: archives S3 `Start Your Own/` to `Archive/day-one-<timestamp>/`, clears it, then dispatches `run-day-one` automatically.
- ECS task definition `microcap-day-one` added:
  - Image: `780372467371.dkr.ecr.us-east-1.amazonaws.com/microcap:latest`
  - Command: `sh -lc "aws s3 sync s3://$BUCKET/Start Your Own 'Start Your Own' && python day_one_bootstrap.py && aws s3 sync 'Start Your Own' s3://$BUCKET/Start Your Own"`
  - Env: `BUCKET=microcap-shared-state-780372467371`
- IAM (tight additions for workflows):
  - EC2 Describe read‑only for VPC/Subnets/SecurityGroups (Day‑1 workflow network discovery).
  - S3 RW on `Start Your Own/*` and `Archive/*` plus ListBucket with prefix (reset workflow).
  - GitHub Actions permission `actions: write` on reset workflow to allow dispatching `run-day-one`.

---

## Holiday / Market‑Clock Gating (Planned)

- Daily (`queue_daily.py`) and EOD (`queue_eod.py`) will pre‑check the Alpaca market clock and exit 0 with a clear log when the market is closed/holiday.
- Executor already gates to Alpaca OPG window (ET 7:00pm–9:28am).
- Outcome: no prompts or S3 writes on holidays; schedules still run and log a skip.

---

## GitHub Actions Details (Variables / Permissions)

- Repository Variables required:
  - `AWS_REGION=us-east-1`, `AWS_ACCOUNT_ID=780372467371`, `ECR_REPO=microcap`, `ECS_CLUSTER=microcap-cluster`, `TASK_FAMILY_DAY_ONE=microcap-day-one`
- Repository Secret:
  - `AWS_ROLE_TO_ASSUME` = IAM role ARN for OIDC (above)
- Build notes:
  - Use REGISTRY host for docker login (not image path). Example: `${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com`
  - If newline/CR sneaks in from variables, compute REGISTRY in a step and strip `\r`, then export via `$GITHUB_ENV`.
- Reset workflow note:
  - Needs `permissions: actions: write` to dispatch `run-day-one` with `gh workflow run`.

---

## Ops Cheatsheet (Quick Verification)

- List S3 archives:
  - `aws s3 ls s3://microcap-shared-state-780372467371/Archive/`
- Check fresh CSV timestamps (no download):
  - `aws s3api head-object --bucket microcap-shared-state-780372467371 --key "Start Your Own/chatgpt_portfolio_update.csv" --query LastModified --output text`
  - `aws s3api head-object --bucket microcap-shared-state-780372467371 --key "Start Your Own/chatgpt_trade_log.csv" --query LastModified --output text`
- Read orders queue inline:
  - `aws s3 cp "s3://microcap-shared-state-780372467371/Start Your Own/orders_queue.json" -`

---

## Known Pitfalls (and fixes)

- Docker login failing with malformed URL: login to REGISTRY host, not IMAGE; compute REGISTRY in a step and strip CRLF before `docker login`.
- `UnauthorizedOperation` in Day‑1 workflow while describing VPC/Subnets/SG: attach EC2 Describe read perms to the OIDC role.
- Reset workflow cannot trigger Day‑1 (`403 Resource not accessible by integration`): add `permissions: actions: write` in the workflow.
- New Python files added but ECS container can’t import them: re‑run `build-push` so ECR `:latest` includes the changes.