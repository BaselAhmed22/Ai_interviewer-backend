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
        # Written by the separate LiveKit agent process (livekit_agent.py).
        # TTL means a crashed agent (no clean on_exit) reads as stale after
        # ~90s instead of "connected" forever.
        await self.connect()
        payload = {"status": status, "detail": detail or "", "updated_at": str(time.time())}
        await self.redis.hset(f"agent-status:{room_name}", mapping=payload)
        await self.redis.expire(f"agent-status:{room_name}", ttl)

    async def get_agent_status(self, room_name: str) -> dict | None:
        await self.connect()
        data = await self.redis.hgetall(f"agent-status:{room_name}")
        return data or None

    async def save_pipeline_context(self, preparation_id: str, context_json: str, ttl: int = 6 * 3600) -> None:
        # Ephemeral working state for one interview pipeline run (see
        # app.interviews.schemas.agent_context.AgentContext) — the durable record
        # lives in InterviewSession/InterviewReport regardless of this TTL.
        await self.connect()
        await self.redis.set(f"pipeline-context:{preparation_id}", context_json, ex=ttl)

    async def get_pipeline_context(self, preparation_id: str) -> str | None:
        await self.connect()
        return await self.redis.get(f"pipeline-context:{preparation_id}")

    async def append_transcript_turn(self, room_name: str, turn: dict, ttl: int = 6 * 3600) -> None:
        # Live, crash-safe snapshot of the conversation as it happens,
        # written by the LiveKit agent process on every completed
        # candidate/agent turn. The durable copy lands in Postgres
        # (InterviewTranscript) when InterviewerAgent.on_exit() fires; if
        # the process dies before that, this is the recovery source.
        await self.connect()
        key = f"transcript:{room_name}"
        await self.redis.rpush(key, json.dumps(turn))
        await self.redis.expire(key, ttl)

    async def get_transcript_turns(self, room_name: str) -> list[dict]:
        await self.connect()
        raw_items = await self.redis.lrange(f"transcript:{room_name}", 0, -1)
        return [json.loads(item) for item in raw_items]

redis_service = RedisService()