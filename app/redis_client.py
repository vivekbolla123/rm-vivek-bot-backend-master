import os
import json
import redis.asyncio as redis
from datetime import datetime, timezone
import uuid

import urllib.parse

redis_host = os.getenv("REDIS_HOST", "172.24.72.8")
redis_port = int(os.getenv("REDIS_PORT", "6379"))
redis_password = os.getenv("REDIS_PASSWORD", "bRT__04M})0")

if redis_host:
    redis_client = redis.Redis(
        host=redis_host, 
        port=redis_port, 
        password=redis_password if redis_password else None,
        decode_responses=True
    )
else:
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

async def init_redis():
    await redis_client.ping()

async def close_redis():
    if redis_client:
        await redis_client.aclose()

async def create_session(user_id: str) -> dict:
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    session_data = {
        "session_id": session_id,
        "created_at": now.isoformat(),
        "stage_id": "",
        "user_id": user_id,
    }
    await redis_client.hset(f"session:{session_id}", mapping=session_data)
    await redis_client.expire(f"session:{session_id}", 86400) # 24 hours TTL
    return session_data

async def get_session(session_id: str) -> dict:
    data = await redis_client.hgetall(f"session:{session_id}")
    if not data:
        return None
    # refresh TTL on activity
    await redis_client.expire(f"session:{session_id}", 86400)
    return data

async def update_session_stage(session_id: str, stage_id: str):
    await redis_client.hset(f"session:{session_id}", "stage_id", stage_id)
    await redis_client.expire(f"session:{session_id}", 86400)

async def check_and_set_idempotency_key(key: str) -> bool:
    if not key:
        return True
    is_new = await redis_client.set(f"idempotency:{key}", "1", ex=86400, nx=True)
    return bool(is_new)

async def check_rate_limit(user_id: str, limit: int = 100, window_sec: int = 60) -> bool:
    key = f"rate_limit:{user_id}"
    current = await redis_client.get(key)
    if current and int(current) >= limit:
        return False
    
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_sec)
    await pipe.execute()
    return True


# ── Token caching (one per session) ────────────────────────────────────────

async def store_token(session_id: str, token: str):
    """Cache the user's JWT token in the session hash. Refreshes TTL."""
    await redis_client.hset(f"session:{session_id}", "rm_token", token)
    await redis_client.expire(f"session:{session_id}", 86400)

async def get_token(session_id: str) -> str:
    """Return the cached JWT token for this session, or empty string."""
    return await redis_client.hget(f"session:{session_id}", "rm_token") or ""


# ── Single active session per user ─────────────────────────────────────────

ACTIVE_SESSION_TTL = 90  # seconds — heartbeat must refresh within this window

async def set_active_session(username: str, session_id: str):
    """Register session_id as the single active session for username."""
    await redis_client.set(f"user_active_session:{username}", session_id, ex=ACTIVE_SESSION_TTL)

async def get_active_session(username: str) -> str:
    """Return the active session_id for username, or None."""
    return await redis_client.get(f"user_active_session:{username}")

async def clear_active_session(username: str):
    """Remove the active session lock for username."""
    await redis_client.delete(f"user_active_session:{username}")

async def refresh_active_session(username: str):
    """Refresh the TTL on the active session key (heartbeat)."""
    await redis_client.expire(f"user_active_session:{username}", ACTIVE_SESSION_TTL)
