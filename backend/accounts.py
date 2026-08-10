from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from backend.config import Settings
from backend.models import TelegramAuthorizationState, TelegramAuthorizationStatus
from backend.telegram.base import TelegramService, TelegramServiceError
from backend.telegram.mock import MockTelegramService
from backend.telegram.tdlib import TDLibTelegramService

_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(slots=True)
class AccountSession:
    session_id: str
    service: TelegramService
    root: Path
    files_root: Path
    active: bool = False
    last_touched: float = 0.0


class TelegramAccountSessionManager:
    """Own isolated TDLib clients for phone/code-authenticated devices."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.tdlib_accounts_directory)
        self._sessions: dict[str, AccountSession] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.telegram_multi_account_enabled

    async def start(self) -> None:
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(self._stop_service(item.service) for item in sessions),
            return_exceptions=True,
        )

    async def begin_login(
        self,
        phone_number: str,
    ) -> tuple[str, TelegramAuthorizationStatus]:
        self._require_enabled()
        await self._prune_idle_sessions()
        async with self._lock:
            if len(self._sessions) >= self.settings.telegram_max_account_sessions:
                raise TelegramServiceError(
                    "account_capacity_reached",
                    "This test server is already using all available Telegram sessions.",
                    status_code=503,
                )
            session_id = uuid4().hex
            session = self._new_session(session_id)
            self._sessions[session_id] = session

        try:
            await self._start_service(session.service)
            status = await self._wait_until_interactive(session.service)
            if status.state is not TelegramAuthorizationState.WAIT_PHONE_NUMBER:
                raise TelegramServiceError(
                    "telegram_login_unavailable",
                    "Telegram did not start a fresh phone-number login.",
                    status_code=503,
                )
            status = await session.service.submit_phone_number(phone_number)
            self._write_marker(session, active=False)
            return session_id, status
        except Exception:
            async with self._lock:
                self._sessions.pop(session_id, None)
            await self._stop_service(session.service)
            raise

    async def submit_code(
        self,
        session_id: str,
        code: str,
    ) -> TelegramAuthorizationStatus:
        session = await self._pending_session(session_id)
        status = await session.service.submit_code(code)
        await self._activate_if_ready(session, status)
        return status

    async def submit_password(
        self,
        session_id: str,
        password: str,
    ) -> TelegramAuthorizationStatus:
        session = await self._pending_session(session_id)
        status = await session.service.submit_password(password)
        await self._activate_if_ready(session, status)
        return status

    async def service_for(self, session_id: str) -> AccountSession:
        self._require_enabled()
        self._validate_session_id(session_id)
        await self._prune_idle_sessions()
        async with self._lock:
            existing = self._sessions.get(session_id)
        if existing is not None:
            if not existing.active:
                raise TelegramServiceError(
                    "telegram_session_not_ready",
                    "Finish Telegram authorization before opening chats.",
                    status_code=401,
                )
            existing.last_touched = monotonic()
            return existing

        root = self.root / session_id
        marker = self._read_marker(root)
        if not marker.get("active"):
            raise TelegramServiceError(
                "telegram_session_expired",
                "This Telegram device session is no longer active.",
                status_code=401,
            )

        async with self._lock:
            if len(self._sessions) >= self.settings.telegram_max_account_sessions:
                raise TelegramServiceError(
                    "account_capacity_reached",
                    "This test server is already using all available Telegram sessions.",
                    status_code=503,
                )
            session = self._new_session(session_id)
            self._sessions[session_id] = session

        try:
            await self._start_service(session.service)
            status = await self._wait_until_interactive(session.service)
        except Exception:
            async with self._lock:
                self._sessions.pop(session_id, None)
            await self._stop_service(session.service)
            raise
        if status.state is not TelegramAuthorizationState.READY:
            async with self._lock:
                self._sessions.pop(session_id, None)
            await self._stop_service(session.service)
            raise TelegramServiceError(
                "telegram_session_expired",
                "Telegram requires this device to sign in again.",
                status_code=401,
            )
        session.active = True
        session.last_touched = monotonic()
        return session

    async def revoke(self, session_id: str) -> None:
        self._validate_session_id(session_id)
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        root = self.root / session_id
        if root.is_dir():
            marker = self._read_marker(root)
            marker["active"] = False
            marker["revoked_at"] = datetime.now(UTC).isoformat()
            self._write_marker_data(root, marker)
        if session is not None:
            await self._stop_service(session.service)

    async def _pending_session(self, session_id: str) -> AccountSession:
        self._validate_session_id(session_id)
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.active:
            raise TelegramServiceError(
                "login_flow_expired",
                "Start a new Telegram phone-number login.",
                status_code=401,
            )
        session.last_touched = monotonic()
        return session

    async def _activate_if_ready(
        self,
        session: AccountSession,
        status: TelegramAuthorizationStatus,
    ) -> None:
        if status.state is TelegramAuthorizationState.READY:
            session.active = True
            self._write_marker(session, active=True)

    def _new_session(self, session_id: str) -> AccountSession:
        self._validate_session_id(session_id)
        root = self.root / session_id
        files_root = root / "files"
        root.mkdir(parents=True, exist_ok=True)
        files_root.mkdir(parents=True, exist_ok=True)
        if self.settings.telegram_auth_mode == "mock":
            service: TelegramService = MockTelegramService(
                enabled=True,
                require_password=self.settings.telegram_mock_require_password,
            )
        elif self.settings.telegram_auth_mode == "tdlib":
            assert self.settings.telegram_api_id is not None
            service = TDLibTelegramService(
                api_id=self.settings.telegram_api_id,
                api_hash=self.settings.telegram_api_hash,
                database_directory=str(root / "database"),
                files_directory=str(files_root),
                database_encryption_key=self._encryption_key(session_id),
                library_path=self.settings.tdlib_library_path,
            )
        else:
            raise TelegramServiceError(
                "telegram_not_configured",
                "Telegram login is not configured on this server.",
                status_code=503,
            )
        return AccountSession(
            session_id=session_id,
            service=service,
            root=root,
            files_root=files_root,
            last_touched=monotonic(),
        )

    async def _prune_idle_sessions(self) -> None:
        """Stop expired login flows and idle device clients before enforcing capacity."""

        now = monotonic()
        expired: list[AccountSession] = []
        async with self._lock:
            for session_id, session in list(self._sessions.items()):
                ttl = (
                    self.settings.telegram_account_session_ttl_seconds
                    if session.active
                    else self.settings.telegram_login_flow_ttl_seconds
                )
                if now - session.last_touched >= ttl:
                    expired.append(self._sessions.pop(session_id))
        await asyncio.gather(
            *(self._stop_service(item.service) for item in expired),
            return_exceptions=True,
        )

    async def _wait_until_interactive(
        self,
        service: TelegramService,
    ) -> TelegramAuthorizationStatus:
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            status = await service.get_authorization_status()
            if status.state in {
                TelegramAuthorizationState.WAIT_PHONE_NUMBER,
                TelegramAuthorizationState.WAIT_CODE,
                TelegramAuthorizationState.WAIT_PASSWORD,
                TelegramAuthorizationState.READY,
                TelegramAuthorizationState.ERROR,
            }:
                return status
            if asyncio.get_running_loop().time() >= deadline:
                raise TelegramServiceError(
                    "telegram_start_timeout",
                    "Telegram did not become ready for authorization in time.",
                    status_code=504,
                )
            await asyncio.sleep(0.05)

    async def _start_service(self, service: TelegramService) -> None:
        start = getattr(service, "start", None)
        if start is not None:
            await start()

    async def _stop_service(self, service: TelegramService) -> None:
        stop = getattr(service, "stop", None)
        if stop is not None:
            await stop()

    def _encryption_key(self, session_id: str) -> str:
        return hmac.new(
            self.settings.tdlib_database_encryption_key.encode(),
            session_id.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _write_marker(self, session: AccountSession, *, active: bool) -> None:
        self._write_marker_data(
            session.root,
            {
                "version": 1,
                "active": active,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    @staticmethod
    def _write_marker_data(root: Path, payload: dict[str, object]) -> None:
        marker = root / "session.json"
        temporary = root / "session.json.tmp"
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(marker)

    @staticmethod
    def _read_marker(root: Path) -> dict[str, object]:
        try:
            payload = json.loads((root / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise TelegramServiceError(
                "multi_account_login_disabled",
                "Phone-number login is not enabled on this server.",
                status_code=404,
            )

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not _SESSION_ID.fullmatch(session_id):
            raise TelegramServiceError(
                "invalid_session",
                "The Telegram device session is invalid.",
                status_code=401,
            )
