"""
EvaluationAgent — pipeline stage 5.

Produces the final interview report once a session ends. This is what
app.workers.tasks.generate_final_interview_report (the Celery task,
dispatched automatically by POST /sessions/end/{id} and explicitly by
POST /api/v1/interviews/evaluate) delegates to.

Split into two layers, on purpose:
  - load_evaluation_inputs() / _pair_questions_and_answers(): pulls the
    session's snapshotted questions + candidate_summary, the user's
    active job description, and the real InterviewTranscript.turns log
    from Postgres, and reconstructs per-question (question, answer)
    pairs from that chronological turns log — this is the "parse turns
    JSON" step. Async, DB-facing, ours.
  - EvaluationAgent.evaluate(): the AI team's scoring engine, verbatim
    prompt/parsing logic — a pure function of already-resolved primitives
    (candidate_name, candidate_analysis, job_description, questions,
    answers), no DB or async concerns. Sync (Gemini's client call is
    blocking), called directly from the sync Celery task.

generate_final_interview_report (app/workers/tasks.py) wires these two
together and flattens the returned EvaluationResult into InterviewReport's
columns; this module has no knowledge of Postgres writes at all.
"""
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

from google import genai
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.interviews.models.session import InterviewSession
from app.interviews.models.transcript import InterviewTranscript
from app.candidates.models import JobDescription

logger = logging.getLogger(__name__)

_MODEL = "gemini-3.6-flash"

_PROMPT_TEMPLATE = """
You are an expert technical interview evaluator.
Evaluate the candidate based on the COMPLETE interview.
Candidate name: {candidate_name}
Candidate analysis: {candidate_analysis}
Job description: {job_description}
Interview questions and candidate answers: {interview_data}
Evaluate across: Technical Knowledge, Problem Solving, Communication.
Return ONLY valid JSON using exactly this structure:
{{
    "overall_score": 0,
    "technical_score": 0,
    "problem_solving_score": 0,
    "communication_score": 0,
    "strengths": [],
    "weaknesses": [],
    "recommendation": "",
    "summary": ""
}}
"""


@dataclass
class EvaluationResult:
    overall_score: float
    technical_score: float
    problem_solving_score: float
    communication_score: float
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    recommendation: str = ""
    summary: str = ""

    def to_dict(self):
        return {
            "overall_score": self.overall_score,
            "technical_score": self.technical_score,
            "problem_solving_score": self.problem_solving_score,
            "communication_score": self.communication_score,
            "strengths": self.strengths,
            "weaknesses": self.weaknesses,
            "recommendation": self.recommendation,
            "summary": self.summary,
        }


class EvaluationAgent:
    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None
        self.model = _MODEL

    def _get_client(self) -> genai.Client:
        # Lazy, like every other Gemini-backed agent in this codebase
        # (DocumentAgent, QuestionnaireAgent) — evaluation_agent is
        # instantiated at app/workers/tasks.py's module level, so raising
        # eagerly here (as the AI team's original __init__ did, reading
        # os.getenv directly) would crash the whole Celery worker process
        # at import time if GEMINI_API_KEY is merely unset in this
        # environment, instead of failing just the one task that needs it.
        if self._client is None:
            api_key = settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not set.")
            self._client = genai.Client(api_key=api_key)
        return self._client

    def evaluate(
        self,
        candidate_name: str,
        candidate_analysis: dict,
        job_description: str,
        questions: list[dict],
        answers: list[dict],
    ) -> EvaluationResult:
        """Verbatim scoring logic from the AI team: pairs each question
        (by id) with its matching answer, builds the prompt, and parses
        Gemini's response — including stripping a ```json fence if the
        model wraps its output in one, since response_mime_type isn't
        forced here."""
        interview_data = []
        for question in questions:
            question_id = question["id"]
            answer = next((a["answer"] for a in answers if a["question_id"] == question_id), "")
            interview_data.append({
                "question": question["question"],
                "category": question.get("category", "General"),
                "difficulty": question.get("difficulty", "medium"),
                "answer": answer,
            })

        prompt = _PROMPT_TEMPLATE.format(
            candidate_name=candidate_name,
            candidate_analysis=json.dumps(candidate_analysis, indent=2),
            job_description=job_description,
            interview_data=json.dumps(interview_data, indent=2),
        )

        response = self._get_client().models.generate_content(model=self.model, contents=prompt)
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw_text)

        return EvaluationResult(
            overall_score=float(result["overall_score"]),
            technical_score=float(result["technical_score"]),
            problem_solving_score=float(result["problem_solving_score"]),
            communication_score=float(result["communication_score"]),
            strengths=result.get("strengths", []),
            weaknesses=result.get("weaknesses", []),
            recommendation=result.get("recommendation", ""),
            summary=result.get("summary", ""),
        )


