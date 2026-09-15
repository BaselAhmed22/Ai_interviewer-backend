from typing import Optional
from pydantic import field_validator
from app.core.schemas_base import CamelModel
from app.interviews.models.report import InterviewReport


class EvaluationDetail(CamelModel):
    """The real, transcript-based Gemini scoring produced by
    app.ai_evaluator.evaluation_agent.EvaluationAgent and persisted onto
    InterviewReport's columns by generate_final_interview_report
    (app/workers/tasks.py)."""

    overall_score: Optional[float] = None
    technical_score: Optional[float] = None
    problem_solving_score: Optional[float] = None
    communication_score: Optional[float] = None
    strengths: list[str] = []
    weaknesses: list[str] = []
    recommendation: Optional[str] = None
    summary: Optional[str] = None


class EvaluationReportResponse(CamelModel):
    """GET /sessions/{id}/report's 200 shape: {success, interviewId,
    evaluation}, matching the AI evaluator's own output contract exactly
    so nothing needs translating between what the evaluator produces,
    what's stored, and what the API returns. Legacy placeholder columns
    (eye_contact_score, posture_score, speech_clarity_score,
    detailed_metrics — see InterviewReport's docstring for why those stay
    in the DB) are intentionally not exposed here."""

    success: bool = True
    interview_id: str
    evaluation: EvaluationDetail

    @field_validator("interview_id", mode="before")
    @classmethod
    def _stringify_uuid(cls, value):
        return str(value) if value is not None else value

    @classmethod
    def from_report(cls, report: InterviewReport) -> "EvaluationReportResponse":
        return cls(
            interview_id=report.session_id,
            evaluation=EvaluationDetail(
                overall_score=report.overall_score,
                technical_score=report.technical_score,
                problem_solving_score=report.problem_solving_score,
                communication_score=report.communication_score,
                strengths=report.strengths or [],
                weaknesses=report.weaknesses or [],
                recommendation=report.recommendation,
                summary=report.summary,
            ),
        )


class ReportPendingResponse(CamelModel):
    """GET /sessions/{id}/report's 202 shape: the session ended and report
    generation was enqueued (see end_session in
    app/interviews/api/sessions.py) but generate_final_interview_report
    hasn't written the row yet — Celery lag, or a dead broker being caught
    later by app.workers.tasks.reconcile_missing_reports. A UI polling
    this should keep retrying, not treat it as an error."""

    status: str = "PENDING"
    session_id: str
    message: str = "The interview has ended and the report is being generated. Please check back shortly."