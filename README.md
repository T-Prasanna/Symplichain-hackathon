# Symplichain Hackathon Submission

**Candidate:** Teemara Prasanna Kumari
**Role:** Software Engineering Intern
**GitHub:** https://github.com/T-Prasanna/Symplichain-hackathon

This repo contains the GitHub Actions workflows for Part 3. Full written answers for all parts are below (and in the submitted PDF).

---

## Part 1 – Shared Gateway Problem

### Architecture

The existing stack already has everything needed — Redis (ElastiCache) and Celery. No new infrastructure required.

```
Customer Request
      │
      ▼
Django View / Signal
      │  enqueue_request(customer_id, payload)
      ▼
Redis List  key: queue:<customer_id>   ← one list per customer
Redis Set   key: gateway:active_customers

      ▲ polled every 1 second
      │
Celery Beat → dispatch_gateway_requests()
      │  round-robin pop, token-bucket gate
      ▼
call_external_api.delay(customer_id, payload)   ← Celery worker
      │
      ▼
External API  (≤ 3 req/s guaranteed)
```

See [`throttle/gateway.py`](throttle/gateway.py) for the implementation.

### Rate Enforcement

I use a **token bucket** stored in Redis, updated atomically via a Lua script (so multiple Celery workers can't race). The bucket holds 3 tokens and refills at 3 tokens/second. The dispatcher task runs every second via Celery Beat and consumes one token per request it dispatches — it stops as soon as tokens run out. This gives a hard ceiling of 3 req/s regardless of how many workers are running.

Why token bucket over a simple counter? A counter resets on a fixed clock boundary, which allows a burst of 6 requests across a boundary (3 at 0.99s + 3 at 1.00s). The token bucket prevents that.

### Fairness

Each customer gets their own Redis list (`queue:<customer_id>`). The dispatcher iterates active customers in **round-robin** order, taking at most one request per customer per cycle. So if Customer A has 100 queued and Customer B has 1:

- Cycle 1: dispatch A[0], B[0], A[1] → B's single request is served in the first second
- Customer B never waits behind all 100 of A's requests

This is weighted fair queuing at its simplest. If we needed priority tiers (e.g. paid vs free customers), we could assign weights and take 2 slots from premium customers per cycle instead of 1.

### Failure Handling

`call_external_api` uses Celery's `self.retry()` with **exponential backoff**: 1s → 2s → 4s → 8s → 16s (max 5 retries). 4xx errors are not retried (they're the caller's fault). On final failure the task raises and the error is logged — the customer's other queued requests are unaffected.

---

## Part 2 – Mobile Architecture

### Tech Stack: React Native (Expo)

**Why React Native over native Kotlin/Swift:**
- The web team already writes React + Tailwind. A large portion of business logic (API calls, state management, validation) can be shared or ported directly.
- A startup with one mobile app and a small team cannot afford two separate native codebases. Shipping speed matters more than the marginal performance gain of native.
- Expo's managed workflow removes most of the native build complexity and allows OTA (over-the-air) updates — critical for a fast-moving product.
- React Native's performance is more than sufficient for a logistics workflow app (it's not a game or a camera-heavy app).

**I would not choose Flutter** because the team has zero Dart experience, and the learning curve cost outweighs the benefits at this stage.

### Interaction Model

The primary users are **logistics partners and drivers** — people who are often moving, wearing gloves, or in poor lighting. The interaction model should be:

1. **Large-target, gesture-first UI** as the default. Big tap zones, swipe-to-confirm for critical actions (e.g. mark delivery complete), minimal text input.
2. **Voice commands via SymAI** for hands-free scenarios. "Mark delivered", "Report damage", "Navigate to next stop" — these map directly to existing API actions and SymAI already has the model infrastructure.
3. **Camera-first for POD uploads** — one tap to open camera, auto-capture on stability detection, no manual crop/confirm step.

The customer-facing (non-driver) side of the app is more form-heavy, so it can lean on standard tap-based navigation with a bottom tab bar.

### Architecture

```
React Native (Expo)
├── screens/          ← one screen per major workflow
├── components/       ← shared UI (mirrors web component library where possible)
├── hooks/            ← API calls via React Query (same pattern as web)
├── store/            ← Zustand for lightweight local state
└── services/
    ├── api.ts        ← wraps existing DRF endpoints (no new backend needed)
    └── voice.ts      ← SymAI voice command integration

Backend: existing Django/DRF — no changes needed for MVP
Auth: existing JWT tokens, stored in SecureStore (Expo)
Push notifications: AWS SNS → Expo Push Notification Service
Offline: React Query's cache + optimistic updates for poor-connectivity scenarios
```

---

## Part 3 – CI/CD Pipeline

See the workflow files:
- [`.github/workflows/staging-deploy.yml`](.github/workflows/staging-deploy.yml)
- [`.github/workflows/production-deploy.yml`](.github/workflows/production-deploy.yml)

### What the pipelines do

**Staging** (push to `staging` branch):
1. Build React frontend with staging env vars → sync to S3 → invalidate CloudFront
2. SSH to staging EC2 → `git reset --hard`, `pip install`, `migrate`, `collectstatic`, restart gunicorn + celery

