"""
LiveKit Agent worker — runs as a separate process from the main uvicorn
server.

Start it with:
    python -m app.interviews.workers.livekit_agent start

Connects to the LiveKit server, joins every dispatched room, and runs
STT -> LLM -> TTS on the candidate's audio. The pipeline and interviewer
persona live in app/interviews/agents/voice_agent.py (VoiceAgent); this file is the
process shell around it — connection lifecycle, job metadata, worker
registration, and the Simli avatar joining alongside VoiceAgent.

Every dispatched job carries a small JSON metadata object (session_id,
user_id, preparation_id — see app/interviews/api/pipeline.py's
start_interview_pipeline). Questions and candidate analysis are read from the Redis
pipeline context first, falling back to the durable copy on the session
row (InterviewSession.questions / .candidate_summary) if that's expired.

The candidate/agent conversation itself is captured live via the
AgentSession's "conversation_item_added" event (mirrored into Redis turn
by turn for crash safety) and written to Postgres in full — see
app.interviews.services.transcript_service and InterviewerAgent.on_exit() in
app/interviews/agents/voice_agent.py — retrievable via GET /sessions/{id}/transcript.

Voice stack (see app/interviews/providers/factory.py — swap via .env, not code):
    STT: Deepgram Nova-3 (monolingual English)
    LLM: Gemma 4 31B, via LiveKit Inference
    TTS: Rime, Coda model

Dependencies (already in requirements.txt):
    livekit-agents[deepgram,openai,elevenlabs,silero]~=1.0
    livekit-plugins-rime
    livekit-plugins-simli

Required environment variables (this process reads them via os.environ,
loaded from .env below — it does NOT go through app.core.config.settings
since it's a separate process from the FastAPI app):
    DEEPGRAM_API_KEY   - https://console.deepgram.com  (STT)
    RIME_API_KEY       - https://rime.ai                (TTS)
    (Gemma needs no separate key — served through LiveKit Inference,
    billed to LIVEKIT_API_KEY/LIVEKIT_API_SECRET below.)

Optional — the on-screen avatar (voice-only fallback if unset):
    SIMLI_API_KEY      - https://app.simli.com/apikey
    SIMLI_FACE_ID       - https://app.simli.com/create/from-existing
"""

import asyncio
import json
import logging
import os
import sys
import uuid

from dotenv import load_dotenv
load_dotenv()

from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobExecutorType,
    WorkerOptions,
    cli,
)
from livekit.plugins import simli

from app.interviews.agents.voice_agent import VoiceAgent
from app.interviews.schemas.agent_context import AgentContext, CandidateSummary, GeneratedQuestion
from app.interviews.services import transcript_service
from app.core.redis_service import redis_service

logger = logging.getLogger(__name__)

_REQUIRED_ENV_VARS = ("DEEPGRAM_API_KEY", "RIME_API_KEY")


def _parse_job_metadata(job_metadata: str) -> dict:
    """Both dispatch paths send a small JSON object (see module
    docstring). Falls back to treating the whole string as a bare
    preparation_id for any external tooling still using that older,
    pre-JSON dispatch convention."""
    job_metadata = (job_metadata or "").strip()
    if not job_metadata:
        return {}
    try:
        data = json.loads(job_metadata)
    except json.JSONDecodeError:
        return {"preparation_id": job_metadata}
    return data if isinstance(data, dict) else {}


async def _load_pipeline_context(preparation_id: str) -> AgentContext | None:
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


async def _load_persisted_session(
    session_id: str,
) -> tuple[list[GeneratedQuestion], CandidateSummary | None] | None:
    """Fallback for when the Redis pipeline context has already expired
    (6h TTL) by the time this job is picked up: reads the durable copy of
    questions/candidate_summary persisted onto the session row itself at
    /interviews/start (see app.interviews.services.session_service.create_active_session).
    This process has no FastAPI request to hang a DB session off of, so it
    opens a short-lived one of its own."""
    from app.core.database import AsyncSessionLocal
    from app.interviews.models.session import InterviewSession
    from sqlalchemy.future import select

    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        return None

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_uuid))
            session_obj = result.scalars().first()
    except Exception as exc:
        logger.error("Failed to load session %s from database: %s", session_id, exc)
        return None

    if not session_obj:
        return None
    summary = CandidateSummary.model_validate(session_obj.candidate_summary) if session_obj.candidate_summary else None
    questions = [GeneratedQuestion.model_validate(q) for q in (session_obj.questions or [])]
    return questions, summary


