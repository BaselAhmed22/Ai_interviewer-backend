"""
VoiceAgent — pipeline stage 4.

The live STT -> LLM -> TTS pipeline that actually runs inside a LiveKit
room. This is what app/workers/livekit_agent.py's entrypoint() builds and
starts for every dispatched interview job — pulled out here so the
pipeline is one importable, testable unit instead of logic inline in the
LiveKit worker process, and so ControllerAgent's prepared questions
(reached via the AgentContext persisted in Redis — see
app.services.interview_pipeline) can be woven into the interviewer's
instructions instead of a single static prompt.

Providers: read dynamically from app.core.providers.factory, which in
turn picks a concrete class per settings.STT_PROVIDER / LLM_PROVIDER /
TTS_PROVIDER (.env — currently openai / openai / elevenlabs as
placeholders while the AI team finalizes real choices). VoiceAgent itself
never names a vendor — swapping one is a config change plus, if it's a
new vendor, one new provider class; nothing here changes either way.
"""
import logging

from livekit.agents import Agent, AgentSession
from livekit.plugins import silero

from app.core.providers.factory import get_llm_provider, get_stt_provider, get_tts_provider
from app.schemas.agent_context import CandidateSummary
from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)

BASE_INSTRUCTIONS = (
    "You are Aria, a professional and friendly AI interviewer. "
    "Ask the candidate concise, relevant questions based on their CV and "
    "the job description, listen carefully to their answers, and follow "
    "up where appropriate."
)


def build_instructions(
    questions: list[str] | None = None,
    candidate_summary: CandidateSummary | None = None,
    candidate_name: str | None = None,
) -> str:
    """Combines the base persona with whatever DocumentAgent/
    QuestionnaireAgent prepared, if any (see entrypoint() in
    app/workers/livekit_agent.py — questions/candidate_summary come from
    either the Redis pipeline context or, if that's expired, the durable
    copy persisted on the session row). Falls back to just the candidate's
    name for the plain /sessions/start path (no prepared question list
    there), or the bare persona if neither is available."""
    parts = [BASE_INSTRUCTIONS]

    name = (candidate_summary.headline if candidate_summary else None) or candidate_name
    if name:
        parts.append(f"The candidate's name is {name}.")

    if questions:
        numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        parts.append(
            "Guide the conversation through these prepared questions, one at a "
            f"time, adapting naturally to the candidate's answers:\n{numbered}"
        )

    return "\n\n".join(parts)


class InterviewerAgent(Agent):
    """LiveKit Agent persona — speaks via the pipeline VoiceAgent configures on its session."""

    def __init__(self, room_name: str, instructions: str = BASE_INSTRUCTIONS) -> None:
        super().__init__(instructions=instructions)
        self._room_name = room_name

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
        questions: list[str] | None = None,
        candidate_summary: CandidateSummary | None = None,
        candidate_name: str | None = None,
    ) -> InterviewerAgent:
        return InterviewerAgent(
            room_name=room_name,
            instructions=build_instructions(questions, candidate_summary, candidate_name),
        )