**Production** (push to `main` branch):
1. Manual approval gate via GitHub Environments (a human must click approve)
2. Same build + S3 sync as staging, but with prod env vars
3. `systemctl reload gunicorn` instead of restart — this does a zero-downtime in-place reload so live connections aren't dropped
4. Slack notification on failure

### Required GitHub Secrets

| Secret | Used in |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Both |
| `STAGING_S3_BUCKET`, `STAGING_CLOUDFRONT_ID` | Staging |
| `STAGING_EC2_HOST`, `STAGING_EC2_SSH_KEY` | Staging |
| `STAGING_API_URL` | Staging frontend build |
| `PROD_S3_BUCKET`, `PROD_CLOUDFRONT_ID` | Production |
| `PROD_EC2_HOST`, `PROD_EC2_SSH_KEY` | Production |
| `PROD_API_URL` | Production frontend build |
| `SLACK_WEBHOOK_URL` | Production failure alert |

### Docker + Terraform improvement

**Docker:** The main pain point with the current setup is that `pip install` on EC2 can fail if the system Python environment drifts, and there's no guarantee the EC2 environment matches what was tested. Containerising the Django app and Celery worker means the image built in CI is exactly what runs in production. The deploy step becomes `docker pull` + `docker compose up -d` instead of a fragile SSH script.

```
# Dockerfile (minimal)
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["gunicorn", "symflow.wsgi:application", "--bind", "0.0.0.0:8000"]
```

The GitHub Actions workflow would push the image to ECR, then SSH to EC2 and run `docker compose pull && docker compose up -d`.

**Terraform:** The current setup has no IaC, which means the staging and production environments can silently drift apart. Terraform would codify the EC2 instance type, security groups, RDS config, ElastiCache cluster, and S3 bucket policies. The immediate wins are: reproducible environments, peer-reviewable infrastructure changes (PRs for infra), and the ability to spin up a fresh environment in minutes for a new region or disaster recovery.

I'd prioritise Terraform over Docker if forced to pick one, because environment drift is a harder problem to debug than a failed pip install.

---

## Part 4 – Debugging the Monday Outage

The upload path is: Driver App → Django (EC2) → S3 → Celery → Bedrock → RDS

I follow the data path in order, stopping as soon as I find the failure point.

### Step 1 — Did the request reach Django?

```bash
# SSH into EC2
tail -n 200 /var/log/gunicorn/error.log | grep -E "upload|POD|ERROR"
# or via CloudWatch Logs if log shipping is configured
```

**What I'm looking for:** HTTP 4xx/5xx on the upload endpoint, or a Python traceback.

- If I see a 403/401 → auth token issue on the driver app side
- If I see a 500 with an S3 traceback → move to Step 2
- If there are no log entries at all → the request never reached EC2 (check Nginx: `tail /var/log/nginx/error.log`, check security group rules, check if gunicorn is even running: `systemctl status gunicorn`)

### Step 2 — Did the S3 upload succeed?

Still in Django logs, look for the S3 `put_object` call result. Alternatively:

```bash
# Check if recent objects exist in the POD bucket
aws s3 ls s3://<pod-bucket>/uploads/ --recursive | tail -20
```

Common failure here: **expired IAM role on the EC2 instance** (the instance profile's permissions were changed or the role was detached). Check in CloudWatch → IAM → look for `AccessDenied` events around 9 AM Monday.

### Step 3 — Did the Celery task get queued and run?

Open **Celery Flower** dashboard (typically `http://<ec2-host>:5555`).

- Check the `Tasks` tab: are POD validation tasks in `FAILURE` state?
- Check the `Workers` tab: are workers online? If all workers show as offline, Redis/ElastiCache may be unreachable (`redis-cli -h <elasticache-endpoint> ping`)

If tasks are failing, click into a failed task to see the exception traceback — this usually tells you exactly which step failed.

### Step 4 — Is Bedrock responding?

In **CloudWatch**:
- Metrics → Bedrock → `InvocationClientErrors` / `InvocationServerErrors` / `InvocationLatency`
- Look for a spike around 9 AM Monday

Also check: did the Bedrock model get deleted or its endpoint changed? This has happened before when a fine-tuned model is re-deployed with a new ARN and the Django settings weren't updated.

```bash
# Quick check from EC2
aws bedrock-runtime invoke-model \
  --model-id <model-id> \
  --body '{"prompt":"test"}' \
  --region ap-south-1 /tmp/test_response.json
```

### Step 5 — Is RDS accepting connections?

In **RDS Console** → Monitoring tab:
- `DatabaseConnections` — is it at the max_connections limit?
- `FreeStorageSpace` — a full disk causes writes to fail silently

```bash
# From EC2, check if Django can reach RDS
python manage.py dbshell
# or
psql -h <rds-endpoint> -U <user> -d <db> -c "SELECT 1;"
```

A common Monday-morning cause: a long-running analytics query from the weekend held open connections and hit the RDS connection limit. Fix: `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle' AND query_start < now() - interval '1 hour';`

### Most likely root cause (based on experience)

Monday 9 AM outages after a quiet weekend most often come down to:
1. **IAM role / credentials rotated over the weekend** → S3 AccessDenied (Step 2)
2. **Celery workers crashed and weren't restarted** → tasks queued but never processed (Step 3)
3. **Bedrock model ARN changed** after a weekend re-deployment (Step 4)

I'd check in that order because Steps 1–2 take under 2 minutes to rule out, and they're the most common.
