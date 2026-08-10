from __future__ import annotations

import asyncio
import hmac
import json
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from time import monotonic
from typing import Annotated, Awaitable, Literal, TypeVar

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.access import (
    create_scoped_token,
    create_session_token,
    read_scoped_token,
    validate_session_token,
)
from backend.accounts import TelegramAccountSessionManager
from backend.config import Settings, web_allowed_origins_from_environment
from backend.media import MediaPreparationError, prepare_media_upload
from backend.models import (
    HealthResponse,
    SystemStatus,
    TelegramAccountProfile,
    TelegramAuthorizationStatus,
    TelegramChatActionRequest,
    TelegramChatActionState,
    TelegramChatSummary,
    TelegramCodeRequest,
    TelegramCustomEmoji,
    TelegramEvent,
    TelegramLoginCodeRequest,
    TelegramLoginPasswordRequest,
    TelegramLoginResponse,
    TelegramMessage,
    TelegramPasswordRequest,
    TelegramPhoneNumberRequest,
    TelegramReadRequest,
    TelegramReadResult,
    TelegramTextMessage,
    TelegramTextMessageRequest,
    TelegramUserProfile,
    WebSessionRequest,
    WebSessionResponse,
)
from backend.status import build_system_status
from backend.telegram.base import TelegramService, TelegramServiceError
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
    app.state.account_session_manager = TelegramAccountSessionManager(settings)
    await app.state.account_session_manager.start()
    app.state.started_at = monotonic()
    try:
        yield
    finally:
        await app.state.account_session_manager.stop()
        if settings.telegram_auth_mode == "tdlib":
            await app.state.telegram_service.stop()


app = FastAPI(
    title="Personal Telegram Gateway",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.middleware("http")
async def require_browser_session(request: Request, call_next):
    settings: Settings | None = getattr(request.app.state, "settings", None)
    public_api_paths = {
        "/api/health",
        "/api/session",
        "/api/auth/phone",
        "/api/auth/code",
        "/api/auth/password",
    }
    if (
        settings is None
        or request.method == "OPTIONS"
        or request.url.path in public_api_paths
        or not request.url.path.startswith("/api/")
    ):
        return await call_next(request)

    token = _bearer_token(request.headers.get("authorization", ""))
    if (
        token
        and settings.ios_device_access_token
        and hmac.compare_digest(token, settings.ios_device_access_token)
    ):
        request.state.telegram_service = request.app.state.telegram_service
        request.state.telegram_files_directory = Path(settings.tdlib_files_directory)
        request.state.ios_device_session = True
        return await call_next(request)

    if token and settings.telegram_multi_account_enabled:
        account_claims = read_scoped_token(
            token,
            settings.telegram_account_token_secret,
            expected_scope="telegram_session",
        )
        if account_claims is not None:
            manager: TelegramAccountSessionManager = (
                request.app.state.account_session_manager
            )
            try:
                account_session = await manager.service_for(account_claims.subject)
            except TelegramServiceError as error:
                return _api_error_response(error)
            request.state.telegram_account_session_id = account_session.session_id
            request.state.telegram_service = account_session.service
            request.state.telegram_files_directory = account_session.files_root
            return await call_next(request)

    if not settings.web_auth_required:
        request.state.telegram_service = request.app.state.telegram_service
        request.state.telegram_files_directory = Path(settings.tdlib_files_directory)
        return await call_next(request)

    if not token or not validate_session_token(token, settings.web_session_secret):
        return JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "code": "web_session_required",
                    "message": "Open the private client link again to unlock this device.",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    request.state.telegram_service = request.app.state.telegram_service
    request.state.telegram_files_directory = Path(settings.tdlib_files_directory)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(web_allowed_origins_from_environment()),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Content-Length", "Content-Type"],
    max_age=600,
)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "main.html")


