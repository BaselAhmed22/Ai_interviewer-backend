# app/agents/document_agent.py
"""
DocumentAgent — pipeline stage 1.

Turns a candidate's already-parsed CV into a structured summary the rest
of the pipeline can use. CV *text extraction* already happens earlier and
separately, in the existing Celery pipeline (see
app.workers.tasks.process_cv_analysis / app.services.cv_parser) — by the
time DocumentAgent runs, the raw text and skills string are already
available. This agent's job is the next step up: making that raw text
useful to QuestionnaireAgent and VoiceAgent.

Placeholder implementation: wraps raw_cv_text/skills into a
CandidateSummary without any real analysis. The AI team should replace
`summarize()` with actual extraction (seniority signal, standout skills
relative to the job description, notable projects, etc.) — keep the same
input/output shape so QuestionnaireAgent doesn't need to change.
"""
from typing import Optional

from app.schemas.agent_context import CandidateSummary


class DocumentAgent:
    def summarize(
        self,
        raw_cv_text: Optional[str],
        skills: Optional[str],
        full_name: Optional[str],
    ) -> CandidateSummary:
        parsed_skills = [s.strip() for s in (skills or "").split(",") if s.strip()]
        return CandidateSummary(
            headline=full_name,
            key_skills=parsed_skills,
            raw_cv_excerpt=(raw_cv_text or "")[:1000] or None,
        )
