"""
Thin async wrapper around bedrock-agentcore's invoke_agent_runtime.

boto3 is synchronous, so every call is pushed onto a thread via
asyncio.to_thread to avoid blocking the event loop. No automatic retries
are configured — retrying a timed-out call risks double-staging records
on the agent side (per rm_bulk_action's contract).
"""

import base64
import json
import logging
import asyncio
import boto3
from botocore.config import Config

from app.exceptions import BotException
from app.config import (
    RM_BULK_ACTION_AGENT_ARN,
    AGENTCORE_REGION,
    AGENTCORE_QUALIFIER,
    AGENTCORE_READ_TIMEOUT,
    AWS_ACCESS_KEY,
    AWS_SECRET_KEY,
)

logger = logging.getLogger(__name__)

_boto_config = Config(
    read_timeout=AGENTCORE_READ_TIMEOUT,
    connect_timeout=10,
    retries={"max_attempts": 1},  # no auto-retry: avoid double-staging on timeout
)

if AWS_ACCESS_KEY and AWS_SECRET_KEY:
    _client = boto3.client(
        "bedrock-agentcore",
        region_name=AGENTCORE_REGION,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        config=_boto_config,
    )
else:
    _client = boto3.client(
        "bedrock-agentcore",
        region_name=AGENTCORE_REGION,
        config=_boto_config,
    )


def _extract_actor_id(rm_token: str) -> str:
    """
    Decode the JWT and extract the Azure AD 'oid' (Object ID) claim.
    This is a UUID unique per user, never changes, and is already safe for
    AgentCore Memory's actorId regex ([a-zA-Z0-9][a-zA-Z0-9-_/]*).
    """
    if not rm_token:
        return ""
    try:
        parts = rm_token.split(".")
        if len(parts) != 3:
            return ""
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return claims.get("oid", "")
    except Exception as e:
        logger.warning("Could not decode JWT for actor_id extraction: %s", e)
    return ""


def _read_response_body(response: dict) -> str:
    """Read the (possibly streamed) response body fully and decode as UTF-8."""
    stream = response["response"]
    if hasattr(stream, "read"):
        raw = stream.read()
    else:
        raw = b"".join(chunk for chunk in stream)
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return raw


def _invoke_sync(session_id: str, message: str, rm_token: str) -> str:
    actor_id = _extract_actor_id(rm_token)

    payload = json.dumps({
        "query": message,
        "rm_token": rm_token or "",
        "session_id": session_id,   # safe fallback for agent local testing
        "actor_id": actor_id,       # stable user identifier for AgentCore Memory
    }).encode("utf-8")

    logger.info(
        "Invoking AgentCore: session=%s actor=%s payload_bytes=%d",
        session_id, actor_id or "(none)", len(payload),
    )

    response = _client.invoke_agent_runtime(
        agentRuntimeArn=RM_BULK_ACTION_AGENT_ARN,
        runtimeSessionId=session_id,
        qualifier=AGENTCORE_QUALIFIER,
        payload=payload,
    )

    raw_body = _read_response_body(response)
    outer = json.loads(raw_body)

    status_code = outer.get("statusCode")
    body_str = outer.get("body", "{}")
    body = json.loads(body_str) if isinstance(body_str, str) else body_str

    if status_code != 200:
        error = body.get("error")
        error_type = body.get("error_type")
        logger.error(
            "AgentCore returned non-200 (statusCode=%s, error_type=%s): %s",
            status_code, error_type, error,
        )
        raise BotException(
            "AGENT_ERROR",
            "The assistant hit an error processing your request. Please try again.",
            status_code=502,
        )

    result = body.get("result")
    if result is None:
        logger.error("AgentCore response missing 'result' field: %s", body)
        raise BotException(
            "AGENT_ERROR",
            "The assistant returned an unexpected response. Please try again.",
            status_code=502,
        )
    return body


async def invoke_agent(session_id: str, message: str, rm_token: str = "", actor_id: str = "") -> dict:
    """
    Invoke the rm_bulk_action AgentCore runtime and return the agent's
    body response dictionary.

    session_id is reused as runtimeSessionId, so it must stay stable for the
    lifetime of a conversation and must be >= 33 chars (session UUIDs are 36).
    """
    try:
        return await asyncio.to_thread(_invoke_sync, session_id, message, rm_token)
    except BotException:
        raise
    except Exception as e:
        logger.error("Failed to invoke AgentCore runtime: %s", e, exc_info=True)
        raise BotException(
            "AGENT_UNAVAILABLE",
            "Could not reach the assistant. Please try again shortly.",
            status_code=502,
        )