@app.post("/api/session", response_model=WebSessionResponse)
async def create_web_session(
    payload: WebSessionRequest,
    request: Request,
    response: Response,
) -> WebSessionResponse:
    settings: Settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    if not settings.web_auth_required:
        raise HTTPException(
            status_code=404,
            detail={"code": "web_auth_disabled", "message": "Web access is not configured."},
        )
    supplied_key = payload.access_key.get_secret_value()
    if not hmac.compare_digest(supplied_key, settings.web_access_key):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_access_key", "message": "This private access link is invalid."},
        )
    token, expires_at = create_session_token(
        settings.web_session_secret,
        settings.web_session_ttl_seconds,
    )
    return WebSessionResponse(token=token, expires_at=expires_at)


@app.post("/api/auth/phone", response_model=TelegramLoginResponse)
async def telegram_login_phone(
    payload: TelegramPhoneNumberRequest,
    request: Request,
    response: Response,
) -> TelegramLoginResponse:
    """Start an existing-account-only Telegram login in an isolated TDLib client."""

    response.headers["Cache-Control"] = "no-store"
    manager: TelegramAccountSessionManager = request.app.state.account_session_manager
    try:
        session_id, status = await manager.begin_login(
            payload.phone_number.get_secret_value()
        )
    except TelegramServiceError as error:
        raise _http_error(error) from None
    return await _telegram_login_response(request, session_id, status)


@app.post("/api/auth/code", response_model=TelegramLoginResponse)
async def telegram_login_code(
    payload: TelegramLoginCodeRequest,
    request: Request,
    response: Response,
) -> TelegramLoginResponse:
    response.headers["Cache-Control"] = "no-store"
    session_id = _login_flow_subject(
        payload.flow_token.get_secret_value(),
        request.app.state.settings,
    )
    manager: TelegramAccountSessionManager = request.app.state.account_session_manager
    try:
        status = await manager.submit_code(
            session_id,
            payload.code.get_secret_value(),
        )
    except TelegramServiceError as error:
        raise _http_error(error) from None
    return await _telegram_login_response(request, session_id, status)


@app.post("/api/auth/password", response_model=TelegramLoginResponse)
async def telegram_login_password(
    payload: TelegramLoginPasswordRequest,
    request: Request,
    response: Response,
) -> TelegramLoginResponse:
    response.headers["Cache-Control"] = "no-store"
    session_id = _login_flow_subject(
        payload.flow_token.get_secret_value(),
        request.app.state.settings,
    )
    manager: TelegramAccountSessionManager = request.app.state.account_session_manager
    try:
        status = await manager.submit_password(
            session_id,
            payload.password.get_secret_value(),
        )
    except TelegramServiceError as error:
        raise _http_error(error) from None
    return await _telegram_login_response(request, session_id, status)


