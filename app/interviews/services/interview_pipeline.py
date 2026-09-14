"""
InterviewPipelineManager — orchestrates the five-stage agent pipeline:

    DocumentAgent -> QuestionnaireAgent -> ControllerAgent -> VoiceAgent -> EvaluationAgent

Redis (redis_service.save_pipeline_context) holds the live AgentContext
for one in-flight run — ephemeral, TTL-bound working state. Once
/interviews/start creates the InterviewSession row, its questions and
candidate_summary are also persisted there, so they outlive this Redis
key. See app/interviews/api/pipeline.py for prepare/start/evaluate.
"""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.interviews.agents.document_agent import DocumentAgent
from app.interviews.agents.questionnaire_agent import QuestionnaireAgent
from app.candidates.models import CandidateProfile, JobDescription
from app.interviews.schemas.agent_context import AgentContext, PipelineStage
from app.core.redis_service import redis_service

document_agent = DocumentAgent()
questionnaire_agent = QuestionnaireAgent()


class InterviewPipelineManager:
    async def prepare(
        self, db: AsyncSession, user_uuid: uuid.UUID, simli_face_id: str | None = None
    ) -> AgentContext:
        """Stage 1-2: DocumentAgent summarizes the candidate's active CV,
        QuestionnaireAgent turns that plus the active job description into
        a question list. Persists the result as a fresh AgentContext and
        returns it."""
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

        candidate_summary = await document_agent.summarize(
            raw_cv_text=profile.raw_cv_text, job_description=job.description_text, full_name=profile.full_name
        )
        questions = await questionnaire_agent.generate(candidate_summary, job.job_title)

        now = datetime.now(timezone.utc)
        context = AgentContext(
            preparation_id=str(uuid.uuid4()),
            user_id=str(user_uuid),
            candidate_profile_id=str(profile.id),
            job_description_id=str(job.id),
            stage=PipelineStage.QUESTIONNAIRE,
            candidate_summary=candidate_summary,
            questions=questions,
            simli_face_id=simli_face_id,
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
