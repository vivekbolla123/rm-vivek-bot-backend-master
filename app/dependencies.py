from fastapi import Request, HTTPException
from .exceptions import BotException
from .config import MOCK_MODE

def get_current_user(request: Request) -> str:
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        if MOCK_MODE:
            return "mock-user-123"
        raise BotException("UNAUTHORIZED", "Missing X-User-Id header. Access denied.", status_code=401)
    return user_id
