# Celery Application Instance
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
    # Without these two, Celery acks a task the moment a worker *picks it
    # up*, not when it finishes. If the worker process is killed mid-task
    # (OOM, container restart, manual kill) the task is just gone — it's
    # never requeued, and max_retries never gets a chance to kick in
    # because that only fires on an in-task exception, not a lost worker.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Safety net for sessions whose end-of-interview report dispatch
    # failed (broker down at that exact moment, etc) and so never got a
    # report through the normal path. Requires a `celery beat` process
    # running alongside the worker: `celery -A app.workers.celery_app beat`.
    beat_schedule={
        "reconcile-missing-reports": {
            "task": "reconcile_missing_reports",
            "schedule": 300.0,  # every 5 minutes
        },
    },
)