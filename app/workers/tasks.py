# app/workers/tasks.py
import asyncio
import logging
import random
import uuid
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine

from celery.signals import worker_process_init, worker_process_shutdown
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.workers.celery_app import celery_app
from app.core.database import AsyncSessionLocal, engine
from app.db.models.session import InterviewSession, SessionStatus
from app.db.models.report import InterviewReport
from app.db.models.user import CandidateProfile
from app.services.cv_parser import extract_text_from_file, CorruptFileError

logger = logging.getLogger(__name__)


def _backoff_countdown(retries: int, base: float = 10.0) -> float:
    # Exponential (10s, 20s, 40s, ...) plus up to 3s of random jitter.
    # A flat 10s-every-time meant every task queued during a shared
    # outage (DB restart, broker blip) would all retry at the exact same
    # moment — a synchronized retry stampede hitting a resource that's
    # still recovering. Jitter spreads that out; the exponential growth
    # backs off harder the more a given task keeps failing.
    return base * (2 ** retries) + random.uniform(0, 3)


# A single long-lived event loop per worker *process*, created once when
# the process starts and torn down (with the shared async engine disposed
# alongside it) only on process shutdown — not after every single task.
# Previously a new loop was created and the shared engine disposed on
# every task call, which meant re-establishing the DB connection pool
# from scratch for each report/CV job instead of reusing a warm one.
_worker_loop: asyncio.AbstractEventLoop | None = None


@worker_process_init.connect
def _init_worker_loop(**kwargs) -> None:
    global _worker_loop
    _worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_worker_loop)


@worker_process_shutdown.connect
def _shutdown_worker_loop(**kwargs) -> None:
    global _worker_loop
    if _worker_loop is not None:
        _worker_loop.run_until_complete(engine.dispose())
        _worker_loop.close()
        _worker_loop = None


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    # Falls back to a create-and-dispose-per-call loop when no worker
    # process signal has fired (e.g. a task run eagerly outside a real
    # Celery worker, such as in a test), so behavior stays correct there.
    if _worker_loop is not None:
        return _worker_loop.run_until_complete(coro)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()


async def _report_already_exists(session_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(InterviewReport).where(InterviewReport.session_id == uuid.UUID(session_id))
        )
        return result.scalars().first() is not None


async def _mark_session_failed(session_id: str, reason: str) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(InterviewSession).where(InterviewSession.id == uuid.UUID(session_id))
        )
        session_obj = result.scalars().first()
        if session_obj:
            session_obj.status = SessionStatus.FAILED
            session_obj.failure_reason = reason
            await db.commit()


@celery_app.task(name="generate_final_interview_report", bind=True, max_retries=3)
def generate_final_interview_report(self, session_id: str, audio_file_path: str = None):
    logger.info("Starting post-processing for session %s", session_id)
    time.sleep(5)  # Simulate AI processing — replace with the real scoring pipeline

    report_metrics = {
        "overall_score": 88.5,
        "eye_contact_score": 90.0,
        "posture_score": 85.0,
        "speech_clarity_score": 90.5,
        "feedback_summary": "Great overall performance with stable body posture and strong eye contact.",
        "is_placeholder": True,
    }

    async def save_to_db():
        async with AsyncSessionLocal() as db:
            report = InterviewReport(
                id=uuid.uuid4(),
                session_id=uuid.UUID(session_id),
                **report_metrics,
            )
            db.add(report)
            await db.commit()

    try:
        run_async(save_to_db())
    except IntegrityError:
        # A concurrent duplicate dispatch (e.g. two near-simultaneous calls
        # to /sessions/end/{id}) can race to insert the report for the same
        # session. If a report already exists, the *interview* already
        # succeeded — this second attempt failing is expected, not a real
        # failure, and must not overwrite the session with FAILED.
        if run_async(_report_already_exists(session_id)):
            logger.info("Report already exists for session %s — duplicate dispatch ignored.", session_id)
            return {"session_id": session_id, "duplicate_dispatch": True}

        logger.error("Unexpected integrity error saving report for session %s", session_id)
        if self.request.retries >= self.max_retries:
            run_async(_mark_session_failed(session_id, "Database integrity error while saving the report."))
            raise
        raise self.retry(countdown=_backoff_countdown(self.request.retries))
    except Exception as exc:
        logger.error("Error generating report for session %s: %s", session_id, exc)
        if self.request.retries >= self.max_retries:
            run_async(_mark_session_failed(session_id, str(exc)))
            raise
        raise self.retry(exc=exc, countdown=_backoff_countdown(self.request.retries))

    logger.info("Report successfully saved to PostgreSQL for session %s", session_id)
    return {"session_id": session_id, **report_metrics}


