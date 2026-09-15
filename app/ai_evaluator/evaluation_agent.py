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
import asyncio
import difflib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from google import genai
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis_service import redis_service
from app.interviews.agents.gemini_retry import call_with_retry
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
        if self._client is None:
            api_key = (
                getattr(settings, "GEMINI_EVAL_API_KEY", None)
                or os.getenv("GEMINI_EVAL_API_KEY")
                or settings.GEMINI_API_KEY
                or os.getenv("GEMINI_API_KEY")
            )
            if not api_key:
                raise RuntimeError("Neither GEMINI_EVAL_API_KEY nor GEMINI_API_KEY is set.")
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
        """Evaluates the candidate's interview responses using Gemini.
        Forces JSON response_mime_type, wraps call in retry logic, and parses output
        with a robust fallback JSON extractor."""
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

        response = call_with_retry(
            lambda: self._get_client().models.generate_content(
                model=self.model,
                contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0.2},
            )
        )
        raw_text = response.text.strip() if response.text else ""

        result = self._parse_json_response(raw_text)

        return EvaluationResult(
            overall_score=float(result.get("overall_score", 0)),
            technical_score=float(result.get("technical_score", 0)),
            problem_solving_score=float(result.get("problem_solving_score", 0)),
            communication_score=float(result.get("communication_score", 0)),
            strengths=result.get("strengths", []),
            weaknesses=result.get("weaknesses", []),
            recommendation=result.get("recommendation", ""),
            summary=result.get("summary", ""),
        )

    @staticmethod
    def _parse_json_response(raw_text: str) -> dict:
        if not raw_text:
            raise ValueError("Empty response text from Gemini model.")

        # 1. Direct JSON parse
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            pass

        # 2. Strip code block wrappers
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            try:
                return json.loads(cleaned.strip())
            except json.JSONDecodeError:
                pass

        # 3. Regex extraction fallback
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0).strip())
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Failed to parse valid JSON from Gemini response: {raw_text[:200]}")


def _clean_text_for_matching(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def _is_question_match(q_text: str, turn_text: str) -> bool:
    q_clean = _clean_text_for_matching(q_text)
    turn_clean = _clean_text_for_matching(turn_text)

    if not q_clean or not turn_clean:
        return False

    # 1. Exact or substring containment
    if q_clean in turn_clean or turn_clean in q_clean:
        return True

    # 2. Token overlap ratio (portion of question words present in agent turn)
    q_words = set(q_clean.split())
    turn_words = set(turn_clean.split())
    if q_words:
        overlap = len(q_words & turn_words) / len(q_words)
        if overlap >= 0.5:
            return True

    # 3. Fuzzy sequence matcher
    ratio = difflib.SequenceMatcher(None, q_clean, turn_clean).ratio()
    return ratio >= 0.5


def _pair_questions_and_answers(
    questions: list[dict], turns: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Reconstructs (questions_with_id, answers) from InterviewTranscript's
    chronological turns log using resilient fuzzy matching and token overlap."""
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
        q_text = q["question"].strip()
        if not q_text:
            continue

        for idx in range(search_from, len(turns)):
            turn = turns[idx]
            if turn.get("speaker") == "agent":
                turn_text = turn.get("text") or ""
                if _is_question_match(q_text, turn_text):
                    match_positions[q["id"]] = idx
                    search_from = idx + 1
                    break

    # Sequential fallback for unmatched questions
    agent_turn_indices = [i for i, t in enumerate(turns) if t.get("speaker") == "agent"]
    if agent_turn_indices:
        last_matched_idx = -1
        for q in indexed_questions:
            qid = q["id"]
            if qid in match_positions:
                last_matched_idx = match_positions[qid]
            else:
                available = [
                    idx for idx in agent_turn_indices
                    if idx > last_matched_idx and idx not in match_positions.values()
                ]
                if available:
                    fallback_idx = available[0]
                    match_positions[qid] = fallback_idx
                    last_matched_idx = fallback_idx

    ordered_ids = sorted(match_positions, key=lambda qid: match_positions[qid])
    answers = []

    if ordered_ids:
        for pos, qid in enumerate(ordered_ids):
            start = match_positions[qid] + 1
            end = match_positions[ordered_ids[pos + 1]] if pos + 1 < len(ordered_ids) else len(turns)
            answer_text = " ".join(
                turn["text"] for turn in turns[start:end]
                if turn.get("speaker") == "candidate" and turn.get("text")
            ).strip()
            answers.append({"question_id": qid, "answer": answer_text})
    else:
        # Ultimate fallback: concatenate all candidate turns if agent turn pairing completely failed
        candidate_turns = " ".join(
            turn["text"] for turn in turns
            if turn.get("speaker") == "candidate" and turn.get("text")
        ).strip()
        for q in indexed_questions:
            answers.append({"question_id": q["id"], "answer": candidate_turns if q["id"] == 0 else ""})

    return indexed_questions, answers


async def load_evaluation_inputs(session_id: str) -> tuple[str, dict, str, list[dict], list[dict]]:
    """Pulls session, transcript, and job description for evaluation.
    Includes Redis live buffer fallback and retry mechanisms to handle race conditions
    when postgres transcript commit hasn't completed yet."""
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

        job_result = await db.execute(
            select(JobDescription).where(
                JobDescription.user_id == session_obj.user_id, JobDescription.is_active.is_(True)
            )
        )
        job = job_result.scalars().first()

    turns = transcript_obj.turns if transcript_obj and transcript_obj.turns else []

    # Handle asynchronous race conditions:
    # If transcript is missing or turns are empty in Postgres, fallback to Redis live turn buffer
    if not turns and session_obj.room_name:
        for attempt in range(3):
            try:
                redis_turns = await redis_service.get_transcript_turns(session_obj.room_name)
                if redis_turns:
                    turns = redis_turns
                    logger.info(
                        "Loaded %d turns from Redis fallback for room %s (attempt %d).",
                        len(turns), session_obj.room_name, attempt + 1
                    )
                    break
            except Exception as redis_exc:
                logger.warning("Error fetching turns from Redis for room %s: %s", session_obj.room_name, redis_exc)

            if attempt < 2:
                await asyncio.sleep(1.0)
                async with AsyncSessionLocal() as db:
                    t_res = await db.execute(
                        select(InterviewTranscript).where(InterviewTranscript.session_id == session_uuid)
                    )
                    t_obj = t_res.scalars().first()
                    if t_obj and t_obj.turns:
                        turns = t_obj.turns
                        logger.info("Transcript turns found in Postgres on retry attempt %d.", attempt + 2)
                        break

    candidate_analysis = session_obj.candidate_summary or {}
    candidate_name = candidate_analysis.get("headline") or "Candidate"
    job_description = job.description_text if job else ""

    questions, answers = _pair_questions_and_answers(session_obj.questions or [], turns)
    return candidate_name, candidate_analysis, job_description, questions, answers

