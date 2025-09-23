# 100% Cloud Context (Option B: GitHub → ECR → ECS)

This file is self‑contained so a new reader can operate the system entirely in the cloud with no local installs after setup.

---

## What this gives you
- No desktop app after setup. Builds run in GitHub Actions. Runtime is ECS Fargate. State is S3. Logs are CloudWatch. Schedules are EventBridge.
- Day‑1 is a one‑time cloud action (GitHub workflow or AWS Console Run task). After Day‑1, jobs run autonomously.

---

## AWS baseline already in place
- Account/Region: 780372467371 / us‑east‑1
- ECR repo: 780372467371.dkr.ecr.us-east-1.amazonaws.com/microcap
- ECS cluster: microcap-cluster
- Task definitions: microcap-queue-daily (rev 7), microcap-queue-eod (rev 5), microcap-executor-morning (rev 1), microcap-day-one
- Schedules (America/New_York):
  - queue-daily @ 16:00 Mon–Thu
  - queue-eod @ 16:00 Fri
  - executor-morning @ 09:25 Mon–Fri
  - Retry = 3; Max event age = 2h; DLQ = microcap-scheduler-dlq
- Secrets Manager (per‑key): microcap/openai-api-key, microcap/alpaca-base-url, microcap/alpaca-key-id, microcap/alpaca-secret-key
- S3 state: s3://microcap-shared-state-780372467371/Start Your Own/
- Logs: /ecs/microcap (prefixes: queue-daily, eod, executor, day-one)

---

