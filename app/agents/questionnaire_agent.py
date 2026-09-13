# app/agents/questionnaire_agent.py
"""
QuestionnaireAgent — pipeline stage 2.

Turns a CandidateSummary (DocumentAgent's output) plus the active job
title into an ordered list of interview questions. ControllerAgent then
owns walking through this list live.

Placeholder implementation: a handful of generic, job-title-templated
questions — not real question generation. The AI team should replace
`generate()` with an LLM-driven implementation grounded in
candidate_summary and job_title (skills gaps to probe, project
highlights to ask about, seniority-appropriate depth) — keep the same
input/output shape so ControllerAgent doesn't need to change.
"""
from app.schemas.agent_context import CandidateSummary


class QuestionnaireAgent:
    def generate(self, candidate_summary: CandidateSummary, job_title: str) -> list[str]:
        questions = [
            f"Can you walk me through your experience relevant to the {job_title} role?",
            "Tell me about a challenging technical problem you solved recently.",
            f"Why are you interested in this {job_title} position?",
            "Where do you see yourself growing professionally over the next few years?",
        ]
        if candidate_summary.key_skills:
            questions.insert(
                1,
                f"I see {candidate_summary.key_skills[0]} on your CV — can you describe "
                "a project where you used it?",
            )
        return questions