async def _start_avatar(
    session: AgentSession, room, room_name: str, face_id_override: str | None = None
) -> bool:
    """Joins the Simli avatar as a second room participant, its
    video/audio synced to the interviewer's TTS output. Best-effort: a
    candidate can still have a full voice interview without a visible
    avatar, so a missing key or a Simli-side failure logs a warning and
    falls back to voice-only instead of failing the whole session.

    `face_id_override` is the candidate's chosen/custom Simli face from
    POST /interviews/prepare (see PrepareInterviewRequest.simli_face_id),
    taking priority over the worker's own SIMLI_FACE_ID env default."""
    api_key = os.environ.get("SIMLI_API_KEY")
    face_id = face_id_override or os.environ.get("SIMLI_FACE_ID")
    if not api_key or not face_id:
        logger.warning("SIMLI_API_KEY/SIMLI_FACE_ID not set — continuing voice-only, no avatar.")
        return False

    try:
        avatar = simli.AvatarSession(
            simli_config=simli.SimliConfig(api_key=api_key, face_id=face_id),
            avatar_participant_identity=f"avatar-{room_name}",
            avatar_participant_name="Aria (avatar)",
        )
        await avatar.start(session, room=room)
        return True
    except Exception as exc:
        logger.error("Simli avatar failed to join room %s — continuing voice-only: %s", room_name, exc)
        return False


async def entrypoint(ctx: JobContext) -> None:
    """
    Entry point for each interview room: connect, load the multi-agent
    pipeline context if this job was dispatched with one, start the
    VoiceAgent session (STT -> LLM -> TTS, with VAD for turn-taking), wait
    for the actual candidate to be in the room, then greet them.
    """
    room_name = ctx.room.name
    logger.info("Agent joining room: %s", room_name)

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    job_info = _parse_job_metadata(ctx.job.metadata)
    session_id = job_info.get("session_id")
    preparation_id = job_info.get("preparation_id")
    candidate_name = job_info.get("user_name")
    simli_face_id = job_info.get("simli_face_id")
    logger.info(
        "Job metadata: session_id=%s user_id=%s preparation_id=%s simli_face_id=%s",
        session_id, job_info.get("user_id"), preparation_id, simli_face_id,
    )

    questions: list[GeneratedQuestion] = []
    candidate_summary: CandidateSummary | None = None

    if preparation_id:
        pipeline_context = await _load_pipeline_context(preparation_id)
        if pipeline_context:
            questions = pipeline_context.questions
            candidate_summary = pipeline_context.candidate_summary
            session_id = session_id or pipeline_context.session_id

    if not questions and session_id:
        fallback = await _load_persisted_session(session_id)
        if fallback:
            questions, db_summary = fallback
            candidate_summary = candidate_summary or db_summary
            logger.info("Loaded questions/candidate_summary for session %s from the database.", session_id)

    voice_agent = VoiceAgent()
    session = voice_agent.build_session()

    @session.on("error")
    def _on_session_error(ev) -> None:
        detail = str(ev.error)[:500]
        logger.error("AgentSession error in room %s (source=%s): %s", room_name, ev.source, detail)
        asyncio.create_task(redis_service.set_agent_status(room_name, "degraded", detail=detail))

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev) -> None:
        # Live, crash-safe capture — the durable write happens once, in
        # full, from session.history at InterviewerAgent.on_exit(). This
        # mirrors each turn into Redis as it happens so a transcript
        # still exists if the process dies before that final flush.
        turn = transcript_service.build_turn(ev.item)
        if turn:
            asyncio.create_task(redis_service.append_transcript_turn(room_name, turn))

    # Must join before session.start(): it hooks into this exact
    # AgentSession to intercept the TTS audio output and re-publish it
    # alongside synced avatar video, rather than the session publishing
    # plain audio on its own.
    avatar_joined = await _start_avatar(session, ctx.room, room_name, face_id_override=simli_face_id)
    logger.info("Avatar joined room %s: %s", room_name, avatar_joined)

    await session.start(
        agent=voice_agent.build_agent(
            room_name,
            session_id=session_id,
            questions=questions,
            candidate_summary=candidate_summary,
            candidate_name=candidate_name,
        ),
        room=ctx.room,
    )
    participant = await ctx.wait_for_participant()
    logger.info("Candidate detected: %s", participant.identity)

    greeting_name = candidate_name or (candidate_summary.headline if candidate_summary else None)
    greeting = "Greet the candidate warmly, introduce yourself as Aria, and ask if they're ready to begin."
    if greeting_name:
        greeting = (
            f"Greet {greeting_name} warmly by name, introduce yourself as Aria, "
            "and ask if they're ready to begin."
        )
    if questions:
        greeting += (
            " Once they confirm they're ready, call the get_next_question tool "
            "to fetch the first question and ask it verbatim — do not ask a "
            "question of your own."
        )
    await session.generate_reply(instructions=greeting)


if __name__ == "__main__":
    from app.core.config import settings

    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
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
            agent_name=settings.LIVEKIT_AGENT_NAME,
            job_executor_type=JobExecutorType.PROCESS,
        )
    )
