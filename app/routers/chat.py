import logging
import json
import asyncio
from fastapi import APIRouter, Request, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Dict, Any, Optional
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.redis_client import (
    get_session, update_session_stage, check_rate_limit, create_session,
    store_token, get_token,
    set_active_session, get_active_session, clear_active_session, refresh_active_session
)
from app.exceptions import BotException

router = APIRouter(prefix="/v1/bot", tags=["bot"])
logger = logging.getLogger(__name__)


class UIChatRequest(BaseModel):
    message: str
    session_id: str
    stage_id: Optional[str] = None
    rm_token: Optional[str] = None  # only required on first message; cached in Redis after that


class SessionStartRequest(BaseModel):
    session_id: str


class TakeoverRequest(BaseModel):
    session_id: str


# ── Session lifecycle endpoints ────────────────────────────────────────────

@router.post("/session/start")
async def session_start(body: SessionStartRequest, user_id: str = Depends(get_current_user)):
    """
    Register a new active session for the user.
    Returns 409 Conflict if another session is already active.
    """
    active = await get_active_session(user_id)
    if active and active != body.session_id:
        raise HTTPException(
            status_code=409,
            detail={"conflict": True, "active_session_id": active,
                    "message": "A session is already active for this user."}
        )
    # Create or refresh the session in Redis
    session = await get_session(body.session_id)
    if not session:
        session = await create_session(user_id)
    await set_active_session(user_id, body.session_id)
    return {"session_id": body.session_id, "status": "ok"}


@router.post("/session/takeover")
async def session_takeover(body: TakeoverRequest, user_id: str = Depends(get_current_user)):
    """
    Force-take the active session slot for this user, displacing any previous session.
    """
    await clear_active_session(user_id)
    session = await get_session(body.session_id)
    if not session:
        session = await create_session(user_id)
    await set_active_session(user_id, body.session_id)
    return {"session_id": body.session_id, "status": "ok"}


@router.post("/session/heartbeat")
async def session_heartbeat(body: SessionStartRequest, user_id: str = Depends(get_current_user)):
    """Refresh the active-session TTL. Frontend calls this every ~60 seconds."""
    active = await get_active_session(user_id)
    if active == body.session_id:
        await refresh_active_session(user_id)
        return {"status": "ok"}
    return {"status": "expired"}


@router.post("/session/end")
async def session_end(body: SessionStartRequest, user_id: str = Depends(get_current_user)):
    """Explicitly release the active session slot (called on tab close)."""
    active = await get_active_session(user_id)
    if active == body.session_id:
        await clear_active_session(user_id)
    return {"status": "ok"}


# ── Chat endpoint ──────────────────────────────────────────────────────────

@router.post("/chat/{session_id}/message")
async def chat_endpoint(session_id: str, body: UIChatRequest, request: Request, user_id: str = Depends(get_current_user)):
    # 1. Enforce single active session lock
    active = await get_active_session(user_id)
    if active and active != session_id:
        raise HTTPException(
            status_code=409,
            detail={"conflict": True, "active_session_id": active,
                    "message": "This session is no longer active."}
        )

    allowed = await check_rate_limit(user_id, limit=20, window_sec=60)
    if not allowed:
        raise BotException("RATE_LIMIT_EXCEEDED", "Too many requests. Please try again later.", status_code=429)

    session = await get_session(session_id)
    if not session:
        session = await create_session(user_id)

    if body.stage_id:
        await update_session_stage(session_id, body.stage_id)
        session["stage_id"] = body.stage_id

    # Token: cache if provided; otherwise read from Redis
    if body.rm_token:
        await store_token(session_id, body.rm_token)
        rm_token = body.rm_token
    else:
        rm_token = await get_token(session_id)

    try:
        # We now invoke the agent using the boto3 Strand wrapper!
        from app.agentcore import invoke_agent
        
        # Since AgentCore Memory expects session UUIDs to be >= 33 chars, ensure padding
        padded_session = session_id if len(session_id) >= 33 else session_id.ljust(33, 'x')
        
        agent_response = await invoke_agent(
            session_id=padded_session, 
            message=body.message, 
            rm_token=rm_token,
            actor_id=user_id
        )
        assistant_text = agent_response.get("result", "")

        # Generate SDUI metadata
        from app.a2ui_orchestrator.parser import parse_agent_response
        from app.a2ui_orchestrator.builder import build_a2ui_messages
        
        stage_id = agent_response.get("stage_id") or session.get("stage_id")
        total_records = agent_response.get("total_records")
        fields_changed = agent_response.get("fields_changed")
        parsed = parse_agent_response(assistant_text, stage_id, total_records, fields_changed)
        
        a2ui_msgs = build_a2ui_messages(parsed, session_id)
        metadata_list = []
        for msg in a2ui_msgs:
            flat_msg = {**msg, **msg.get("metadata", {})}
            metadata_list.append(flat_msg)
            
        return {
            "text": assistant_text,
            "metadata": metadata_list
        }

    except BotException as e:
        logger.error(f"Agent returned error: {str(e)}")
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except asyncio.CancelledError:
        logger.info("Client disconnected, cancelling request to agent.")
        raise
    except Exception as e:
        logger.error(f"Endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

