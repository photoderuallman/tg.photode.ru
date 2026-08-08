from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Settings
from backend.models import HealthResponse, SystemStatus
from backend.status import build_system_status
from backend.telegram.mock import MockTelegramService

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings.from_environment()
    app.state.telegram_service = MockTelegramService()
    app.state.started_at = monotonic()
    yield


app = FastAPI(
    title="Personal Telegram Gateway",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        environment=settings.environment,
        uptime_seconds=int(monotonic() - request.app.state.started_at),
    )


@app.get("/api/status", response_model=SystemStatus)
async def status(request: Request) -> SystemStatus:
    return await build_system_status(
        request.app.state.settings,
        request.app.state.telegram_service,
    )
