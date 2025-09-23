## AWS Console Runbook — Microcap Autonomous Trading (Full Context)

This runbook documents the complete AWS Console context for your fully cloud, no‑desktop pipeline. It explains what exists, why, how to operate it, and how to recover. Keep this next to `AWS-Autonomous-Trading-Scheduler-Context.md` and `CLOUD-100-Percent-Mode.md` for a single‑pane reference.

---

### 1) High‑Level Overview
- Architectural goal: 100% cloud operation. Builds: GitHub Actions. Runtime: ECS Fargate. State: S3. Secrets: Secrets Manager. Logs: CloudWatch. Scheduling: EventBridge Scheduler.
- Repo/branch: `LotharsBoots/ChatGPT-Micro-Cap-Experiment` (default `API-Brokerage`).
- AWS: Account `780372467371`, Region `us-east-1`.

Outcome: Daily and EOD generate/merge `orders_queue.json`; Executor submits OPG orders during the allowed window; CSVs and queue live in S3. No desktop required.

---

### 2) Resource Inventory (Current)
- ECR: `780372467371.dkr.ecr.us-east-1.amazonaws.com/microcap`
- ECS Cluster: `microcap-cluster`
- Task Definitions (Fargate, 0.25 vCPU / 0.5 GB):
  - `microcap-queue-daily` — runs `queue_daily.py`
  - `microcap-queue-eod` — runs `queue_eod.py`
  - `microcap-executor-morning` — runs `executor_morning.py`
  - `microcap-day-one` — runs `day_one_bootstrap.py` with S3 sync in/out
- EventBridge Schedules (ET):
  - Daily: Mon–Thu 16:00
  - EOD: Fri 16:00
  - Executor: Weekdays 09:25 (OPG window requirement)
  - Retries: 3; Max event age: 2h; DLQ: `microcap-scheduler-dlq`
- S3: `microcap-shared-state-780372467371/Start Your Own/` (queue + CSVs)
  - Archive path used by resets: `Archive/day-one-<timestamp>/`
- Logs: CloudWatch Logs group `/ecs/microcap` with stream prefixes `queue-daily`, `eod`, `executor`, `day-one`
- GitHub Actions (repo):
  - `build-push` (on push to `API-Brokerage`, or manual)
  - `run-day-one` (manual, `STARTING_CASH` input)
  - `reset-day-one` (manual; archives + clears S3, then dispatches `run-day-one`)

---

### 3) IAM (Identity and Access Management)

#### 3.1 GitHub OIDC role
- Role: `microcap-github-oidc-role`
- Trust policy: OIDC provider `token.actions.githubusercontent.com` with audience `sts.amazonaws.com` and `sub` restricted to repo `LotharsBoots/ChatGPT-Micro-Cap-Experiment:ref:refs/heads/API-Brokerage`.
- Policies (least privilege for CI):
  - ECR push (`GetAuthorizationToken`, `UploadLayer*`, `PutImage`, etc.) on repo `microcap`.
  - ECS reads (`DescribeTaskDefinition`, `ListTaskDefinitions`), and `RunTask` on task‑defs you invoke (Day‑1). `iam:PassRole` for the task execution role in use.
  - EC2 Describe read-only (`DescribeVpcs`, `DescribeSubnets`, `DescribeSecurityGroups`) to pick default VPC/subnets/SG.
  - S3 RW on `Start Your Own/*` and `Archive/*` plus `ListBucket` with prefixes for reset archiving.
  - For reset workflow: GitHub Actions job permission `permissions: actions: write` (workflow‑level) so it can dispatch `run-day-one`.

#### 3.2 Task Role vs Execution Role
- Execution role: lets ECS agent pull from ECR, inject Secrets Manager values (ValueFrom), and write CloudWatch logs.
- Task role: permissions for the running container (e.g., S3 read/write to the bucket/prefix, Secrets read if needed at runtime).

---

### 4) ECR (Elastic Container Registry)
- Repository: `microcap`.
- Login target MUST be the REGISTRY host, not the image path:
  - `${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com`
- Tagging: current tasks use `:latest` for auto‑pickup on next run. For pinned releases, push `:vX.Y.Z` and register new task‑def revisions.
- Troubleshooting: If login fails due to malformed URL, build REGISTRY in a bash step and strip CR `\r` before `docker login`.

