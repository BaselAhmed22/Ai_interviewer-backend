"""
InterviewPipelineManager — orchestrates the five-stage agent pipeline:

    DocumentAgent -> QuestionnaireAgent -> ControllerAgent -> VoiceAgent -> EvaluationAgent

Redis (redis_service.save_pipeline_context) holds the live AgentContext
for one in-flight run — ephemeral, TTL-bound working state. Once
/interviews/start creates the InterviewSession row, its questions and
candidate_summary are also persisted there, so they outlive this Redis
key. See app/interviews/api/pipeline.py for prepare/start/evaluate.

Stage 1-2's Gemini output (candidate_summary + questions) is additionally
cached by a content fingerprint of the CV/job description that produced
it (interview-prep-cache:<fingerprint>, 24h TTL) — see _fingerprint and
prepare()'s cache check below. Same CV text against the same job
description always deserves the same analysis, so this turns repeated
prepare() calls for an unchanged profile (test runs, a candidate
re-clicking "prepare") from two fresh Gemini calls into a Redis read,
which is the difference between burning free-tier quota and not.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.interviews.agents.document_agent import DocumentAgent
from app.interviews.agents.questionnaire_agent import QuestionnaireAgent
from app.candidates.models import CandidateProfile, JobDescription
from app.interviews.schemas.agent_context import AgentContext, CandidateSummary, GeneratedQuestion, PipelineStage
from app.core.redis_service import redis_service

logger = logging.getLogger(__name__)

document_agent = DocumentAgent()
questionnaire_agent = QuestionnaireAgent()

_PREP_CACHE_TTL_SECONDS = 24 * 3600
_NUMBER_OF_QUESTIONS = 5


def _fingerprint(cv_text: str, job_title: str, job_description: str, number_of_questions: int) -> str:
    """Deterministic cache key: the exact inputs that decide DocumentAgent
    + QuestionnaireAgent's Gemini output. Not scoped to a candidate_id on
    purpose — two requests with byte-identical CV text and job description
    (the common case in repeated test runs against the same fixture) will
    always deserve the identical answer regardless of which account sent
    it, so sharing the cache across users only saves quota, it never leaks
    anything a given request didn't already contain itself."""
    raw = "␟".join([cv_text.strip(), job_title.strip(), job_description.strip(), str(number_of_questions)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class InterviewPipelineManager:
    async def prepare(
        self,
        db: AsyncSession,
        user_uuid: uuid.UUID,
        company_name: str | None = None,
        force_regenerate: bool = False,
        number_of_questions: int = _NUMBER_OF_QUESTIONS,
    ) -> AgentContext:
        """Stage 1-2: DocumentAgent summarizes the candidate's active CV,
        QuestionnaireAgent turns that plus the active job description into
        a question list — unless an identical CV+job fingerprint is
        already cached (see module docstring), in which case both Gemini
        calls are skipped entirely. `force_regenerate=True` bypasses the
        cache lookup (used to intentionally get a fresh set), but a
        freshly generated result still overwrites the cache afterward.
        Persists the result as a fresh AgentContext and returns it."""
        cv_result = await db.execute(
            select(CandidateProfile).where(
                CandidateProfile.user_id == user_uuid, CandidateProfile.is_active.is_(True)
            )
        )
        profile = cv_result.scalars().first()
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "missing_cv", "message": "Upload a CV before preparing an interview."},
            )
        if profile.processing_failed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "cv_processing_failed",
                    "message": "Your CV could not be processed. Please re-upload it before preparing an interview.",
                },
            )

        job_result = await db.execute(
            select(JobDescription).where(
                JobDescription.user_id == user_uuid, JobDescription.is_active.is_(True)
            )
        )
        job = job_result.scalars().first()
        if not job:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "missing_job_description",
                    "message": "Add a job description before preparing an interview.",
                },
            )

        fingerprint = _fingerprint(
            profile.raw_cv_text or "", job.job_title, job.description_text, number_of_questions
        )

        cached = None if force_regenerate else await redis_service.get_cached_interview_preparation(fingerprint)
        from_cache = cached is not None
        if cached:
            cached_data = json.loads(cached)
            candidate_summary = CandidateSummary.model_validate(cached_data["candidate_summary"])
            questions = [GeneratedQuestion.model_validate(q) for q in cached_data["questions"]]
            logger.info(
                "Interview preparation cache HIT for fingerprint %s (user %s) — Gemini not called.",
                fingerprint[:12], user_uuid,
            )
        else:
            candidate_summary = await document_agent.summarize(
                raw_cv_text=profile.raw_cv_text, job_description=job.description_text, full_name=profile.full_name
            )
            questions = await questionnaire_agent.generate(candidate_summary, job.job_title, number_of_questions)
            await redis_service.cache_interview_preparation(
                fingerprint,
                json.dumps({
                    "candidate_summary": candidate_summary.model_dump(mode="json"),
                    "questions": [q.model_dump(mode="json") for q in questions],
                }),
                ttl=_PREP_CACHE_TTL_SECONDS,
            )
            logger.info(
                "Interview preparation cache MISS for fingerprint %s (user %s) — generated via Gemini and cached.",
                fingerprint[:12], user_uuid,
            )

        now = datetime.now(timezone.utc)
        context = AgentContext(
            preparation_id=str(uuid.uuid4()),
            user_id=str(user_uuid),
            candidate_profile_id=str(profile.id),
            job_description_id=str(job.id),
            stage=PipelineStage.QUESTIONNAIRE,
            candidate_summary=candidate_summary,
            questions=questions,
            company_name=company_name,
            from_cache=from_cache,
            created_at=now,
            updated_at=now,
        )
        await self._save(context)
        return context

    async def get_context(self, preparation_id: str) -> AgentContext:
        raw = await redis_service.get_pipeline_context(preparation_id)
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "Interview preparation not found or expired."},
            )
        return AgentContext.model_validate_json(raw)

    async def mark_started(self, context: AgentContext, session_id: uuid.UUID) -> AgentContext:
        """Stage 3: hands the prepared context to ControllerAgent. The
        LiveKit agent process loads this same context back out of Redis
        via preparation_id in its job's dispatch metadata (see
        app/interviews/workers/livekit_agent.py)."""
        context.session_id = str(session_id)
        context.stage = PipelineStage.VOICE
        context.updated_at = datetime.now(timezone.utc)
        await self._save(context)
        return context

    async def _save(self, context: AgentContext) -> None:
        await redis_service.save_pipeline_context(context.preparation_id, context.model_dump_json())


interview_pipeline_manager = InterviewPipelineManager()
