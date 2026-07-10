import httpx
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Dict, Any

from app.dependencies import get_current_user
from app.config import RM_WEBSERVICE_URL

router = APIRouter(prefix="/v1/bot", tags=["data"])
logger = logging.getLogger(__name__)

async def proxy_request(method: str, path: str, request: Request, payload: dict = None, params: dict = None) -> dict:
    async with httpx.AsyncClient() as client:
        try:
            url = f"{RM_WEBSERVICE_URL}/v1/staging{path}"
            
            headers = {}
            # Pass through the rm_token if present in headers or payload
            rm_token = request.headers.get("rm-token") or (payload.get("rm_token") if payload else None)
            if rm_token:
                headers["token"] = rm_token

            response = await client.request(
                method, 
                url, 
                json=payload, 
                params=params,
                headers=headers,
                timeout=60.0
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"RM Webservice error: {e.response.text}")
            raise HTTPException(status_code=e.response.status_code, detail=f"RM Webservice error: {e.response.text}")
        except Exception as e:
            logger.error(f"Failed to communicate with RM Webservice: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to communicate with RM Webservice: {str(e)}")

@router.get("/preview")
async def get_preview(
    request: Request,
    sessionId: str,
    stageId: str,
    status: str = "changed",
    limit: int = 50,
    user_id: str = Depends(get_current_user)
):
    """Fetch preview data directly from the Java RM Webservice"""
    params = {
        "sessionId": sessionId,
        "stageId": stageId,
        "status": status,
        "limit": limit
    }
    return await proxy_request("GET", "/preview", request, params=params)

@router.get("/summary")
async def get_summary(
    request: Request,
    sessionId: str,
    stageId: str,
    user_id: str = Depends(get_current_user)
):
    """Fetch summary data directly from the Java RM Webservice"""
    params = {
        "sessionId": sessionId,
        "stageId": stageId
    }
    return await proxy_request("GET", "/summary", request, params=params)

@router.post("/submit")
async def submit_batch(
    request: Request,
    payload: Dict[str, Any],
    user_id: str = Depends(get_current_user)
):
    """Proxy submit request directly to the Java RM Webservice"""
    # payload is expected to have sessionId, stageId, batchSize
    return await proxy_request("POST", "/submitBatch", request, payload=payload)
