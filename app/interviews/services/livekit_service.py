import asyncio
import logging

from livekit import api
from app.core.config import settings

logger = logging.getLogger(__name__)


class LiveKitService:
    def generate_token(self, room_name: str, participant_identity: str, participant_name: str = None) -> str:
        token = api.AccessToken(
            settings.LIVEKIT_API_KEY,
            settings.LIVEKIT_API_SECRET
        )
        token.with_identity(participant_identity)
        if participant_name:
            token.with_name(participant_name)

        grant = api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True
        )
        token.with_grants(grant)

        return token.to_jwt()

    async def dispatch_agent(self, room_name: str, metadata: str | None = None) -> None:
        """Explicitly send the interviewer agent (app/interviews/workers/livekit_agent.py,
        registered under LIVEKIT_AGENT_NAME) into this room now, rather than
        waiting on server-side automatic dispatch — deterministic and
        immediate, which matters when multiple interviews start at once.

        `metadata` is the JSON object the agent parses in
        _parse_job_metadata (see app/interviews/workers/livekit_agent.py) to look up
        prepared questions and greet the candidate by name. Left empty,
        the agent falls back to its base persona.
        """
        client = api.LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        )
        try:
            await client.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=settings.LIVEKIT_AGENT_NAME,
                    room=room_name,
                    metadata=metadata or "",
                )
            )

            # LiveKit accepting the dispatch only means it recorded the
            # request, not that a worker is connected to fulfill it. Give
            # it a moment to match the job, then check worker_id directly.
            await asyncio.sleep(1.5)
            dispatches = await client.agent_dispatch.list_dispatch(room_name=room_name)
            picked_up = any(
                job.state.worker_id
                for d in dispatches
                if d.agent_name == settings.LIVEKIT_AGENT_NAME
                for job in d.state.jobs
            )
            if not picked_up:
                logger.error(
                    "Room '%s': dispatch for agent '%s' was accepted by LiveKit "
                    "but no worker has picked it up yet — no matching agent "
                    "worker appears to be connected. The candidate will join an "
                    "empty room. Start the worker with: "
                    "python -m app.interviews.workers.livekit_agent start",
                    room_name,
                    settings.LIVEKIT_AGENT_NAME,
                )
        finally:
            await client.aclose()

    async def close_room(self, room_name: str) -> None:
        """Deletes the room, disconnecting every participant including the
        agent — otherwise it keeps running STT/LLM/TTS until it times out
        on its own, well after our DB already marks the session finished."""
        client = api.LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        )
        try:
            await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        finally:
            await client.aclose()


livekit_service = LiveKitService()