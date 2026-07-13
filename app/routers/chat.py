import httpx
import logging
import json
import asyncio
from fastapi import APIRouter, Request, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import websockets
from typing import Dict, Any, Optional

from app.dependencies import get_current_user
from app.redis_client import (
    get_session, update_session_stage, check_rate_limit, create_session,
    store_token, get_token,
    set_active_session, get_active_session, clear_active_session, refresh_active_session
)
from app.exceptions import BotException
from app.config import AGENT_URL

router = APIRouter(prefix="/v1/bot", tags=["bot"])
logger = logging.getLogger(__name__)

# Global HTTP client for proxying to the agent
# Using a shared client allows connection pooling (keep-alive) and significantly reduces latency.
agent_http_client = httpx.AsyncClient(timeout=300.0)


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
            payload = {
                "message": body.message,
                "session_id": session_id,
                "user_id": user_id,
                "rm_token": rm_token
            }
            async with agent_http_client.stream("POST", AGENT_URL + "/chat", json=payload) as agent_res:
                agent_res.raise_for_status()
                async for chunk in agent_res.aiter_text():
                    if chunk:
                        yield chunk
        except httpx.HTTPStatusError as e:
            logger.error(f"Agent returned error: {e.response.text}")
            yield f"event: error\ndata: {json.dumps({'detail': 'Agent returned an error.'})}\n\n"
        except asyncio.CancelledError:
            logger.info("Client disconnected, cancelling request to agent.")
            raise
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(stream_generator(), media_type="text/event-stream")

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
            await websocket.send_json({"event": "error", "data": {"detail": "Missing user_id query param."}})
            await websocket.close()
            return
    
    # 1. Enforce single active session lock
    active = await get_active_session(user_id)
    if active and active != session_id:
        await websocket.send_json({"event": "error", "data": {"detail": "This session is no longer active."}})
        await websocket.close()
        return

    session = await get_session(session_id)
    if not session:
        session = await create_session(user_id)
        
    # Convert HTTP AGENT_URL to WS
    ws_agent_url = AGENT_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/chat"
    
    try:
        async with websockets.connect(ws_agent_url) as agent_ws:
            # We need a task to read from client and forward to agent, and vice versa.
            async def forward_to_agent():
                try:
                    while True:
                        data = await websocket.receive_json()
                        
                        # Re-verify session lock!
                        active = await get_active_session(user_id)
                        if active and active != session_id:
                            await websocket.send_json({"event": "error", "data": {"detail": "This session is no longer active."}})
                            break

                        message = data.get("message")
                        rm_token = data.get("rm_token")
                        stage_id = data.get("stage_id")
                        
                        allowed = await check_rate_limit(user_id, limit=20, window_sec=60)
                        if not allowed:
                            await websocket.send_json({"event": "error", "data": {"detail": "Too many requests. Please try again later."}})
                            continue
                            
                        if stage_id:
                            await update_session_stage(session_id, stage_id)
                            
                        if rm_token:
                            await store_token(session_id, rm_token)
                        else:
                            rm_token = await get_token(session_id)
                            
                        payload = {
                            "message": message,
                            "session_id": session_id,
                            "user_id": user_id,
                            "rm_token": rm_token
                        }
                        await agent_ws.send(json.dumps(payload))
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.error(f"Error forwarding to agent: {e}")

            async def forward_to_client():
                try:
                    while True:
                        response = await agent_ws.recv()
                        await websocket.send_text(response)  # Since agent yields JSON string dicts
                except websockets.ConnectionClosed:
                    pass
                except Exception as e:
                    logger.error(f"Error forwarding to client: {e}")

            await asyncio.gather(
                forward_to_agent(),
                forward_to_client()
            )
            
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
        try:
            await websocket.send_json({"event": "error", "data": {"detail": str(e)}})
            await websocket.close()
        except:
            pass