---

### 5) ECS (Elastic Container Service)

#### 5.1 Cluster
- `microcap-cluster` (Fargate). Networking: default VPC, 1–2 default subnets, default security group, Public IP Enabled.

#### 5.2 Task Definitions
- `microcap-queue-daily` / `microcap-queue-eod` / `microcap-executor-morning`
  - Command pattern (container):
    - `sh -lc "aws s3 sync 's3://$BUCKET/Start Your Own' 'Start Your Own' && python <script>.py && aws s3 sync 'Start Your Own' 's3://$BUCKET/Start Your Own'"`
  - Env: `BUCKET=microcap-shared-state-780372467371` + any prompt IDs/flags as needed.
- `microcap-day-one`
  - Same pattern; runs `day_one_bootstrap.py`.
  - Idempotent: exits early if existing positions are detected in CSVs.

#### 5.3 Running a Task Manually (Console)
1) ECS → Task Definitions → select one → Actions → Run task
2) Cluster `microcap-cluster`, Launch type `FARGATE`, Platform LATEST
3) Networking: default VPC; pick any 1–2 default subnets; default security group; Public IP Enabled
4) Run task → View logs in CloudWatch

---

### 6) EventBridge Scheduler
- Mon–Thu 16:00 ET: `microcap-queue-daily`
- Fri 16:00 ET: `microcap-queue-eod`
- Weekdays 09:25 ET: `microcap-executor-morning`
- Retries: 3; Max event age: 2h; DLQ: `microcap-scheduler-dlq`
- Note: Schedules fire on holidays unless gated by app logic. Daily/EOD now include Alpaca calendar gating and will log a skip when market is closed.

---

### 7) S3 State
- Bucket: `microcap-shared-state-780372467371`
- Prefix: `Start Your Own/`
  - Files: `orders_queue.json`, `chatgpt_trade_log.csv`, `chatgpt_portfolio_update.csv`
- Archive: `Archive/day-one-<timestamp>/` (full snapshot of `Start Your Own/` when running reset)
- Optional durability:
  - Enable Versioning; optionally add lifecycle to tier old versions.

#### Useful CLI snippets (no download)
- List archives: `aws s3 ls s3://microcap-shared-state-780372467371/Archive/`
- Head timestamps:
  - `aws s3api head-object --bucket microcap-shared-state-780372467371 --key "Start Your Own/chatgpt_portfolio_update.csv" --query LastModified --output text`
  - `aws s3api head-object --bucket microcap-shared-state-780372467371 --key "Start Your Own/chatgpt_trade_log.csv" --query LastModified --output text`
- Read queue inline: `aws s3 cp "s3://microcap-shared-state-780372467371/Start Your Own/orders_queue.json" -`

---

