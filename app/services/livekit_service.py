# LiveKit Server Integration
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

        # Grant room permissions (join room & allow sending audio/video)
        grant = api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True
        )
        token.with_grants(grant)

        return token.to_jwt()

    async def dispatch_agent(self, room_name: str) -> None:
        """
        Explicitly tell the LiveKit server to send the interviewer agent
        (app/workers/livekit_agent.py, registered under LIVEKIT_AGENT_NAME)
        into this specific room right now, instead of relying on
        server-side automatic dispatch to eventually notice the new room.
        Explicit dispatch is deterministic and immediate — important once
        multiple interviews can be starting at the same moment, where
        "eventually notices" is exactly the kind of race that leaves one
        candidate's room without an agent.
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
                )
            )

            # LiveKit *accepting* the dispatch only means it recorded the
            # request — it says nothing about whether a worker is actually
            # connected to fulfill it. Without this check, "no worker
            # running" and "worker running fine" look identical from here:
            # both return success, and the candidate only finds out
            # something's wrong once they're already in an empty room
            # ("No one else is in this room!"). A short wait gives the
            # server a moment to match the job to a connected worker, then
            # a job still showing no worker_id is a reliable, real-time
            # "no live agent worker" signal — not a guess.
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
                    "python -m app.workers.livekit_agent start",
                    room_name,
                    settings.LIVEKIT_AGENT_NAME,
                )
        finally:
            await client.aclose()

    async def close_room(self, room_name: str) -> None:
        """
        Tear down a room on the LiveKit server once its interview is over.
        Without this, ending a session on our side only updates our own
        DB row — the room (and the agent process sitting in it, still
        running STT/LLM/TTS) keeps existing on LiveKit until it times out
        on its own, which means continued cost/resource use on an
        interview that's already "finished" from the candidate's
        perspective. Deleting the room disconnects every participant,
        including the agent, immediately.
        """
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