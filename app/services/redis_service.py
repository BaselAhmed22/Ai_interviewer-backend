# Async Redis Service
import json
import time
import redis.asyncio as aioredis
from app.core.config import settings

class RedisService:
    def __init__(self):
        self.redis = None

    async def connect(self):
        if not self.redis:
            self.redis = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True
            )

    async def close(self):
        if self.redis:
            await self.redis.close()

    async def increment_counter(self, key: str, ttl: int = 60) -> int:
        await self.connect()
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            results = await pipe.execute()
        return results[0]

    async def reset_counter(self, key: str):
        await self.connect()
        await self.redis.delete(key)

    async def store_session_state(self, session_id: str, data: dict, ttl: int = 3600):
        await self.connect()
        await self.redis.set(f"session:{session_id}:state", json.dumps(data), ex=ttl)

    async def mark_password_reset_token_used(self, jti: str, ttl: int) -> None:
        await self.connect()
        await self.redis.set(f"used-reset-token:{jti}", "1", ex=ttl)

    async def is_password_reset_token_used(self, jti: str) -> bool:
        await self.connect()
        return bool(await self.redis.exists(f"used-reset-token:{jti}"))

    async def cache_refresh_rotation(self, old_token_hash: str, payload_json: str, ttl: int) -> None:
        # Grace-period cache for refresh-token rotation, keyed by the old
        # token's hash — see token_service.rotate_refresh_token.
        await self.connect()
        await self.redis.set(f"refresh-rotation:{old_token_hash}", payload_json, ex=ttl)

    async def get_cached_refresh_rotation(self, old_token_hash: str) -> str | None:
        await self.connect()
        return await self.redis.get(f"refresh-rotation:{old_token_hash}")

    async def set_agent_status(
        self, room_name: str, status: str, detail: str | None = None, ttl: int = 90
    ) -> None:
        # Written by the LiveKit agent process (a separate OS process, its
        # own event loop — see app/workers/livekit_agent.py) so the
        # backend has *some* visibility into whether the AI side of an
        # interview is actually working, instead of an IN_PROGRESS session
        # that silently never gets a response because the LLM/STT/TTS
        # pipeline is failing. The TTL means a crashed/killed agent process
        # (no clean on_exit) still reads as stale/unknown after ~90s
        # instead of "connected" forever.
        await self.connect()
        payload = {"status": status, "detail": detail or "", "updated_at": str(time.time())}
        await self.redis.hset(f"agent-status:{room_name}", mapping=payload)
        await self.redis.expire(f"agent-status:{room_name}", ttl)

    async def get_agent_status(self, room_name: str) -> dict | None:
        await self.connect()
        data = await self.redis.hgetall(f"agent-status:{room_name}")
        return data or None

    async def save_pipeline_context(self, preparation_id: str, context_json: str, ttl: int = 6 * 3600) -> None:
        # The working state of one multi-agent interview pipeline run
        # (see app.schemas.agent_context.AgentContext /
        # app.services.interview_pipeline.InterviewPipelineManager).
        # Deliberately ephemeral (6h TTL) — this is scratch state for one
        # interview attempt, not a durable record; the interview itself
        # lives in the InterviewSession/InterviewReport tables regardless
        # of whether this key has expired.
        await self.connect()
        await self.redis.set(f"pipeline-context:{preparation_id}", context_json, ex=ttl)

    async def get_pipeline_context(self, preparation_id: str) -> str | None:
        await self.connect()
        return await self.redis.get(f"pipeline-context:{preparation_id}")

redis_service = RedisService()