from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from ctypes import CDLL, c_char_p, c_double, c_int
from ctypes.util import find_library
from itertools import count
from pathlib import Path
from typing import Any, Protocol

from backend.models import TelegramAuthorizationState, TelegramAuthorizationStatus
from backend.telegram.base import TelegramAuthorizationError

logger = logging.getLogger(__name__)


class TDLibLoadError(RuntimeError):
    """Raised when the native TDLib JSON library can't be loaded."""


class _TDJsonClient(Protocol):
    def create_client(self) -> int: ...

    def send(self, client_id: int, query: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def execute(self, query: dict[str, Any]) -> dict[str, Any] | None: ...


class TDJsonClient:
    """Small ctypes wrapper around TDLib's supported JSON C interface."""

    def __init__(self, library_path: str = "") -> None:
        resolved_path = library_path or find_library("tdjson")
        if not resolved_path:
            raise TDLibLoadError(
                "TDLib's libtdjson shared library is not installed or discoverable."
            )

        try:
            self._library = CDLL(resolved_path)
        except OSError as error:
            raise TDLibLoadError("TDLib's libtdjson shared library could not be loaded.") from error

        self._create_client_id = self._library.td_create_client_id
        self._create_client_id.restype = c_int
        self._create_client_id.argtypes = []

        self._send = self._library.td_send
        self._send.restype = None
        self._send.argtypes = [c_int, c_char_p]

        self._receive = self._library.td_receive
        self._receive.restype = c_char_p
        self._receive.argtypes = [c_double]

        self._execute = self._library.td_execute
        self._execute.restype = c_char_p
        self._execute.argtypes = [c_char_p]

    def create_client(self) -> int:
        return self._create_client_id()

    def send(self, client_id: int, query: dict[str, Any]) -> None:
        payload = json.dumps(query, ensure_ascii=False, separators=(",", ":")).encode()
        self._send(client_id, payload)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        payload = self._receive(timeout)
        if not payload:
            return None
        return json.loads(payload.decode())

    def execute(self, query: dict[str, Any]) -> dict[str, Any] | None:
        payload = json.dumps(query, ensure_ascii=False, separators=(",", ":")).encode()
        result = self._execute(payload)
        if not result:
            return None
        return json.loads(result.decode())


class TDLibTelegramService:
    """Real Telegram authorization adapter backed by TDLib's JSON interface."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        database_directory: str,
        files_directory: str,
        database_encryption_key: str,
        library_path: str = "",
        native_client: _TDJsonClient | None = None,
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._database_directory = database_directory
        self._files_directory = files_directory
        self._database_encryption_key = database_encryption_key
        self._library_path = library_path
        self._native = native_client

        self._client_id: int | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._request_ids = count(1)
        self._submission_lock = asyncio.Lock()
        self._state_changed = asyncio.Event()
        self._closed = asyncio.Event()
        self._closing = False
        self._stopping = False

        self._state = TelegramAuthorizationState.NOT_CONFIGURED
        self._detail = "TDLib is configured and waiting to start."
        self._next_action: str | None = "Wait for the Telegram connection to initialize."
        self._password_hint: str | None = None

    async def start(self) -> None:
        Path(self._database_directory).mkdir(parents=True, exist_ok=True)
        Path(self._files_directory).mkdir(parents=True, exist_ok=True)

        if self._native is None:
            self._native = TDJsonClient(self._library_path)

        self._native.execute(
            {"@type": "setLogVerbosityLevel", "new_verbosity_level": 1}
        )
        self._client_id = self._native.create_client()
        self._detail = "TDLib is starting and loading its encrypted local session."
        self._receive_task = asyncio.create_task(
            self._receive_loop(),
            name="tdlib-receive-loop",
        )
        self._send({"@type": "getOption", "name": "version"})

    async def stop(self) -> None:
        if self._receive_task is None:
            return

        self._closing = True
        with suppress(RuntimeError):
            self._send({"@type": "close"})
            with suppress(TimeoutError):
                await asyncio.wait_for(self._closed.wait(), timeout=8)

        self._stopping = True
        self._receive_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._receive_task
        self._receive_task = None

        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def get_authorization_status(self) -> TelegramAuthorizationStatus:
        return self._status()

    async def submit_phone_number(
        self,
        phone_number: str,
    ) -> TelegramAuthorizationStatus:
        normalized = phone_number.strip()
        if (
            not normalized.startswith("+")
            or not normalized[1:].isdigit()
            or not 8 <= len(normalized[1:]) <= 15
        ):
            raise TelegramAuthorizationError(
                "invalid_phone_number",
                "Use an international phone number such as +12223334455.",
                status_code=400,
            )

        async with self._submission_lock:
            self._require_state(TelegramAuthorizationState.WAIT_PHONE_NUMBER)
            await self._request(
                {
                    "@type": "setAuthenticationPhoneNumber",
                    "phone_number": normalized,
                },
                action="phone number",
            )
            return await self._wait_for_transition(
                TelegramAuthorizationState.WAIT_PHONE_NUMBER
            )

    async def submit_code(self, code: str) -> TelegramAuthorizationStatus:
        self._validate_secret(code, "authorization code", maximum_length=64)
        async with self._submission_lock:
            self._require_state(TelegramAuthorizationState.WAIT_CODE)
            await self._request(
                {"@type": "checkAuthenticationCode", "code": code},
                action="authorization code",
            )
            return await self._wait_for_transition(TelegramAuthorizationState.WAIT_CODE)

    async def submit_password(self, password: str) -> TelegramAuthorizationStatus:
        self._validate_secret(password, "password", maximum_length=256)
        async with self._submission_lock:
            self._require_state(TelegramAuthorizationState.WAIT_PASSWORD)
            await self._request(
                {"@type": "checkAuthenticationPassword", "password": password},
                action="two-step verification password",
            )
            return await self._wait_for_transition(
                TelegramAuthorizationState.WAIT_PASSWORD
            )

    async def _receive_loop(self) -> None:
        assert self._native is not None
        try:
            while not self._stopping:
                event = await asyncio.to_thread(self._native.receive, 0.5)
                if event is not None:
                    self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TDLib receive loop stopped")
            self._set_error(
                "The TDLib receiver stopped unexpectedly.",
                "Restart the service and inspect the protected server log.",
            )

    def _handle_event(self, event: dict[str, Any]) -> None:
        request_id = event.get("@extra")
        if request_id is not None:
            future = self._pending.pop(str(request_id), None)
            if future is not None and not future.done():
                future.set_result(event)
                return

        event_type = event.get("@type")
        if event_type == "updateAuthorizationState":
            self._apply_authorization_state(event.get("authorization_state", {}))
        elif event_type == "error":
            self._set_error(
                "TDLib rejected an initialization request.",
                "Check the protected TDLib configuration and restart the service.",
            )

    def _apply_authorization_state(self, authorization_state: dict[str, Any]) -> None:
        state_type = authorization_state.get("@type")

        if state_type == "authorizationStateWaitTdlibParameters":
            self._set_status(
                TelegramAuthorizationState.NOT_CONFIGURED,
                "TDLib loaded and is applying the private API credentials.",
                "Wait for TDLib to request the account phone number.",
            )
            self._send(
                {
                    "@type": "setTdlibParameters",
                    "use_test_dc": False,
                    "database_directory": self._database_directory,
                    "files_directory": self._files_directory,
                    "database_encryption_key": self._database_encryption_key,
                    "use_file_database": True,
                    "use_chat_info_database": True,
                    "use_message_database": True,
                    "use_secret_chats": True,
                    "api_id": self._api_id,
                    "api_hash": self._api_hash,
                    "system_language_code": "en",
                    "device_model": "tg.photode.ru private VPS",
                    "system_version": "Debian 13",
                    "application_version": "0.1.0",
                }
            )
            return

        if state_type == "authorizationStateWaitDatabaseEncryptionKey":
            self._set_status(
                TelegramAuthorizationState.NOT_CONFIGURED,
                "TDLib is unlocking the encrypted local session database.",
                "Wait for the Telegram authorization state to load.",
            )
            self._send(
                {
                    "@type": "checkDatabaseEncryptionKey",
                    "encryption_key": self._database_encryption_key,
                }
            )
            return

        if state_type == "authorizationStateWaitPhoneNumber":
            self._set_status(
                TelegramAuthorizationState.WAIT_PHONE_NUMBER,
                "TDLib is connected and waiting for your Telegram phone number.",
                "Enter your phone number in international format.",
            )
            return

        if state_type == "authorizationStateWaitCode":
            self._set_status(
                TelegramAuthorizationState.WAIT_CODE,
                "Telegram accepted the phone number and requested a login code.",
                "Enter the code delivered by Telegram.",
            )
            return

        if state_type == "authorizationStateWaitPassword":
            self._set_status(
                TelegramAuthorizationState.WAIT_PASSWORD,
                "Telegram requires your two-step verification password.",
                "Enter the password to finish authorization.",
                password_hint=authorization_state.get("password_hint") or None,
            )
            return

        if state_type == "authorizationStateReady":
            self._set_status(
                TelegramAuthorizationState.READY,
                "TDLib is authorized and the encrypted Telegram session is active.",
                "The next phase can load your real chat list and messages.",
            )
            return

        if state_type in {
            "authorizationStateLoggingOut",
            "authorizationStateClosing",
        }:
            self._set_status(
                TelegramAuthorizationState.NOT_CONFIGURED,
                "The Telegram session is closing.",
                "Wait for the service to stop cleanly.",
            )
            return

        if state_type == "authorizationStateClosed":
            self._closed.set()
            if not self._closing:
                self._set_error(
                    "The Telegram session closed unexpectedly.",
                    "Restart the service before retrying authorization.",
                )
            return

        self._set_error(
            "Telegram requested an authorization step this first interface doesn't support.",
            "Inspect the protected server status before continuing.",
        )

    async def _request(
        self,
        query: dict[str, Any],
        *,
        action: str,
    ) -> dict[str, Any]:
        request_id = str(next(self._request_ids))
        payload = {**query, "@extra": request_id}
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._send(payload)

        try:
            response = await asyncio.wait_for(future, timeout=45)
        except TimeoutError as error:
            self._pending.pop(request_id, None)
            raise TelegramAuthorizationError(
                "telegram_timeout",
                f"Telegram did not finish the {action} step in time.",
                status_code=504,
            ) from error

        if response.get("@type") == "error":
            raise self._authorization_error(response, action)
        return response

    async def _wait_for_transition(
        self,
        previous_state: TelegramAuthorizationState,
    ) -> TelegramAuthorizationStatus:
        deadline = asyncio.get_running_loop().time() + 15
        while self._state is previous_state:
            self._state_changed.clear()
            if self._state is not previous_state:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TelegramAuthorizationError(
                    "authorization_state_timeout",
                    "Telegram accepted the request, but the next authorization state did not arrive.",
                    status_code=504,
                )
            try:
                await asyncio.wait_for(self._state_changed.wait(), timeout=remaining)
            except TimeoutError as error:
                raise TelegramAuthorizationError(
                    "authorization_state_timeout",
                    "Telegram accepted the request, but the next authorization state did not arrive.",
                    status_code=504,
                ) from error
        return self._status()

    def _send(self, query: dict[str, Any]) -> None:
        if self._native is None or self._client_id is None:
            raise RuntimeError("TDLib is not started.")
        self._native.send(self._client_id, query)

    def _require_state(self, expected: TelegramAuthorizationState) -> None:
        if self._state is not expected:
            raise TelegramAuthorizationError(
                "invalid_authorization_state",
                f"This action is unavailable while authorization is {self._state.value}.",
            )

    @staticmethod
    def _validate_secret(value: str, label: str, *, maximum_length: int) -> None:
        if not value.strip() or len(value) > maximum_length:
            raise TelegramAuthorizationError(
                "invalid_secret",
                f"Enter a valid {label}.",
                status_code=400,
            )

    @staticmethod
    def _authorization_error(
        response: dict[str, Any],
        action: str,
    ) -> TelegramAuthorizationError:
        code = int(response.get("code", 500))
        message = str(response.get("message", "")).upper()

        if code == 406:
            return TelegramAuthorizationError(
                "telegram_rejected_request",
                f"Telegram rejected the {action} step.",
                status_code=400,
            )
        if "FLOOD" in message or code == 429:
            return TelegramAuthorizationError(
                "telegram_rate_limited",
                "Telegram rate-limited this authorization attempt. Wait before trying again.",
                status_code=429,
            )
        if "PHONE_NUMBER_INVALID" in message:
            return TelegramAuthorizationError(
                "invalid_phone_number",
                "Telegram rejected this phone-number format.",
                status_code=400,
            )
        if "PHONE_CODE_EXPIRED" in message:
            return TelegramAuthorizationError(
                "expired_code",
                "The Telegram authorization code expired. Request a new login attempt later.",
                status_code=409,
            )
        if "PHONE_CODE" in message:
            return TelegramAuthorizationError(
                "invalid_code",
                "Telegram rejected that authorization code.",
                status_code=400,
            )
        if "PASSWORD_HASH_INVALID" in message:
            return TelegramAuthorizationError(
                "invalid_password",
                "Telegram rejected that two-step verification password.",
                status_code=400,
            )
        if "API_ID_INVALID" in message:
            return TelegramAuthorizationError(
                "invalid_api_credentials",
                "Telegram rejected the server's API application credentials.",
                status_code=503,
            )
        return TelegramAuthorizationError(
            "telegram_rejected_request",
            f"Telegram rejected the {action} step.",
            status_code=400,
        )

    def _set_status(
        self,
        state: TelegramAuthorizationState,
        detail: str,
        next_action: str | None,
        *,
        password_hint: str | None = None,
    ) -> None:
        self._state = state
        self._detail = detail
        self._next_action = next_action
        self._password_hint = password_hint
        self._state_changed.set()

    def _set_error(self, detail: str, next_action: str) -> None:
        self._set_status(TelegramAuthorizationState.ERROR, detail, next_action)

    def _status(self) -> TelegramAuthorizationStatus:
        return TelegramAuthorizationStatus(
            state=self._state,
            detail=self._detail,
            next_action=self._next_action,
            password_hint=self._password_hint,
            is_mock=False,
        )
