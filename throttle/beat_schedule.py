# Add to your Django settings.py (or celery.py)
# Runs the round-robin dispatcher every second

from celery.schedules import crontab  # noqa – shown for context

CELERY_BEAT_SCHEDULE = {
    "dispatch-gateway-requests": {
        "task": "throttle.gateway.dispatch_gateway_requests",
        "schedule": 1.0,  # every 1 second
    },
}
