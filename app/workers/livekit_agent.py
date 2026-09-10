"""
app/workers/livekit_agent.py
-----------------------------
LiveKit Agent - runs as a SEPARATE PROCESS from the main uvicorn server.

Start it with:
    python -m app.workers.livekit_agent dev

The agent connects to the LiveKit server, joins every new room, listens to
the candidate's audio via STT, generates a response via the LLM, and
speaks it back via TTS. Without an STT/LLM/TTS pipeline configured, the
agent would join and subscribe to the candidate's audio but never publish
any audio track of its own — which is exactly what produces "camera/mic
work, but there's no sound": the human's local capture is fine, there is
just no second audio track in the room to hear anything from.

Dependencies (already in requirements.txt):
    livekit-agents[deepgram,openai,elevenlabs,silero]~=1.0

Required environment variables (this process reads them via os.environ,
loaded from .env below — it does NOT go through app.core.config.settings
since it's a separate process from the FastAPI app):
    DEEPGRAM_API_KEY   - https://console.deepgram.com
    OPENAI_API_KEY     - https://platform.openai.com
    ELEVEN_API_KEY     - https://elevenlabs.io
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

# Must run before the plugin imports below: livekit-agents plugins read
# their API keys straight out of os.environ (DEEPGRAM_API_KEY,
# OPENAI_API_KEY, ELEVEN_API_KEY) at construction time. pydantic-settings
# in app.core.config only populates its own Settings object from .env —
# it never touches the real process environment — and this file runs as
# its own separate process anyway, so nothing else would ever load .env
# for it.
load_dotenv()

from livekit.agents import (
    AgentSession,
    Agent,
    AutoSubscribe,
    JobContext,
    JobExecutorType,
    WorkerOptions,
    cli,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero

from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)

_REQUIRED_ENV_VARS = ("DEEPGRAM_API_KEY", "OPENAI_API_KEY", "ELEVEN_API_KEY")


class InterviewerAgent(Agent):
    """AI Interviewer agent — speaks via the STT/LLM/TTS pipeline configured on its AgentSession."""

    def __init__(self, room_name: str) -> None:
        super().__init__(instructions=(
            "You are Aria, a professional and friendly AI interviewer. "
            "Ask the candidate concise, relevant questions based on their "
            "CV and the job description, listen carefully to their answers, "
            "and follow up where appropriate."
        ))
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


async def entrypoint(ctx: JobContext) -> None:
    """
    Entry point for each interview room: connect, start the AgentSession
    (STT -> LLM -> TTS, with VAD for turn-taking), wait for the actual
    candidate to be in the room, then greet them.
    """
    room_name = ctx.room.name
    logger.info("Agent joining room: %s", room_name)

    # AUDIO_ONLY: this agent never looks at video, so subscribing to it too
    # would just cost bandwidth/CPU for tracks it never uses.
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    session = AgentSession(
        stt=deepgram.STT(),
        llm=openai.LLM(model="gpt-4o"),
        tts=elevenlabs.TTS(),
        vad=silero.VAD.load(),
    )

    @session.on("error")
    def _on_session_error(ev) -> None:
        # Fires when the STT/LLM/TTS pipeline itself fails (confirmed
        # live: an exhausted OpenAI quota surfaces here) — previously this
        # only ever reached the agent process's own log, with the
        # InterviewSession in the backend's DB staying IN_PROGRESS as if
        # nothing were wrong and no one aware the AI had gone silent.
        # session.on callbacks run synchronously, so the Redis write is
        # scheduled as a task rather than awaited directly.
        detail = str(ev.error)[:500]
        logger.error("AgentSession error in room %s (source=%s): %s", room_name, ev.source, detail)
        asyncio.create_task(redis_service.set_agent_status(room_name, "degraded", detail=detail))

    await session.start(agent=InterviewerAgent(room_name=room_name), room=ctx.room)

    # The agent can finish connecting and starting faster than the
    # candidate's own client does (dispatch is near-instant; a phone/app
    # still has to open, get mic permission, negotiate WebRTC, ...).
    # Greeting immediately on session start — before checking for this —
    # risks the one-shot welcome finishing before the candidate has
    # joined at all, so they arrive to silence having missed it entirely.
    participant = await ctx.wait_for_participant()
    logger.info("Candidate detected: %s", participant.identity)
    await session.generate_reply(
        instructions="Greet the candidate warmly, introduce yourself as Aria, and ask if they're ready to begin."
    )


if __name__ == "__main__":
    from app.core.config import settings

    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        # Fail loudly and immediately with the exact missing names,
        # instead of starting the worker and only discovering the gap
        # deep inside a plugin's constructor the first time a real
        # interview room tries to use it.
        logger.error(
            "Missing required environment variable(s) for the voice pipeline: %s. "
            "Set them in .env before starting this worker — see the module "
            "docstring for where to get each key.",
            ", ".join(missing),
        )
        sys.exit(1)

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
            ws_url=settings.LIVEKIT_URL,
            # Required for the backend's explicit dispatch call
            # (livekit_service.dispatch_agent) to be able to target this
            # worker by name — without it, this worker only ever receives
            # LiveKit's automatic per-room dispatch. Must match
            # settings.LIVEKIT_AGENT_NAME exactly.
            agent_name=settings.LIVEKIT_AGENT_NAME,
            # Default is THREAD (all concurrent interview jobs share one
            # process/GIL). PROCESS gives each concurrent session's own
            # OS process — its own event loop, its own memory, one
            # session's STT/LLM/TTS load or a crash can't starve or take
            # down any other candidate's interview.
            job_executor_type=JobExecutorType.PROCESS,
        )
    )