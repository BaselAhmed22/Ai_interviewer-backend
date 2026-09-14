from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "intervyou_workers",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Without these, a worker killed mid-task (OOM, restart) loses the
    # task silently instead of requeuing it — max_retries only fires on an
    # in-task exception, not a lost worker.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Requires `celery -A app.workers.celery_app beat` running alongside the worker.
    beat_schedule={
        "reconcile-missing-reports": {
            "task": "reconcile_missing_reports",
            "schedule": 300.0,  # every 5 minutes
        },
        "fail-stale-sessions": {
            "task": "fail_stale_sessions",
            "schedule": 600.0,  # every 10 minutes
        },
        "cleanup-expired-refresh-tokens": {
            "task": "cleanup_expired_refresh_tokens",
            "schedule": 86400.0,  # once a day
        },
    },
)