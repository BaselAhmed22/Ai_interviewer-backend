"""
QuestionnaireAgent — pipeline stage 2.

Turns DocumentAgent's CandidateSummary plus the job title into an
ordered list of interview questions via Gemini. The prompt is tuned for
spoken delivery (short, one concept per question) since these are read
aloud by VoiceAgent, not displayed as text. ControllerAgent then owns
walking through this list live.
"""
import asyncio
import json
from typing import Optional

from google import genai

from app.core.config import settings
from app.schemas.agent_context import CandidateSummary

_MODEL = "gemini-2.5-flash"

_PROMPT_TEMPLATE = """
Generate {number_of_questions} technical interview questions
for this candidate and job.

The questions will be used in a spoken AI Engineering interview.

Use the candidate's skills, projects, experience, and the job
requirements to make the questions relevant.

Return ONLY valid JSON.

Do not explain anything.
Do not include answers.
Do not include any text outside the JSON.

Return exactly this structure:

{{
    "questions": [
        {{
            "question": "",
            "category": ""
        }}
    ]
}}

================ QUESTION RULES ================

Generate exactly {number_of_questions} questions.

Each question MUST:

- Be short and natural for voice.
- Be between 8 and 20 words whenever possible.
- Contain ONE main question only.
- Test ONE technical concept.
- Be easy to understand when spoken aloud.
- Be directly relevant to the candidate or job.
- Be specific enough to test technical knowledge.
- Sound like something a human interviewer would naturally ask.

Avoid:

- Long introductions.
- Repeating the candidate's project description.
- Unnecessary context.
- Multiple questions in one sentence.
- "Can you describe..." when a shorter question works.
- "Can you explain..." when a shorter question works.
- Questions with several clauses.
- Questions containing "and how", "and why", "and what", etc.
- Asking for multiple things in the same question.
- Long scenario descriptions.
- Academic or overly formal wording.
- Compound questions.
- More than 25 words.

IMPORTANT:

The question should NOT repeat information that is already obvious
from the candidate's CV.

Instead of:

"In your exoplanet detection pipeline, you used a Random Forest
model for classification. Can you describe a scenario where you
might opt for a deep learning approach instead for such a task?"

Generate:

"When would you choose deep learning instead of Random Forest?"

================ GOOD QUESTIONS ================

GOOD:

"What is overfitting?"

"How would you reduce overfitting?"

"When would you use deep learning instead of Random Forest?"

"How would you reduce latency in an OpenCV pipeline?"

"What is the purpose of a validation set?"

"How would you handle missing data?"

"Why is normalization useful in machine learning?"

"What is the difference between TCP and UDP?"

================ IMPORTANT VOICE RULE ================

Imagine a human interviewer saying the question aloud.

If the question feels too long to say comfortably in one breath,
rewrite it shorter.

================ CANDIDATE / JOB ANALYSIS ================

{analysis}
"""


class QuestionnaireAgent:
    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set.")
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    def _generate_sync(self, prompt: str, number_of_questions: int) -> list[dict]:
        response = self._get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json", "temperature": 0.7},
        )
        try:
            result = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ValueError("Gemini returned invalid JSON for question generation.") from exc

        questions = result.get("questions")
        if not isinstance(questions, list) or len(questions) != number_of_questions:
            raise ValueError(f"Gemini did not return exactly {number_of_questions} questions.")
        return questions

    async def generate(
        self, candidate_summary: CandidateSummary, job_title: str, number_of_questions: int = 5
    ) -> list[str]:
        analysis = {
            "candidate_name": candidate_summary.headline,
            "job_title": job_title,
            "skills": candidate_summary.key_skills,
            "experience": candidate_summary.experience,
            "projects": candidate_summary.projects,
            "required_skills": candidate_summary.required_skills,
            "matching_skills": candidate_summary.matching_skills,
            "missing_skills": candidate_summary.missing_skills,
            "profile": candidate_summary.profile,
        }
        prompt = _PROMPT_TEMPLATE.format(
            number_of_questions=number_of_questions,
            analysis=json.dumps(analysis, ensure_ascii=False),
        )
        items = await asyncio.to_thread(self._generate_sync, prompt, number_of_questions)
        return [q for item in items if (q := str(item.get("question", "")).strip())]
