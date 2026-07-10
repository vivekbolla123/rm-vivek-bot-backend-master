from fastapi import Request
from fastapi.responses import JSONResponse
import uuid
import logging

logger = logging.getLogger(__name__)

class BotException(Exception):
    def __init__(self, error_code: str, message: str, status_code: int = 400, retryable: bool = False):
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.trace_id = str(uuid.uuid4())
        super().__init__(self.message)

async def bot_exception_handler(request: Request, exc: BotException):
    logger.error(f"[{exc.trace_id}] {exc.error_code}: {exc.message}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "retryable": exc.retryable,
            "trace_id": exc.trace_id
        }
    )

async def global_exception_handler(request: Request, exc: Exception):
    trace_id = str(uuid.uuid4())
    logger.error(f"[{trace_id}] Unhandled Exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred.",
            "retryable": False,
            "trace_id": trace_id
        }
    )
