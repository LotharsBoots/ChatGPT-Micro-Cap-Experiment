




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

Idempotency:
- “accepted” items with `order_id` won’t re-submit.
- “pending” items are candidates for submission at next open.
- Unfilled LOO orders are logged; pipeline continues.

---

## Exact per-task Commands (array form)
- queue-daily
  - `sh`
  - `-lc`
  - `aws s3 sync "s3://$BUCKET/Start Your Own" "Start Your Own" && python queue_daily.py && aws s3 sync "Start Your Own" "s3://$BUCKET/Start Your Own"`
- queue-eod
  - `sh`
  - `-lc`
  - `aws s3 sync "s3://$BUCKET/Start Your Own" "Start Your Own" && python queue_eod.py && aws s3 sync "Start Your Own" "s3://$BUCKET/Start Your Own"`
- executor
  - `sh`
  - `-lc`
  - `aws s3 sync "s3://$BUCKET/Start Your Own" "Start Your Own" && python executor_morning.py && aws s3 sync "Start Your Own" "s3://$BUCKET/Start Your Own"`

---

## Secrets ValueFrom ARN Format (critical)
- Use your exact secret ARN with JSON key and trailing `::`, e.g.:
  - `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:microcap/runtime-env-XXXX:OPENAI_API_KEY::`
  - Repeat for `ALPACA_BASE_URL`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`
- Task role must allow `secretsmanager:GetSecretValue` on `...:microcap/runtime-env-XXXX*`

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
