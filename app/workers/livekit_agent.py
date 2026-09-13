"""
app/workers/livekit_agent.py
-----------------------------
LiveKit Agent - runs as a SEPARATE PROCESS from the main uvicorn server.

Start it with:
    python -m app.workers.livekit_agent start

The agent connects to the LiveKit server, joins every dispatched room,
listens to the candidate's audio via STT, generates a response via the
LLM, and speaks it back via TTS. Without an STT/LLM/TTS pipeline
configured, the agent would join and subscribe to the candidate's audio
but never publish any audio track of its own — which is exactly what
produces "camera/mic work, but there's no sound": the human's local
capture is fine, there is just no second audio track in the room to hear
anything from.

The STT/LLM/TTS/VAD pipeline and interviewer persona live in
app/agents/voice_agent.py (VoiceAgent) — pipeline stage 4 of the
multi-agent interview pipeline (see app/agents/__init__.py and
app/services/interview_pipeline.py). This file is the LiveKit worker
process shell around it: connection lifecycle, job metadata, and worker
registration.

A job dispatched with metadata (the multi-agent pipeline's
preparation_id — see app.services.interview_pipeline and
POST /api/v1/interviews/start) briefs VoiceAgent with the CandidateSummary
and questions DocumentAgent/QuestionnaireAgent already prepared. A job
with no metadata (e.g. dispatched via the plain POST /sessions/start)
falls back to VoiceAgent's base persona.

Dependencies (already in requirements.txt):
    livekit-agents[deepgram,openai,elevenlabs,silero]~=1.0

Required environment variables (this process reads them via os.environ,
loaded from .env below — it does NOT go through app.core.config.settings
since it's a separate process from the FastAPI app):
    OPENAI_API_KEY     - https://platform.openai.com  (STT + LLM)
    ELEVEN_API_KEY     - https://elevenlabs.io          (TTS)
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

# Must run before the plugin imports below: livekit-agents plugins read
# their API keys straight out of os.environ (OPENAI_API_KEY,
# ELEVEN_API_KEY) at construction time. pydantic-settings in
# app.core.config only populates its own Settings object from .env — it
# never touches the real process environment — and this file runs as its
# own separate process anyway, so nothing else would ever load .env for
# it.
load_dotenv()

from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobExecutorType,
    WorkerOptions,
    cli,
)

from app.agents.voice_agent import VoiceAgent
from app.schemas.agent_context import AgentContext
from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)

_REQUIRED_ENV_VARS = ("OPENAI_API_KEY", "ELEVEN_API_KEY")


async def _load_pipeline_context(job_metadata: str) -> AgentContext | None:
    preparation_id = (job_metadata or "").strip()
    if not preparation_id:
        return None
    try:
        raw = await redis_service.get_pipeline_context(preparation_id)
    except Exception as exc:
        logger.error("Failed to load pipeline context %s: %s", preparation_id, exc)
        return None
    if not raw:
        logger.warning(
            "No pipeline context found for preparation_id %s (expired, or never prepared).", preparation_id
        )
        return None
    return AgentContext.model_validate_json(raw)


async def entrypoint(ctx: JobContext) -> None:
    """
    Entry point for each interview room: connect, load the multi-agent
    pipeline context if this job was dispatched with one, start the
    VoiceAgent session (STT -> LLM -> TTS, with VAD for turn-taking), wait
    for the actual candidate to be in the room, then greet them.
    """
    room_name = ctx.room.name
    logger.info("Agent joining room: %s", room_name)

    # AUDIO_ONLY: this agent never looks at video, so subscribing to it too
    # would just cost bandwidth/CPU for tracks it never uses.
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    context = await _load_pipeline_context(ctx.job.metadata)

    voice_agent = VoiceAgent()
    session = voice_agent.build_session()

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

    await session.start(agent=voice_agent.build_agent(room_name, context=context), room=ctx.room)

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
