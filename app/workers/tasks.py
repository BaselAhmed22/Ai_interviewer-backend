import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine

from celery.signals import worker_process_init, worker_process_shutdown
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from app.workers.celery_app import celery_app
from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.interviews.models.session import InterviewSession, SessionStatus
from app.interviews.models.report import InterviewReport
from app.interviews.models.transcript import InterviewTranscript
from app.interviews.services import transcript_service
from app.auth.models import RefreshToken
from app.ai_evaluator.evaluation_agent import EvaluationAgent, load_evaluation_inputs
from app.core.redis_service import redis_service

logger = logging.getLogger(__name__)

evaluation_agent = EvaluationAgent()


def _backoff_countdown(retries: int, base: float = 10.0) -> float:
    return base * (2 ** retries) + random.uniform(0, 3)

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

    try:
        # Pull session/transcript/job-description from Postgres and pair
        # each prepared question up with the candidate's actual answer
        # (see evaluation_agent.load_evaluation_inputs /
        # _pair_questions_and_answers), then run the AI team's scoring
        # engine against those resolved primitives.
        candidate_name, candidate_analysis, job_description, questions, answers = run_async(
            load_evaluation_inputs(session_id)
        )
        result = evaluation_agent.evaluate(candidate_name, candidate_analysis, job_description, questions, answers)
    except Exception as exc:
        # Transcript/session missing, GEMINI_API_KEY not configured, Gemini
        # unreachable or quota-exhausted, malformed JSON back — all land
        # here and get Celery's normal backoff-retry treatment.
        logger.error("Error evaluating interview for session %s: %s", session_id, exc)
        if self.request.retries >= self.max_retries:
            run_async(_mark_session_failed(session_id, str(exc)))
            raise
        raise self.retry(exc=exc, countdown=_backoff_countdown(self.request.retries))

    evaluation = result.to_dict()

    async def save_to_db():
        async with AsyncSessionLocal() as db:
            report = InterviewReport(
                id=uuid.uuid4(),
                session_id=uuid.UUID(session_id),
                overall_score=evaluation.get("overall_score"),
                technical_score=evaluation.get("technical_score"),
                problem_solving_score=evaluation.get("problem_solving_score"),
                communication_score=evaluation.get("communication_score"),
                strengths=evaluation.get("strengths") or [],
                weaknesses=evaluation.get("weaknesses") or [],
                recommendation=evaluation.get("recommendation"),
                summary=evaluation.get("summary"),
                is_placeholder=False,
            )
            db.add(report)
            await db.commit()

    try:
        run_async(save_to_db())
    except IntegrityError:
        if run_async(_report_already_exists(session_id)):
            logger.info("Report already exists for session %s — duplicate dispatch ignored.", session_id)
            return {"session_id": session_id, "duplicate_dispatch": True}

        logger.error("Unexpected integrity error saving report for session %s", session_id)
        if self.request.retries >= self.max_retries:
            run_async(_mark_session_failed(session_id, "Database integrity error while saving the report."))
            raise
        raise self.retry(countdown=_backoff_countdown(self.request.retries))
    except Exception as exc:
        logger.error("Error saving report for session %s: %s", session_id, exc)
        if self.request.retries >= self.max_retries:
            run_async(_mark_session_failed(session_id, str(exc)))
            raise
        raise self.retry(exc=exc, countdown=_backoff_countdown(self.request.retries))

    logger.info("Report successfully saved to PostgreSQL for session %s", session_id)
    return {"session_id": session_id, **evaluation}


@celery_app.task(name="reconcile_missing_reports")
def reconcile_missing_reports(stale_after_minutes: int = 10) -> dict:
    """Periodic safety net (celery_app.py's beat_schedule): re-dispatches
    report generation for COMPLETED sessions still missing one after
    stale_after_minutes — covers an end_session dispatch that failed."""
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


@celery_app.task(name="reconcile_missing_transcripts")
def reconcile_missing_transcripts(stale_after_minutes: int = 10) -> dict:
    """Periodic safety net (celery_app.py's beat_schedule): recovers
    transcripts for terminal sessions (COMPLETED/FAILED) that never got
    InterviewerAgent.on_exit()'s durable Postgres write — e.g. the LiveKit
    worker process crashed or was killed mid-interview. Falls back to the
    live Redis mirror (redis_service.append_transcript_turn, written
    turn-by-turn as the interview happens); if Redis has nothing either
    (its TTL already expired, or the crash happened before any turn was
    captured), the transcript is unrecoverable and is just logged as such
    rather than retried forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)

    async def find_stale_sessions() -> list[tuple[str, str]]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(InterviewSession.id, InterviewSession.room_name).where(
                    InterviewSession.status.in_([SessionStatus.COMPLETED, SessionStatus.FAILED]),
                    InterviewSession.updated_at < cutoff,
                    ~InterviewSession.id.in_(select(InterviewTranscript.session_id)),
                )
            )
            return [(str(row.id), row.room_name) for row in result.all()]

    async def recover_from_redis(session_id: str, room_name: str) -> bool:
        turns = await redis_service.get_transcript_turns(room_name)
        if not turns:
            return False
        await transcript_service.save_transcript(uuid.UUID(session_id), turns)
        return True

    stale_sessions = run_async(find_stale_sessions())
    recovered = 0
    unrecoverable = 0
    for session_id, room_name in stale_sessions:
        if run_async(recover_from_redis(session_id, room_name)):
            logger.warning("Reconciliation: recovered transcript for session %s from the Redis mirror.", session_id)
            recovered += 1
        else:
            logger.warning(
                "Reconciliation: no transcript recoverable for session %s — missing from both "
                "Postgres and the Redis mirror (TTL expired, or the worker crashed before "
                "capturing any turn).", session_id,
            )
            unrecoverable += 1

    return {"recovered": recovered, "unrecoverable": unrecoverable}


@celery_app.task(name="fail_stale_sessions")
def fail_stale_sessions() -> dict:
    """Closes out IN_PROGRESS sessions abandoned for too long (dropped
    connection, closed tab, ...) so the candidate isn't permanently blocked
    from starting a new one by the one-active-session-per-user constraint."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.STALE_SESSION_TIMEOUT_MINUTES)

    async def fail_stale() -> int:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(InterviewSession).where(
                    InterviewSession.status == SessionStatus.IN_PROGRESS,
                    InterviewSession.created_at < cutoff,
                )
            )
            sessions = result.scalars().all()
            for session_obj in sessions:
                session_obj.status = SessionStatus.FAILED
                session_obj.failure_reason = "Session timed out due to inactivity."
            await db.commit()
            return len(sessions)

    failed = run_async(fail_stale())
    if failed:
        logger.warning("Closed %d stale IN_PROGRESS session(s) past the %d-minute timeout.",
                        failed, settings.STALE_SESSION_TIMEOUT_MINUTES)
    return {"failed": failed}


@celery_app.task(name="cleanup_expired_refresh_tokens")
def cleanup_expired_refresh_tokens() -> dict:
    """Deletes refresh_tokens rows past their expires_at (see celery_app.py's beat_schedule)."""
    cutoff = datetime.now(timezone.utc)

    async def delete_expired() -> int:
        async with AsyncSessionLocal() as db:
            result = await db.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff))
            await db.commit()
            return result.rowcount

    deleted = run_async(delete_expired())
    if deleted:
        logger.info("Cleaned up %d expired refresh token(s).", deleted)
    return {"deleted": deleted}