## One‑time: GitHub OIDC role (no long‑lived keys)
1) Create an IAM role trusted by `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`, and restrict `sub` to your repo:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::780372467371:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:<OWNER>/<REPO>:ref:refs/heads/*",
            "repo:<OWNER>/<REPO>:ref:refs/tags/*"
          ]
        }
      }
    }
  ]
}
```
2) Attach a policy that allows:
- ECR push (GetAuthorizationToken, UploadLayer*, PutImage…)
- ECS (RegisterTaskDefinition, DescribeTaskDefinition, RunTask, iam:PassRole)
- Logs (PutLogEvents)
- S3 list/get/put/delete on the state bucket

3) In your GitHub repo → Settings → Actions → General: enable “Read and write permissions”.

4) Add Actions variables/secrets (Settings → Secrets and variables → Actions):
- Variables: `AWS_REGION=us-east-1`, `AWS_ACCOUNT_ID=780372467371`, `ECR_REPO=microcap`, `ECS_CLUSTER=microcap-cluster`, `TASK_FAMILY_DAY_ONE=microcap-day-one`
- Secrets: `AWS_ROLE_TO_ASSUME` = the IAM role ARN above

---

## Workflow 1 — Build and push on every push
Create `.github/workflows/build-push.yml`
```yaml
name: build-push
on:
  push:
    branches: [ main ]
  workflow_dispatch:
permissions:
  id-token: write
  contents: read
env:
  AWS_REGION: ${{ vars.AWS_REGION }}
  AWS_ACCOUNT_ID: ${{ vars.AWS_ACCOUNT_ID }}
  ECR_REPO: ${{ vars.ECR_REPO }}
  IMAGE: ${{ vars.AWS_ACCOUNT_ID }}.dkr.ecr.${{ vars.AWS_REGION }}.amazonaws.com/${{ vars.ECR_REPO }}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_TO_ASSUME }}
          aws-region: ${{ env.AWS_REGION }}
      - run: aws ecr describe-repositories --repository-names "$ECR_REPO" || aws ecr create-repository --repository-name "$ECR_REPO"
      - run: aws ecr get-login-password | docker login --username AWS --password-stdin "$IMAGE"
      - run: |
          docker build -t "$IMAGE:latest" .
          docker push "$IMAGE:latest"
```
Notes: Using `:latest` means schedules pick up new code automatically on the next run. For release control, also tag/push a version and register a new task‑def revision.

---

## Workflow 2 — Run Day‑1 (manual, one‑time)
Create `.github/workflows/run-day-one.yml`
```yaml
name: run-day-one
on:
  workflow_dispatch:
    inputs:
      STARTING_CASH:
        description: "Starting cash (e.g., 3500)"
        required: true
        type: string
permissions:
  id-token: write
  contents: read
env:
  AWS_REGION: ${{ vars.AWS_REGION }}
  ECS_CLUSTER: ${{ vars.ECS_CLUSTER }}
  TASK_FAMILY_DAY_ONE: ${{ vars.TASK_FAMILY_DAY_ONE }}
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_TO_ASSUME }}
          aws-region: ${{ env.AWS_REGION }}
      - id: td
        run: |
          arn=$(aws ecs list-task-definitions --family-prefix "$TASK_FAMILY_DAY_ONE" --sort DESC --max-items 1 --query 'taskDefinitionArns[0]' --output text)
          echo "td_arn=$arn" >> $GITHUB_OUTPUT
      - id: net
        run: |
          vpc=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
          subnets=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$vpc --query 'Subnets[].SubnetId' --output text)
          s1=$(echo $subnets | awk '{print $1}')
          s2=$(echo $subnets | awk '{print $2}')
          sg=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values=$vpc Name=group-name,Values=default --query 'SecurityGroups[0].GroupId' --output text)
          echo "s1=$s1" >> $GITHUB_OUTPUT
          echo "s2=$s2" >> $GITHUB_OUTPUT
          echo "sg=$sg" >> $GITHUB_OUTPUT
      - name: Run Day-1 task
        run: |
          overrides='{"containerOverrides":[{"name":"app","environment":[{"name":"STARTING_CASH","value":"${{ inputs.STARTING_CASH }}"}]}]}'
          netcfg="awsvpcConfiguration={subnets=[$(printf '"%s","%s"' ${{ steps.net.outputs.s1 }} ${{ steps.net.outputs.s2 }})],securityGroups=[\"${{ steps.net.outputs.sg }}\"],assignPublicIp=\"ENABLED\"}"
          aws ecs run-task --cluster "$ECS_CLUSTER" --launch-type FARGATE --task-definition "${{ steps.td.outputs.td_arn }}" --overrides "$overrides" --network-configuration "$netcfg"
```
Usage: GitHub → Actions → `run-day-one` → Run workflow → enter STARTING_CASH. Idempotent: if positions already exist, it no‑ops.

---

## (Optional) Workflow 3 — Reset to Day‑1 (cloud)
Archive S3 → clear → re‑run Day‑1. Create `.github/workflows/reset-day-one.yml`
```yaml
name: reset-day-one
on:
  workflow_dispatch:
    inputs:
      STARTING_CASH:
        description: "Starting cash after reset"
        required: true
permissions:
  id-token: write
  contents: read
env:
  AWS_REGION: ${{ vars.AWS_REGION }}
  BUCKET: microcap-shared-state-780372467371
  PREFIX: "Start Your Own"
jobs:
  reset:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_TO_ASSUME }}
          aws-region: ${{ env.AWS_REGION }}
      - name: Archive and clear
        run: |
          stamp=$(date +%Y%m%d-%H%M%S)
          aws s3 sync "s3://$BUCKET/$PREFIX" "s3://$BUCKET/Archive/day-one-$stamp/"
          aws s3 rm "s3://$BUCKET/$PREFIX" --recursive
      - name: Trigger Day-1
        run: |
          gh workflow run run-day-one.yml -f STARTING_CASH=${{ inputs.STARTING_CASH }}
        env:
          GH_TOKEN: ${{ github.token }}
```

---

## Day‑to‑Day Operations (autonomous)
- Mon–Thu 16:00 ET: queue_daily generates/merges orders_queue.json
- Fri 16:00 ET: queue_eod deep‑research merge
- Weekdays 09:25 ET: executor submits OPG and updates CSVs
- S3: `Start Your Own/` holds CSVs and orders_queue.json; logs: `/ecs/microcap`; DLQ: `microcap-scheduler-dlq`

---

## Retention, Security, Idempotency
- S3 Versioning recommended to retain every update; optional lifecycle to Glacier.
- CloudWatch Logs retention as needed.
- Secrets: per‑key ARNs; execution role needs `secretsmanager:GetSecretValue`. Task role limited to bucket/prefix.
- Day‑1 is idempotent; re‑run only after a deliberate reset.

---

## Minimal Troubleshooting
- Task stops before logs → likely Secrets permission/ARN format; verify execution role and ARNs.
- No S3 writes → check task role S3 perms and bucket/prefix.
- Executor says “Outside OPG window” → expected when run outside ET 7:00pm–9:28am.

---

## Recent Enhancements & Notes (Sep 2025)

- GitHub OIDC role in AWS with trust restricted to repo/branch; repo variables and secret configured.
- Workflows:
  - `build-push`: builds image and pushes to ECR on branch `API-Brokerage`. Docker login must use REGISTRY host `${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com` (not the full IMAGE path). If needed, compute REGISTRY in a step and strip CRLF before login.
  - `run-day-one`: manual, finds latest Day‑1 task‑def and runs Fargate after resolving default VPC/subnets/SG; input `STARTING_CASH`.
  - `reset-day-one`: archives S3 `Start Your Own/` to `Archive/day-one-<timestamp>/`, clears it, then dispatches `run-day-one`. Requires workflow `permissions: actions: write`.
- ECS task definition added: `microcap-day-one` (runs `python day_one_bootstrap.py` with S3 sync in/out).
- IAM additions for workflows:
  - EC2 Describe VPC/Subnets/SG (read-only) for Day‑1 network lookup.
  - S3 RW on `Start Your Own/*` and `Archive/*` for reset.

Planned: add Alpaca market‑clock gating to daily/EOD so they skip on holidays/closed market (executor already OPG‑gated).