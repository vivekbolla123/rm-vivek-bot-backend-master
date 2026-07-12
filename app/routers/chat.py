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
    store_token, get_token, redis_client,
    set_active_session, get_active_session, clear_active_session, refresh_active_session
)
from app.exceptions import BotException
from app.config import AGENT_URL

router = APIRouter(prefix="/v1/bot", tags=["bot"])
logger = logging.getLogger(__name__)

# Global HTTP client for proxying to the agent
import boto3
agentcore_client = boto3.client('bedrock-agentcore', region_name='ap-south-1')
AGENTCORE_RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-south-1:891377165721:runtime/rm_bot-aYSAibEuyu"


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
            # Load history from Redis
            history_key = f"agent_history:{session_id}"
            raw_history = await redis_client.lrange(history_key, 0, -1)
            
            strands_history = []
            for msg_str in raw_history:
                try:
                    msg = json.loads(msg_str)
                    strands_history.append(msg)
                except:
                    pass
            
            payload = {
                "input": {"text": body.message},
                "history": strands_history,
                "sessionId": session_id
            }
            # Run the synchronous boto3 call in the threadpool
            response = await asyncio.to_thread(
                agentcore_client.invoke_agent_runtime,
                agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
                payload=json.dumps(payload).encode('utf-8')
            )
            
            response_payload = response['response'].read().decode("utf-8")
            response_dict = json.loads(response_payload)
            
            # Extract the a2ui text payload returned by our custom app.py
            output_text = response_dict.get("orchestrationOutput", {}).get("text", "{}")
            
            # Save User and Agent messages to Redis
            user_msg = {"role": "user", "content": body.message}
            agent_msg = {"role": "assistant", "content": output_text}
            await redis_client.rpush(history_key, json.dumps(user_msg))
            await redis_client.rpush(history_key, json.dumps(agent_msg))
            await redis_client.expire(history_key, 86400 * 7)
            
            # Since the frontend expects an SSE stream, we just yield the final output as a single chunk
            yield output_text
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
        
    try:
        while True:
            data = await websocket.receive_json()
            message = data.get("message")
            rm_token = data.get("rm_token")
            stage_id = data.get("stage_id")
            
            logger.info(f"Received WS message: {message[:50]}...")
            
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
                
            # Load history from Redis
            history_key = f"agent_history:{session_id}"
            raw_history = await redis_client.lrange(history_key, 0, -1)
            
            strands_history = []
            for msg_str in raw_history:
                try:
                    msg = json.loads(msg_str)
                    strands_history.append(msg)
                except:
                    pass
            
            payload = {
                "input": {"text": message},
                "history": strands_history,
                "sessionId": session_id
            }
            
            # Invoke the AgentCore runtime synchronously in the threadpool
            logger.info("Calling AgentCore Runtime...")
            response = await asyncio.to_thread(
                agentcore_client.invoke_agent_runtime,
                agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
                payload=json.dumps(payload).encode('utf-8')
            )
            logger.info("Received AgentCore Response.")
            
            response_payload = response['response'].read().decode("utf-8")
            response_dict = json.loads(response_payload)
            output_text = response_dict.get("orchestrationOutput", {}).get("text", "{}")
            
            # Save User and Agent messages to Redis
            user_msg = {"role": "user", "content": message}
            agent_msg = {"role": "assistant", "content": output_text}
            await redis_client.rpush(history_key, json.dumps(user_msg))
            await redis_client.rpush(history_key, json.dumps(agent_msg))
            await redis_client.expire(history_key, 86400 * 7)
            
            # Send the JSON payload in the format expected by the frontend
            try:
                parsed_output = json.loads(output_text)
                if isinstance(parsed_output, dict):
                    # Wrap single dictionary in a list so the UI map() doesn't crash
                    parsed_output = [parsed_output]
            except json.JSONDecodeError:
                parsed_output = [{"type": "TEXT", "content": output_text}]
                
            ui_payload = {
                "event": "metadata",
                "data": {
                    "messages": parsed_output
                }
            }
            logger.info(f"Sending WS response to UI...")
            await websocket.send_json(ui_payload)
            
    except WebSocketDisconnect:
        logger.info("Client websocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket processing error: {e}")
        try:
            await websocket.send_json({"event": "error", "data": {"detail": str(e)}})
            await websocket.close()
        except:
            pass