@celery_app.task(name="process_cv_analysis", bind=True, max_retries=3)
def process_cv_analysis(self, profile_id: str):
    logger.info("Starting CV parsing for profile %s", profile_id)

    async def run_pipeline():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CandidateProfile).where(CandidateProfile.id == uuid.UUID(profile_id))
            )
            profile = result.scalar_one_or_none()
            if not profile or not profile.cv_file_path:
                logger.warning("Profile or file path not found for ID: %s", profile_id)
                return

            raw_text = extract_text_from_file(profile.cv_file_path)
            profile.raw_cv_text = raw_text

            # TODO: replace with a real skills-extraction AI call
            profile.skills = "Python, FastAPI, PostgreSQL, Docker, AsyncIO, REST APIs"

            await db.commit()
            logger.info("CV parsed & analyzed successfully for profile %s", profile_id)

    async def mark_processing_failed(reason: str) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CandidateProfile).where(CandidateProfile.id == uuid.UUID(profile_id))
            )
            profile = result.scalars().first()
            if profile:
                profile.processing_failed = True
                profile.failure_reason = reason
                await db.commit()

    try:
        run_async(run_pipeline())
    except CorruptFileError as exc:
        # Unlike a transient error (DB hiccup, disk hiccup), re-parsing the
        # exact same corrupt bytes on retry can never succeed — retrying
        # here would just burn 3 worker slots and ~30s of countdown delay
        # before landing on the same failure it hit immediately. Fail the
        # profile on the first attempt instead.
        logger.warning("CV for profile %s is corrupt — failing without retry: %s", profile_id, exc)
        run_async(mark_processing_failed(str(exc)))
        return {"profile_id": profile_id, "corrupt_file": True}
    except Exception as exc:
        logger.error("Error parsing CV for profile %s: %s", profile_id, exc)
        if self.request.retries >= self.max_retries:
            # Previously this just raised after exhausting retries with no
            # persisted signal — CandidateProfile had no failure field at
            # all, so a candidate could start an interview session on a CV
            # that silently failed to parse, with empty raw_cv_text/skills
            # and no warning anywhere.
            run_async(mark_processing_failed(str(exc)))
            raise
        raise self.retry(exc=exc, countdown=_backoff_countdown(self.request.retries))


@celery_app.task(name="reconcile_missing_reports")
def reconcile_missing_reports(stale_after_minutes: int = 10) -> dict:
    """
    Periodic safety net (see celery_app.py's beat_schedule): finds
    sessions that ended (COMPLETED) more than stale_after_minutes ago but
    still have no report, and re-dispatches report generation for them.
    Without this, a session whose end_session dispatch failed (broker
    down at that exact moment, say) had no other path to ever getting a
    report — it just sat there forever with the candidate stuck on
    "report_not_ready".
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)

    async def find_stale_sessions() -> list[str]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(InterviewSession.id).where(
                    InterviewSession.status == SessionStatus.COMPLETED,
                    InterviewSession.updated_at < cutoff,
                    ~InterviewSession.id.in_(select(InterviewReport.session_id)),
                )
            )
            return [str(row) for row in result.scalars().all()]

    stale_session_ids = run_async(find_stale_sessions())
    for session_id in stale_session_ids:
        logger.warning("Reconciliation: re-dispatching missing report for session %s", session_id)
        generate_final_interview_report.delay(session_id)

    return {"reconciled": len(stale_session_ids)}