### 8) Secrets Manager
- Per‑key secrets recommended; ValueFrom ARNs attached directly in the task definition.
- Required keys today: OpenAI, Alpaca (`OPENAI_API_KEY`, `ALPACA_BASE_URL`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`).
- Task execution role must allow `secretsmanager:GetSecretValue` for those ARNs.

---

### 9) GitHub Actions — How it Maps to AWS

#### Variables (repo → Settings → Secrets and variables → Actions → Variables)
- `AWS_REGION=us-east-1`
- `AWS_ACCOUNT_ID=780372467371`
- `ECR_REPO=microcap`
- `ECS_CLUSTER=microcap-cluster`
- `TASK_FAMILY_DAY_ONE=microcap-day-one`

#### Secret
- `AWS_ROLE_TO_ASSUME` (ARN of `microcap-github-oidc-role`)

#### Workflows
- `build-push`
  - Assumes role via OIDC → ensures ECR repo → logs into REGISTRY → builds → pushes `:latest`.
- `run-day-one`
  - Looks up latest `microcap-day-one` task‑def → finds default VPC/subnets/SG → runs Fargate with `STARTING_CASH`.
- `reset-day-one`
  - Archives S3 `Start Your Own/` to `Archive/day-one-<stamp>/` → clears → triggers `run-day-one` using `gh workflow run`.
  - Requires `permissions: actions: write` in the workflow to dispatch `run-day-one`.

---

### 10) Holiday / Market‑Clock Gating
- Executor: OPG window enforced programmatically (ET 7:00pm–9:28am). Outside the window, it logs a skip and leaves the queue intact.
- Daily/EOD: Alpaca calendar check added. If today is a holiday/closed, they log a skip and exit 0 (nothing written to S3).

---

### 11) Day‑1 Operations
- Run Day‑1 (first time or after reset): GitHub → Actions → `run-day-one` → enter `STARTING_CASH` → Run.
- Reset Day‑1: GitHub → Actions → `reset-day-one` → enter `STARTING_CASH` → Run.
  - This archives `Start Your Own/` → clears the prefix → dispatches `run-day-one` automatically.
- Verification: CloudWatch `day-one/` logs include “Day‑1 initialization complete.”

---

### 12) Verification Runbook (Console)
- S3 queue preview: copy queue to stdout (see CLI snippets above).
- EOD queue (manual): ECS → `microcap-queue-eod` → Run task → verify S3 `orders_queue.json` updated.
- Executor (manual): run during OPG window; otherwise expect the “Outside OPG window” skip message.
- Logs: CloudWatch → log group `/ecs/microcap` → choose the latest stream for the run.

---

### 13) Troubleshooting Matrix
- Docker login failing in build:
  - Symptom: `invalid control character` or malformed URL. Fix: login to REGISTRY host only; compute REGISTRY in step and strip CR `\r` before `docker login`.
- Day‑1 workflow cannot find VPC/Subnets/SG:
  - Symptom: `UnauthorizedOperation` on `DescribeVpcs`. Fix: add EC2 Describe read-only to the OIDC role.
- Reset cannot dispatch Day‑1:
  - Symptom: `403 Resource not accessible by integration`. Fix: in `reset-day-one.yml`, add `permissions: actions: write`.
- New Python module not visible to ECS:
  - Symptom: `ModuleNotFoundError` in logs after push. Fix: re‑run `build-push` so ECR `:latest` includes the file; update task defs if pinned.
- Executor “Outside OPG window”:
  - Expected when run outside ET 7:00pm–9:28am. Run during the window or rely on the 09:25 ET schedule.

---

### 14) Security & Retention Best Practices
- Enable S3 Versioning for `microcap-shared-state-780372467371` (retain all snapshots of CSVs/queue). Consider lifecycle to Glacier after N days.
- Set CloudWatch Logs retention (e.g., 14–30 days) on `/ecs/microcap`.
- Limit task role to only required bucket/prefix and secrets ARNs. Keep OIDC role tight to the one repo/branch.

---

### 15) Release Management
- Current: tasks point to `:latest` (schedules pick up new code on next run).
- Option: pin images by tag. Steps: push `:vX.Y.Z`, register task‑def with pinned tag, keep schedules set to “use latest revision.”

---

### 16) Appendices

#### A) Example Trust Policy (GitHub OIDC)
- Federated principal: `arn:aws:iam::780372467371:oidc-provider/token.actions.githubusercontent.com`
- Conditions:
  - `StringEquals` audience `sts.amazonaws.com`
  - `StringLike` subject `repo:LotharsBoots/ChatGPT-Micro-Cap-Experiment:ref:refs/heads/API-Brokerage`

#### B) Inline Policies (Sketches)
- EC2 Describe (read-only): `ec2:DescribeVpcs|DescribeSubnets|DescribeSecurityGroups` on `*`.
- S3 State+Archive RW: `s3:GetObject|PutObject|DeleteObject` on `arn:aws:s3:::microcap-shared-state-780372467371/Start Your Own/*` and `/Archive/*`, plus ListBucket with those prefixes.
- ECR push: repo `arn:aws:ecr:us-east-1:780372467371:repository/microcap` with `GetAuthorizationToken`, `UploadLayer*`, `PutImage`, etc.

---

### 17) What To Do If… (Quick Guides)
- You need a clean Day‑1 slate: run `reset-day-one` with desired `STARTING_CASH` → verify new archive folder in S3.
- Daily queue contains empty brackets `[]`: model returned no trades; pipeline is healthy; try EOD or next day.
- You want to force a live submit: run executor during OPG window (ET 7:00pm–9:28am) from ECS Task Definitions.