@app.post("/api/auth/logout", status_code=204)
async def telegram_logout(request: Request) -> Response:
    session_id = getattr(request.state, "telegram_account_session_id", "")
    if not session_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "account_session_required",
                "message": "This client is not using a device Telegram session.",
            },
        )
    manager: TelegramAccountSessionManager = request.app.state.account_session_manager
    await manager.revoke(session_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


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
        _telegram_service(request),
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
    service = _telegram_service(request)
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
    service = _telegram_service(request)
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
    service = _telegram_service(request)
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
    service = _telegram_service(request)
    return await _authorization_action(
        service.submit_password(payload.password.get_secret_value())
    )


@app.get("/api/telegram/me", response_model=TelegramAccountProfile)
async def telegram_account(request: Request) -> TelegramAccountProfile:
    service = _telegram_service(request)
    return await _telegram_action(service.get_me())


@app.get("/api/chats", response_model=list[TelegramChatSummary])
async def telegram_chats(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[TelegramChatSummary]:
    service = _telegram_service(request)
    return await _telegram_action(service.get_chats(limit=limit))


@app.get(
    "/api/chats/{chat_id}/messages",
    response_model=list[TelegramTextMessage],
)
async def telegram_messages(
    chat_id: int,
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    before_message_id: int | None = Query(default=None, gt=0),
) -> list[TelegramTextMessage]:
    service = _telegram_service(request)
    return await _telegram_action(
        service.get_messages(
            chat_id,
            limit=limit,
            before_message_id=before_message_id,
        )
    )


@app.post(
    "/api/chats/{chat_id}/messages",
    response_model=TelegramTextMessage,
    status_code=201,
)
async def telegram_send_message(
    chat_id: int,
    payload: TelegramTextMessageRequest,
    request: Request,
) -> TelegramTextMessage:
    service = _telegram_service(request)
    return await _telegram_action(
        service.send_text_message(chat_id, payload.text, payload.entities)
    )


@app.get("/api/users/{user_id}", response_model=TelegramUserProfile)
async def telegram_user(user_id: int, request: Request) -> TelegramUserProfile:
    service = _telegram_service(request)
    return await _telegram_action(service.get_user(user_id))


@app.post("/api/chats/{chat_id}/read", response_model=TelegramReadResult)
async def telegram_mark_read(
    chat_id: int,
    payload: TelegramReadRequest,
    request: Request,
) -> TelegramReadResult:
    service = _telegram_service(request)
    return await _telegram_action(
        service.mark_messages_read(chat_id, payload.message_ids)
    )


@app.post(
    "/api/chats/{chat_id}/messages/{message_id}/open",
    status_code=204,
)
async def telegram_open_message_content(
    chat_id: int,
    message_id: int,
    request: Request,
) -> Response:
    service = _telegram_service(request)
    await _telegram_action(service.open_message_content(chat_id, message_id))
    return Response(status_code=204)


@app.post(
    "/api/chats/{chat_id}/actions",
    response_model=TelegramChatActionState,
)
async def telegram_chat_action(
    chat_id: int,
    payload: TelegramChatActionRequest,
    request: Request,
) -> TelegramChatActionState:
    service = _telegram_service(request)
    return await _telegram_action(
        service.send_chat_action(
            chat_id,
            payload.action,
            progress=payload.progress,
        )
    )


@app.post(
    "/api/chats/{chat_id}/media",
    response_model=TelegramMessage,
    status_code=201,
)
async def telegram_send_media(
    chat_id: int,
    request: Request,
    file: Annotated[UploadFile, File()],
    kind: Annotated[
        Literal["photo", "video", "voice_note", "video_note"],
        Form(),
    ],
    caption: Annotated[str, Form(max_length=1024)] = "",
    duration: Annotated[int, Form(ge=0, le=86400)] = 0,
    width: Annotated[int, Form(ge=0, le=8192)] = 0,
    height: Annotated[int, Form(ge=0, le=8192)] = 0,
) -> TelegramMessage:
    settings: Settings = request.app.state.settings
    upload_root = _telegram_files_directory(request) / "uploads"
    try:
        prepared = await prepare_media_upload(
            file,
            kind=kind,
            root=upload_root,
            maximum_bytes=settings.media_upload_max_bytes,
            ffmpeg_path=settings.media_ffmpeg_path,
            width=width,
            height=height,
        )
    except MediaPreparationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": str(error)},
        ) from None

    service = _telegram_service(request)
    try:
        return await _telegram_action(
            service.send_media_message(
                chat_id,
                kind=kind,
                path=prepared.path,
                caption=caption,
                duration=duration,
                width=prepared.width,
                height=prepared.height,
            )
        )
    except Exception:
        prepared.path.unlink(missing_ok=True)
        raise


@app.get("/api/files/{file_id}", response_class=FileResponse)
async def telegram_file(file_id: int, request: Request) -> FileResponse:
    service = _telegram_service(request)
    path = await _telegram_action(service.download_file(file_id))
    allowed_root = _telegram_files_directory(request).resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(allowed_root) or not resolved_path.is_file():
        raise HTTPException(
            status_code=502,
            detail={
                "code": "unsafe_telegram_file_path",
                "message": "Telegram returned an invalid local file path.",
            },
        )
    return FileResponse(
        resolved_path,
        media_type=mimetypes.guess_type(resolved_path.name)[0],
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get(
    "/api/emojis/custom/{custom_emoji_id}",
    response_model=TelegramCustomEmoji,
)
async def telegram_custom_emoji(
    custom_emoji_id: int,
    request: Request,
) -> TelegramCustomEmoji:
    service = _telegram_service(request)
    return await _telegram_action(service.get_custom_emoji(custom_emoji_id))


@app.get("/api/events", response_class=StreamingResponse)
async def telegram_events(
    request: Request,
    chat_id: int | None = Query(default=None),
) -> StreamingResponse:
    service = _telegram_service(request)
    authorization = await service.get_authorization_status()
    if authorization.state.value != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "telegram_not_ready",
                "message": "Telegram must be authorized before receiving events.",
            },
        )
    if chat_id is not None:
        await _telegram_action(service.open_chat(chat_id))

    async def stream() -> AsyncIterator[str]:
        events = service.event_stream()
        pending_event = asyncio.create_task(anext(events))
        try:
            while True:
                if await request.is_disconnected():
                    break
                completed, _ = await asyncio.wait({pending_event}, timeout=10)
                if not completed:
                    yield ": keepalive\n\n"
                    continue
                try:
                    event = pending_event.result()
                except StopAsyncIteration:
                    break
                pending_event = asyncio.create_task(anext(events))
                if chat_id is not None and event.chat_id != chat_id:
                    continue
                payload = json.dumps(
                    event.model_dump(mode="json", exclude_none=True),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"data: {payload}\n\n"
        except TelegramServiceError:
            return
        finally:
            if not pending_event.done():
                pending_event.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_event
            await events.aclose()
            if chat_id is not None:
                with suppress(TelegramServiceError):
                    await service.close_chat(chat_id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.get(
    "/api/events/next",
    response_model=TelegramEvent,
    responses={204: {"description": "No matching event arrived before the timeout."}},
)
async def telegram_next_event(
    request: Request,
    chat_id: int | None = Query(default=None),
    timeout_seconds: int = Query(default=20, ge=1, le=25),
) -> TelegramEvent | Response:
    """Return one Telegram event using an ordinary HTTPS long-poll request."""

    service = _telegram_service(request)
    authorization = await service.get_authorization_status()
    if authorization.state.value != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "telegram_not_ready",
                "message": "Telegram must be authorized before receiving events.",
            },
        )
    if chat_id is not None:
        await _telegram_action(service.open_chat(chat_id))

    events = service.event_stream()
    deadline = monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            try:
                event = await asyncio.wait_for(anext(events), timeout=remaining)
            except (TimeoutError, StopAsyncIteration):
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            if chat_id is None or event.chat_id == chat_id:
                return event
    except TelegramServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": str(error)},
        ) from None
    finally:
        await events.aclose()
        if chat_id is not None:
            with suppress(TelegramServiceError):
                await service.close_chat(chat_id)


