"""
DocumentAgent — pipeline stage 1.

Analyzes a candidate's CV against the job description via Gemini,
producing the CandidateSummary QuestionnaireAgent builds questions
from. The Gemini call itself is synchronous (google-genai's default
client) — run off the event loop via asyncio.to_thread so a slow
response doesn't stall this FastAPI process for every other request in
flight, same reasoning as the Google OAuth cert-fetch call in
app/core/security.py.
"""
import asyncio
import json
from typing import Optional

from google import genai

from app.core.config import settings
from app.interviews.schemas.agent_context import CandidateSummary

_MODEL = "gemini-2.5-flash"

_PROMPT_TEMPLATE = """
Analyze this candidate CV against this job description.

Your output will be used later to generate technical interview questions.

Return ONLY JSON.

Do NOT explain anything.
Do NOT repeat the CV.
Do NOT repeat the job description.
Do NOT invent information.
Keep the output concise.

Return exactly this structure:

{{
    "candidate_name": "",
    "skills": [],
    "experience": [],
    "projects": [],
    "required_skills": [],
    "matching_skills": [],
    "missing_skills": [],
    "profile": ""
}}

Rules:

- candidate_name: candidate's name if available.
- skills: important technical skills found in the CV. Maximum 15.
- experience: only relevant experience. Maximum 5 short items.
- projects: only relevant technical projects. Maximum 5 short items.
- required_skills: important skills required by the job. Maximum 15.
- matching_skills: required skills that are clearly present in the CV.
- missing_skills: required skills that are not clearly present in the CV.
- profile: maximum 2 short sentences describing the candidate's technical background.

Keep every item short.
Do not include unnecessary details.

================ CV ================

{cv_text}

================ JOB DESCRIPTION ================

{job_description}
"""


class DocumentAgent:
    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None

    def _get_client(self) -> genai.Client:
        # Lazy: this class is instantiated at module import time, so
        # raising here would crash the whole app on a missing key instead
        # of just this endpoint on first use.
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set.")
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    def _analyze_sync(self, cv_text: str, job_description: str) -> dict:
        response = self._get_client().models.generate_content(
            model=_MODEL,
            contents=_PROMPT_TEMPLATE.format(cv_text=cv_text, job_description=job_description),
            config={"response_mime_type": "application/json", "temperature": 0.1},
        )
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ValueError("Gemini returned invalid JSON for CV analysis.") from exc

    async def summarize(
        self, raw_cv_text: Optional[str], job_description: str, full_name: Optional[str]
    ) -> CandidateSummary:
        cv_text = (raw_cv_text or "").strip()
        if not cv_text:
            # No parsed CV text yet (extraction still pending, or came
            # back empty) — degrade to a bare summary instead of sending
            # Gemini nothing useful to analyze.
            return CandidateSummary(headline=full_name)

        analysis = await asyncio.to_thread(self._analyze_sync, cv_text, job_description)

        return CandidateSummary(
            headline=analysis.get("candidate_name") or full_name,
            key_skills=analysis.get("skills") or [],
            raw_cv_excerpt=cv_text[:1000] or None,
            experience=analysis.get("experience") or [],
            projects=analysis.get("projects") or [],
            required_skills=analysis.get("required_skills") or [],
            matching_skills=analysis.get("matching_skills") or [],
            missing_skills=analysis.get("missing_skills") or [],
            profile=analysis.get("profile") or None,
        )
