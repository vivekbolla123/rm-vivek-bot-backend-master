import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.routers import chat, data
from app.redis_client import init_redis, close_redis
from app.exceptions import BotException
from fastapi.responses import JSONResponse
from fastapi import Request

# Make app-level loggers visible under uvicorn (uvicorn only wires up its own
# loggers by default, leaving the root logger at WARNING and hiding our
# logger.info calls).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

async def bot_exception_handler(request: Request, exc: BotException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"message": exc.message, "error_code": exc.error_code}},
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    yield
    await close_redis()

app = FastAPI(
    title="RM Bot Backend", 
    description="Backend connecting UI to RM FastAPI Agent", 
    version="1.0.0", 
    lifespan=lifespan,
    root_path="/api/rm-bot-backend"
)

app.add_exception_handler(BotException, bot_exception_handler)

# Enable CORS for the UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(data.router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}