@app.websocket("/api/events")
async def telegram_websocket_events(
    websocket: WebSocket,
    chat_id: int | None = Query(default=None),
) -> None:
    settings: Settings = websocket.app.state.settings
    origin = (websocket.headers.get("origin") or "").rstrip("/")
    if settings.web_auth_required and origin not in settings.web_allowed_origins:
        await websocket.close(code=4403, reason="This web origin is not allowed.")
        return

    protocols = [
        item.strip()
        for item in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if item.strip()
    ]
    session_token = protocols[1] if len(protocols) > 1 and protocols[0] == "tg-session" else ""
    service: TelegramService | None = None
    if settings.telegram_multi_account_enabled:
        account_claims = read_scoped_token(
            session_token,
            settings.telegram_account_token_secret,
            expected_scope="telegram_session",
        )
        if account_claims is not None:
            manager: TelegramAccountSessionManager = (
                websocket.app.state.account_session_manager
            )
            try:
                account_session = await manager.service_for(account_claims.subject)
            except TelegramServiceError:
                await websocket.close(code=4401, reason="Telegram session expired.")
                return
            service = account_session.service

    if (
        service is None
        and settings.web_auth_required
        and not validate_session_token(session_token, settings.web_session_secret)
    ):
        await websocket.close(code=4401, reason="A private web session is required.")
        return

    await websocket.accept(subprotocol="tg-session" if protocols else None)
    service = service or websocket.app.state.telegram_service
    authorization = await service.get_authorization_status()
    if authorization.state.value != "ready":
        await websocket.close(code=1013, reason="Telegram is not authorized.")
        return
    if chat_id is not None:
        try:
            await service.open_chat(chat_id)
        except TelegramServiceError:
            await websocket.close(code=1008, reason="Telegram chat could not be opened.")
            return

    try:
        async for event in service.event_stream():
            if chat_id is not None and event.chat_id != chat_id:
                continue
            await websocket.send_json(
                event.model_dump(mode="json", exclude_none=True)
            )
    except WebSocketDisconnect:
        return
    except TelegramServiceError:
        await websocket.close(code=1011, reason="Telegram event stream stopped.")
    finally:
        if chat_id is not None:
            with suppress(TelegramServiceError):
                await service.close_chat(chat_id)


