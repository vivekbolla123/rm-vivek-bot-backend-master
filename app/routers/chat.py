import logging
import json
import asyncio
from fastapi import APIRouter, Request, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional

from app.dependencies import get_current_user
from app.redis_client import (
    get_session, update_session_stage, check_rate_limit, create_session,
    store_token, get_token,
    set_active_session, get_active_session, clear_active_session, refresh_active_session
)
from app.exceptions import BotException
from app.agentcore_client import invoke_agent

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

    async def stream_generator():
        try:
            # AgentCore invoke_agent_runtime is not itself a stream from our
            # side (non-streaming entrypoint), so we get back one full markdown
            # result and emit it as a single SSE chunk to keep the frontend's
            # existing SSE consumption contract intact.
            result = await invoke_agent(session_id, body.message, rm_token)
            yield f"data: {json.dumps({'content': result})}\n\n"
        except BotException as e:
            logger.error(f"Agent error: {e.message}")
            yield f"event: error\ndata: {json.dumps({'detail': e.message})}\n\n"
        except asyncio.CancelledError:
            logger.info("Client disconnected, cancelling request to agent.")
            raise
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(stream_generator(), media_type="text/event-stream")

async def _send_event(websocket: WebSocket, event: str, data: dict) -> None:
    """
    Emit one frame in the shape the frontend expects:
        {"event": "<name>", "data": {...}}

    Sent as text (not send_json) so the wire format is unambiguous and
    matches the raw JSON strings the old upstream FastAPI agent produced.
    """
    try:
        await websocket.send_text(json.dumps({"event": event, "data": data}))
    except Exception as e:
        logger.error(f"Failed to send WS frame event={event}: {e}")


@router.websocket("/ws/chat/{session_id}")
async def websocket_proxy_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    user_id = websocket.query_params.get("user_id")
    if not user_id:
        # Fallback for MOCK_MODE, we can import it or just assume mock if not provided, but ideally we reject.
        # Since MOCK_MODE is used in dependencies, let's just allow a mock user if testing.
        from app.config import MOCK_MODE
        if MOCK_MODE:
            user_id = "mock-user-123"
        else:
            await _send_event(websocket, "error", {"detail": "Missing user_id query param."})
            await websocket.close()
            return

    # 1. Enforce single active session lock
    active = await get_active_session(user_id)
    if active and active != session_id:
        await _send_event(websocket, "error", {"detail": "This session is no longer active."})
        await websocket.close()
        return

    session = await get_session(session_id)
    if not session:
        session = await create_session(user_id)

    # There is no upstream agent WebSocket anymore — AgentCore is invoked via
    # boto3 (SigV4-signed request/response, not a socket). Each inbound
    # message triggers one invoke_agent_runtime call; the full result is sent
    # back as a sequence of frames matching the frontend contract:
    #   running=true → text → running=false      (success)
    #   running=false → error                    (failure)
    # running=false is the frontend's "done" signal — it MUST be emitted on
    # every terminal path, otherwise isLoading stays true and the Reset /
    # Close buttons stay disabled forever.
    logger.info(f"WS connected: session={session_id} user={user_id}")
    try:
        while True:
            data = await websocket.receive_json()
            logger.info(f"WS recv: session={session_id} keys={list(data.keys())}")

            # Re-verify session lock on every turn.
            active = await get_active_session(user_id)
            if active and active != session_id:
                await _send_event(websocket, "error", {"detail": "This session is no longer active."})
                await _send_event(websocket, "running", {"status": False})
                break

            message = data.get("message")
            rm_token = data.get("rm_token")
            stage_id = data.get("stage_id")

            allowed = await check_rate_limit(user_id, limit=20, window_sec=60)
            if not allowed:
                await _send_event(websocket, "error", {"detail": "Too many requests. Please try again later."})
                await _send_event(websocket, "running", {"status": False})
                continue

            if stage_id:
                await update_session_stage(session_id, stage_id)

            if rm_token:
                await store_token(session_id, rm_token)
            else:
                rm_token = await get_token(session_id)

            # Signal turn start (marks the bot bubble as "thinking").
            await _send_event(websocket, "running", {"status": True})
            try:
                result = await invoke_agent(session_id, message, rm_token)
                logger.info(
                    f"WS agent result: session={session_id} len={len(result) if result else 0}"
                )
                # Non-streaming entrypoint: emit the full markdown as a single
                # text chunk. Frontend appends it to the current bot bubble.
                await _send_event(websocket, "text", {"text": result})
            except BotException as e:
                logger.error(f"WS agent error: session={session_id} detail={e.message}")
                await _send_event(websocket, "error", {"detail": e.message})
            finally:
                # ALWAYS release the pending UI state, success or failure.
                await _send_event(websocket, "running", {"status": False})

    except WebSocketDisconnect:
        logger.info(f"WS disconnected: session={session_id}")
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}", exc_info=True)
        try:
            await _send_event(websocket, "error", {"detail": str(e)})
            await _send_event(websocket, "running", {"status": False})
            await websocket.close()
        except Exception:
            pass