def _pair_questions_and_answers(
    questions: list[dict], turns: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Reconstructs (questions_with_id, answers) from InterviewTranscript's
    chronological turns log ([{speaker: "agent"|"candidate", text, timestamp}, ...]
    — see app.interviews.services.transcript_service).

    Our prepared questions (InterviewSession.questions) have no stable id
    of their own, so ids are synthesized as list position. Each question
    is matched to the AGENT turn that asked it by exact substring
    containment, not fuzzy text similarity — InterviewerAgent is forced
    to speak each prepared question verbatim via its get_next_question
    tool (see app/interviews/agents/voice_agent.py), so this is a
    reliable match, not a guess. Every CANDIDATE turn between that match
    and the next matched question's turn is concatenated as the answer;
    a question the agent never reached (interview cut short) simply gets
    no answer entry, and EvaluationAgent.evaluate() already defaults that
    to "" via its own lookup.
    """
    indexed_questions = [
        {
            "id": i,
            "question": q.get("question", ""),
            "category": q.get("category", "General"),
            "difficulty": q.get("difficulty", "medium"),
        }
        for i, q in enumerate(questions)
    ]

    match_positions: dict[int, int] = {}
    search_from = 0
    for q in indexed_questions:
        text = q["question"].strip()
        if not text:
            continue
        for idx in range(search_from, len(turns)):
            turn = turns[idx]
            if turn.get("speaker") == "agent" and text in (turn.get("text") or ""):
                match_positions[q["id"]] = idx
                search_from = idx + 1
                break

    ordered_ids = sorted(match_positions, key=lambda qid: match_positions[qid])
    answers = []
    for pos, qid in enumerate(ordered_ids):
        start = match_positions[qid] + 1
        end = match_positions[ordered_ids[pos + 1]] if pos + 1 < len(ordered_ids) else len(turns)
        answer_text = " ".join(
            turn["text"] for turn in turns[start:end] if turn.get("speaker") == "candidate"
        ).strip()
        answers.append({"question_id": qid, "answer": answer_text})

    return indexed_questions, answers


async def load_evaluation_inputs(session_id: str) -> tuple[str, dict, str, list[dict], list[dict]]:
    """Pulls everything EvaluationAgent.evaluate() needs straight from
    Postgres. Opens its own short-lived DB session: this runs inside a
    Celery task, which has no request-scoped session to reuse (same
    reasoning as transcript_service.save_transcript and
    livekit_agent.py's _load_persisted_session)."""
    session_uuid = uuid.UUID(session_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_uuid))
        session_obj = result.scalars().first()
        if not session_obj:
            raise ValueError(f"No InterviewSession found for {session_id}")

        transcript_result = await db.execute(
            select(InterviewTranscript).where(InterviewTranscript.session_id == session_uuid)
        )
        transcript_obj = transcript_result.scalars().first()

        # InterviewSession doesn't itself snapshot job description text
        # (only questions + candidate_summary are persisted at
        # /interviews/start) — this is the best available source. If the
        # candidate changed their active job description mid-interview,
        # this reflects the current one, not necessarily the one the
        # questions were originally generated against.
        job_result = await db.execute(
            select(JobDescription).where(
                JobDescription.user_id == session_obj.user_id, JobDescription.is_active.is_(True)
            )
        )
        job = job_result.scalars().first()

    turns = transcript_obj.turns if transcript_obj and transcript_obj.turns else []
    candidate_analysis = session_obj.candidate_summary or {}
    candidate_name = candidate_analysis.get("headline") or "Candidate"
    job_description = job.description_text if job else ""

    questions, answers = _pair_questions_and_answers(session_obj.questions or [], turns)
    return candidate_name, candidate_analysis, job_description, questions, answers
