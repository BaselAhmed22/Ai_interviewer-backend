"""
VoiceAgent — pipeline stage 4.

The live STT -> LLM -> TTS pipeline that actually runs inside a LiveKit
room. This is what app/interviews/workers/livekit_agent.py's entrypoint() builds and
starts for every dispatched interview job — pulled out here so the
pipeline is one importable, testable unit instead of logic inline in the
LiveKit worker process, and so ControllerAgent's prepared questions
(reached via the AgentContext persisted in Redis — see
app.interviews.services.interview_pipeline) can be woven into the interviewer's
instructions instead of a single static prompt.

Providers: read dynamically from app.interviews.providers.factory, which in
turn picks a concrete class per settings.STT_PROVIDER / LLM_PROVIDER /
TTS_PROVIDER (.env — currently openai / openai / elevenlabs as
placeholders while the AI team finalizes real choices). VoiceAgent itself
never names a vendor — swapping one is a config change plus, if it's a
new vendor, one new provider class; nothing here changes either way.
"""
import json
import logging
import uuid

from livekit.agents import Agent, AgentSession, function_tool
from livekit.plugins import silero

from app.interviews.providers.factory import get_llm_provider, get_stt_provider, get_tts_provider
from app.interviews.schemas.agent_context import CandidateSummary, GeneratedQuestion
from app.interviews.services import transcript_service
from app.core.redis_service import redis_service

logger = logging.getLogger(__name__)

BASE_INSTRUCTIONS = (
    "You are Aria, a professional and friendly AI interviewer. "
    "Listen carefully to the candidate's answers and follow up naturally, "
    "but you must NEVER invent your own interview questions — every "
    "question you ask must come, verbatim, from the get_next_question "
    "tool. Call get_next_question to fetch each question (the first one "
    "included) and ask it exactly as returned, word for word — do not "
    "paraphrase, reorder, or add extra technical questions of your own. "
    "Once the tool reports there are no more questions, thank the "
    "candidate and wrap up the interview."
)


def build_instructions(
    candidate_summary: CandidateSummary | None = None,
    candidate_name: str | None = None,
) -> str:
    """Combines the base persona with the candidate's name, if known (see
    entrypoint() in app/interviews/workers/livekit_agent.py — candidate_summary
    comes from either the Redis pipeline context or, if that's expired, the
    durable copy persisted on the session row). The prepared questions
    themselves are deliberately NOT embedded in the prompt text — they are
    served one at a time through the get_next_question function_tool below
    so the LLM cannot drift into asking questions of its own invention."""
    parts = [BASE_INSTRUCTIONS]

    name = (candidate_summary.headline if candidate_summary else None) or candidate_name
    if name:
        parts.append(f"The candidate's name is {name}.")

    return "\n\n".join(parts)


class InterviewerAgent(Agent):
    """LiveKit Agent persona — speaks via the pipeline VoiceAgent configures on its session.

    Enforces that every question asked comes from `questions` (the exact
    list DocumentAgent/QuestionnaireAgent prepared via /interviews/prepare)
    rather than the LLM's own invention: `questions` is never inlined into
    the prompt text, it's only reachable through the get_next_question
    function_tool below, which LiveKit auto-registers because it's defined
    directly on this Agent subclass (see Agent.__init__ -> find_function_tools)."""

    def __init__(
        self,
        room_name: str,
        session_id: str | None = None,
        instructions: str = BASE_INSTRUCTIONS,
        questions: list[GeneratedQuestion] | None = None,
    ) -> None:
        super().__init__(instructions=instructions)
        self._room_name = room_name
        self._session_id = session_id
        self._questions = questions or []
        self._next_index = 0

    @function_tool()
    async def get_next_question(self) -> str:
        """Fetch the next prepared interview question to ask the candidate,
        verbatim. Call this once at the start of the interview and again
        every time you're ready to move on to a new question — never make
        up a question yourself. Returns the exact question text to ask, or
        a message telling you there are no more questions once the list is
        exhausted, at which point you should wrap up the interview."""
        if self._next_index >= len(self._questions):
            logger.info("get_next_question: exhausted (%d asked) for room %s.", self._next_index, self._room_name)
            return "There are no more prepared questions. Thank the candidate and wrap up the interview."

        question = self._questions[self._next_index]
        question_index = self._next_index
        self._next_index += 1
        logger.info(
            "get_next_question: serving question %d/%d for room %s.",
            self._next_index, len(self._questions), self._room_name,
        )
        await self._publish_question_changed(question_index, question)
        return f"[{question.difficulty}] {question.question}"

    async def _publish_question_changed(self, question_index: int, question: GeneratedQuestion) -> None:
        """Notifies the Flutter frontend over LiveKit's data channel that
        the interviewer has moved to a new question, so its UI can sync
        (progress bar, current question text) without polling
        GET /sessions/{id}. Best-effort: a publish failure (room not
        reachable, data channel hiccup) must never break the interview
        itself, so this only logs and moves on."""
        payload = json.dumps({
            "event": "QUESTION_CHANGED",
            "question_index": question_index,
            "total_questions": len(self._questions),
            "question_text": question.question,
        })
        try:
            room = self.session.room_io.room
            await room.local_participant.publish_data(
                payload.encode("utf-8"), reliable=True, topic="interview_state",
            )
        except Exception as exc:
            logger.warning("Failed to publish QUESTION_CHANGED for room %s: %s", self._room_name, exc)

    async def on_enter(self) -> None:
        logger.info("InterviewerAgent entered room.")
        # Written to Redis so the backend (a separate process) has some
        # visibility into whether the AI side of an interview is actually
        # up — see redis_service.set_agent_status and
        # GET /sessions/{id}/agent-status.
        await redis_service.set_agent_status(self._room_name, "connected")

    async def on_exit(self) -> None:
        logger.info("InterviewerAgent exited room.")
        await redis_service.set_agent_status(self._room_name, "disconnected")

        # Final, durable write of the full conversation — session.history
        # is the authoritative source (the same object the live
        # "conversation_item_added" hook mirrors into Redis turn-by-turn
        # for crash safety; see entrypoint() in app/interviews/workers/livekit_agent.py).
        if not self._session_id:
            return
        try:
            turns = transcript_service.build_turns(self.session.history.messages())
            await transcript_service.save_transcript(uuid.UUID(self._session_id), turns)
            logger.info("Saved %d transcript turn(s) for session %s.", len(turns), self._session_id)
        except Exception as exc:
            logger.error("Failed to save transcript for session %s: %s", self._session_id, exc)


class VoiceAgent:
    """Builds the STT -> LLM -> TTS -> VAD pipeline and the interviewer
    persona for one interview room."""

    def build_session(self) -> AgentSession:
        # VAD stays fixed (Silero, local — no vendor/network dependency,
        # so there's nothing to make pluggable here the way there is for
        # STT/LLM/TTS).
        return AgentSession(
            stt=get_stt_provider().to_livekit(),
            llm=get_llm_provider().to_livekit(),
            tts=get_tts_provider().to_livekit(),
            vad=silero.VAD.load(),
        )

    def build_agent(
        self,
        room_name: str,
        session_id: str | None = None,
        questions: list[GeneratedQuestion] | None = None,
        candidate_summary: CandidateSummary | None = None,
        candidate_name: str | None = None,
    ) -> InterviewerAgent:
        return InterviewerAgent(
            room_name=room_name,
            session_id=session_id,
            instructions=build_instructions(candidate_summary, candidate_name),
            questions=questions,
        )
