"""
Part 1 – Shared Gateway: Fair Round-Robin Throttle
===================================================
Components used:
  - Redis (ElastiCache) : per-customer request queues + token bucket state
  - Celery              : task dispatch and rate enforcement
  - Django signal/view  : enqueue incoming customer requests

Flow:
  1. Customer request arrives → enqueued into Redis list  key: queue:<customer_id>
  2. Dispatcher task runs every second, drains up to 3 tokens from the bucket,
     and picks requests round-robin across active customer queues.
  3. Each picked request is handed to `call_external_api` task for execution.
  4. On API failure, exponential backoff retry (max 5 attempts).
"""

import time
import json
import redis
from celery import shared_task
from celery.utils.log import get_task_logger
from django.conf import settings

logger = get_task_logger(__name__)

_redis = redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)

# ── Token bucket (Redis-backed, atomic via Lua) ───────────────────────────────

_BUCKET_KEY = "gateway:token_bucket"
_RATE = 3          # tokens per second
_CAPACITY = 3      # burst cap = 1 second worth

_REFILL_SCRIPT = _redis.register_script("""
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local want     = tonumber(ARGV[4])

local data     = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens   = tonumber(data[1]) or capacity
local last     = tonumber(data[2]) or now

local elapsed  = math.max(0, now - last)
tokens         = math.min(capacity, tokens + elapsed * rate)

if tokens < want then return 0 end

tokens = tokens - want
redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
redis.call('EXPIRE', key, 60)
return 1
""")


def _acquire_tokens(n=1) -> bool:
    """Atomically consume n tokens. Returns True if granted."""
    return bool(_REFILL_SCRIPT(keys=[_BUCKET_KEY], args=[_CAPACITY, _RATE, time.time(), n]))


# ── Customer queue helpers ────────────────────────────────────────────────────

def enqueue_request(customer_id: str, payload: dict):
    """Called from Django view/signal when a customer triggers an external API call."""
    _redis.rpush(f"queue:{customer_id}", json.dumps(payload))
    _redis.sadd("gateway:active_customers", customer_id)


def _pop_next_request(customer_id: str) -> dict | None:
    raw = _redis.lpop(f"queue:{customer_id}")
    if raw is None:
        _redis.srem("gateway:active_customers", customer_id)
        return None
    return json.loads(raw)


# ── Dispatcher: runs every second via Celery beat ────────────────────────────

@shared_task
def dispatch_gateway_requests():
    """
    Celery beat task scheduled every 1 second.
    Drains up to RATE requests, one per customer in round-robin order.
    """
    customers = list(_redis.smembers("gateway:active_customers"))
    if not customers:
        return

    dispatched = 0
    # Round-robin: iterate customers, take at most 1 request each per cycle
    for customer_id in customers:
        if dispatched >= _RATE:
            break
        if not _acquire_tokens(1):
            break
        payload = _pop_next_request(customer_id)
        if payload:
            call_external_api.delay(customer_id, payload)
            dispatched += 1


# ── Worker task: calls the external API with exponential backoff ──────────────

@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=1,   # overridden below with exponential backoff
)
def call_external_api(self, customer_id: str, payload: dict):
    import requests  # local import to keep module lightweight

    try:
        resp = requests.post(settings.EXTERNAL_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("customer=%s status=%s", customer_id, resp.status_code)
        return resp.json()

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response else 0
        # 4xx client errors: don't retry
        if 400 <= status < 500:
            logger.error("customer=%s non-retryable HTTP %s", customer_id, status)
            raise

        # 5xx server errors: exponential backoff (1s, 2s, 4s, 8s, 16s)
        delay = 2 ** self.request.retries
        logger.warning("customer=%s HTTP %s, retry %s in %ss",
                       customer_id, status, self.request.retries + 1, delay)
        raise self.retry(exc=exc, countdown=delay)

    except requests.Timeout as exc:
        delay = 2 ** self.request.retries
        logger.warning("customer=%s timeout, retry %s in %ss",
                       customer_id, self.request.retries + 1, delay)
        raise self.retry(exc=exc, countdown=delay)
