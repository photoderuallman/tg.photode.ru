from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Awaitable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Settings
from backend.models import (
    HealthResponse,
    SystemStatus,
    TelegramAuthorizationStatus,
    TelegramCodeRequest,
    TelegramPasswordRequest,
    TelegramPhoneNumberRequest,
)
from backend.status import build_system_status
from backend.telegram.base import TelegramAuthorizationError, TelegramService
from backend.telegram.mock import MockTelegramService
from backend.telegram.tdlib import TDLibTelegramService

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings.from_environment()
    settings = app.state.settings
    if settings.telegram_auth_mode == "tdlib":
        assert settings.telegram_api_id is not None
        app.state.telegram_service = TDLibTelegramService(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            database_directory=settings.tdlib_database_directory,
            files_directory=settings.tdlib_files_directory,
            database_encryption_key=settings.tdlib_database_encryption_key,
            library_path=settings.tdlib_library_path,
        )
        await app.state.telegram_service.start()
    else:
        app.state.telegram_service = MockTelegramService(
            enabled=settings.telegram_auth_mode == "mock",
            require_password=settings.telegram_mock_require_password,
        )
    app.state.started_at = monotonic()
    try:
        yield
    finally:
        if settings.telegram_auth_mode == "tdlib":
            await app.state.telegram_service.stop()


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


@app.get(
    "/api/telegram/auth",
    response_model=TelegramAuthorizationStatus,
)
async def telegram_authorization_status(
    request: Request,
    response: Response,
) -> TelegramAuthorizationStatus:
    response.headers["Cache-Control"] = "no-store"
    service: TelegramService = request.app.state.telegram_service
    return await service.get_authorization_status()


@app.post(
    "/api/telegram/auth/phone",
    response_model=TelegramAuthorizationStatus,
)
async def telegram_authorization_phone(
    payload: TelegramPhoneNumberRequest,
    request: Request,
    response: Response,
) -> TelegramAuthorizationStatus:
    response.headers["Cache-Control"] = "no-store"
    service: TelegramService = request.app.state.telegram_service
    return await _authorization_action(
        service.submit_phone_number(payload.phone_number.get_secret_value())
    )


@app.post(
    "/api/telegram/auth/code",
    response_model=TelegramAuthorizationStatus,
)
async def telegram_authorization_code(
    payload: TelegramCodeRequest,
    request: Request,
    response: Response,
) -> TelegramAuthorizationStatus:
    response.headers["Cache-Control"] = "no-store"
    service: TelegramService = request.app.state.telegram_service
    return await _authorization_action(service.submit_code(payload.code.get_secret_value()))


@app.post(
    "/api/telegram/auth/password",
    response_model=TelegramAuthorizationStatus,
)
async def telegram_authorization_password(
    payload: TelegramPasswordRequest,
    request: Request,
    response: Response,
) -> TelegramAuthorizationStatus:
    response.headers["Cache-Control"] = "no-store"
    service: TelegramService = request.app.state.telegram_service
    return await _authorization_action(
        service.submit_password(payload.password.get_secret_value())
    )


async def _authorization_action(
    action: Awaitable[TelegramAuthorizationStatus],
) -> TelegramAuthorizationStatus:
    try:
        return await action
    except TelegramAuthorizationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": str(error)},
        ) from None
