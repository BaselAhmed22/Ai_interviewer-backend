# app/services/interview_pipeline.py
"""
InterviewPipelineManager — orchestrates the five-stage agent pipeline:

    DocumentAgent -> QuestionnaireAgent -> ControllerAgent -> VoiceAgent -> EvaluationAgent

Two durability layers, not one:
- Redis (via redis_service.save_pipeline_context/get_pipeline_context):
  the live AgentContext for one in-flight pipeline run — candidate
  summary, generated questions, current question index. Ephemeral by
  design (TTL) — this is working state for one interview attempt, not a
  permanent record.
- PostgreSQL (the existing InterviewSession / InterviewReport tables,
  unchanged by this module): the durable record of the interview itself
  and its final evaluation.

See app/api/v1/endpoints/interviews.py for how the three pipeline
endpoints (prepare/start/evaluate) drive this.
"""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.agents.document_agent import DocumentAgent
from app.agents.questionnaire_agent import QuestionnaireAgent
from app.db.models.user import CandidateProfile, JobDescription
from app.schemas.agent_context import AgentContext, PipelineStage
from app.services.redis_service import redis_service

document_agent = DocumentAgent()
questionnaire_agent = QuestionnaireAgent()


class InterviewPipelineManager:
    async def prepare(self, db: AsyncSession, user_uuid: uuid.UUID) -> AgentContext:
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

        candidate_summary = document_agent.summarize(
            raw_cv_text=profile.raw_cv_text, skills=profile.skills, full_name=profile.full_name
        )
        questions = questionnaire_agent.generate(candidate_summary, job.job_title)

        now = datetime.now(timezone.utc)
        context = AgentContext(
            preparation_id=str(uuid.uuid4()),
            user_id=str(user_uuid),
            candidate_profile_id=str(profile.id),
            job_description_id=str(job.id),
            stage=PipelineStage.QUESTIONNAIRE,
            candidate_summary=candidate_summary,
            questions=questions,
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
        """Stage 3: hands the prepared context to ControllerAgent — from
        here, question sequencing is driven by whatever's reading this
        context next (VoiceAgent, in the LiveKit agent process, receives
        preparation_id as its job's dispatch metadata and loads this same
        context back out of Redis — see app/workers/livekit_agent.py)."""
        context.session_id = str(session_id)
        context.stage = PipelineStage.VOICE
        context.updated_at = datetime.now(timezone.utc)
        await self._save(context)
        return context

    async def _save(self, context: AgentContext) -> None:
        await redis_service.save_pipeline_context(context.preparation_id, context.model_dump_json())


interview_pipeline_manager = InterviewPipelineManager()