async def _telegram_login_response(
    request: Request,
    session_id: str,
    status: TelegramAuthorizationStatus,
) -> TelegramLoginResponse:
    settings: Settings = request.app.state.settings
    flow_token: str | None = None
    token: str | None = None
    expires_at = None
    account = None

    if status.state.value in {"wait_code", "wait_password"}:
        flow_token, _ = create_scoped_token(
            settings.telegram_account_token_secret,
            subject=session_id,
            scope="telegram_login",
            ttl_seconds=settings.telegram_login_flow_ttl_seconds,
        )
    elif status.state.value == "ready":
        manager: TelegramAccountSessionManager = request.app.state.account_session_manager
        account_session = await manager.service_for(session_id)
        account = await _telegram_action(account_session.service.get_me())
        token, expires_at = create_scoped_token(
            settings.telegram_account_token_secret,
            subject=session_id,
            scope="telegram_session",
            ttl_seconds=settings.telegram_account_session_ttl_seconds,
        )

    return TelegramLoginResponse(
        state=status.state,
        detail=status.detail,
        next_action=status.next_action,
        password_hint=status.password_hint,
        flow_token=flow_token,
        token=token,
        expires_at=expires_at,
        account=account,
    )


def _login_flow_subject(token: str, settings: Settings) -> str:
    claims = read_scoped_token(
        token,
        settings.telegram_account_token_secret,
        expected_scope="telegram_login",
    )
    if claims is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "login_flow_expired",
                "message": "Start a new Telegram phone-number login.",
            },
        )
    return claims.subject


def _telegram_service(request: Request) -> TelegramService:
    return getattr(
        request.state,
        "telegram_service",
        request.app.state.telegram_service,
    )


def _telegram_files_directory(request: Request) -> Path:
    return Path(
        getattr(
            request.state,
            "telegram_files_directory",
            request.app.state.settings.tdlib_files_directory,
        )
    )


def _http_error(error: TelegramServiceError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
    )


def _api_error_response(error: TelegramServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": {"code": error.code, "message": str(error)}},
        headers={"Cache-Control": "no-store"},
    )


async def _authorization_action(
    action: Awaitable[TelegramAuthorizationStatus],
) -> TelegramAuthorizationStatus:
    return await _telegram_action(action)


ResultT = TypeVar("ResultT")


async def _telegram_action(action: Awaitable[ResultT]) -> ResultT:
    try:
        return await action
    except TelegramServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": str(error)},
        ) from None


def _bearer_token(header: str) -> str:
    scheme, separator, token = header.partition(" ")
    if separator and scheme.lower() == "bearer":
        return token.strip()
    return ""
