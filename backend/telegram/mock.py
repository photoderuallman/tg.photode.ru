from __future__ import annotations

import asyncio

from backend.models import TelegramAuthorizationState, TelegramAuthorizationStatus
from backend.telegram.base import TelegramAuthorizationError


class MockTelegramService:
    """A deterministic TDLib-shaped authorization flow with no network access."""

    def __init__(self, *, enabled: bool = False, require_password: bool = True) -> None:
        self._enabled = enabled
        self._require_password = require_password
        self._state = (
            TelegramAuthorizationState.WAIT_PHONE_NUMBER
            if enabled
            else TelegramAuthorizationState.NOT_CONFIGURED
        )
        self._lock = asyncio.Lock()

    async def get_authorization_status(self) -> TelegramAuthorizationStatus:
        async with self._lock:
            return self._status()

    async def submit_phone_number(
        self,
        phone_number: str,
    ) -> TelegramAuthorizationStatus:
        async with self._lock:
            self._require_state(TelegramAuthorizationState.WAIT_PHONE_NUMBER)
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

            # The value is intentionally discarded. The real adapter will forward it
            # directly to TDLib without persisting it in application state.
            self._state = TelegramAuthorizationState.WAIT_CODE
            return self._status()

    async def submit_code(self, code: str) -> TelegramAuthorizationStatus:
        async with self._lock:
            self._require_state(TelegramAuthorizationState.WAIT_CODE)
            self._validate_secret(code, "authorization code", maximum_length=64)
            self._state = (
                TelegramAuthorizationState.WAIT_PASSWORD
                if self._require_password
                else TelegramAuthorizationState.READY
            )
            return self._status()

    async def submit_password(self, password: str) -> TelegramAuthorizationStatus:
        async with self._lock:
            self._require_state(TelegramAuthorizationState.WAIT_PASSWORD)
            self._validate_secret(password, "password", maximum_length=256)
            self._state = TelegramAuthorizationState.READY
            return self._status()

    def _require_state(self, expected: TelegramAuthorizationState) -> None:
        if self._state is TelegramAuthorizationState.NOT_CONFIGURED:
            raise TelegramAuthorizationError(
                "not_configured",
                "Telegram authorization is disabled until TDLib credentials are available.",
            )
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

    def _status(self) -> TelegramAuthorizationStatus:
        statuses = {
            TelegramAuthorizationState.NOT_CONFIGURED: TelegramAuthorizationStatus(
                state=self._state,
                detail="TDLib and Telegram API credentials are not connected yet.",
                next_action="Create the Telegram API application, then enable the TDLib adapter.",
                is_mock=False,
            ),
            TelegramAuthorizationState.WAIT_PHONE_NUMBER: TelegramAuthorizationStatus(
                state=self._state,
                detail="Mock authorization is ready. No Telegram request will be sent.",
                next_action="Enter a test phone number to simulate the first TDLib step.",
                is_mock=True,
            ),
            TelegramAuthorizationState.WAIT_CODE: TelegramAuthorizationStatus(
                state=self._state,
                detail="The test phone number was accepted and immediately discarded.",
                next_action="Enter any non-empty code to simulate Telegram verification.",
                is_mock=True,
            ),
            TelegramAuthorizationState.WAIT_PASSWORD: TelegramAuthorizationStatus(
                state=self._state,
                detail="The test code was accepted and immediately discarded.",
                next_action="Enter any non-empty password to simulate optional 2FA.",
                password_hint="Mock two-step verification is enabled.",
                is_mock=True,
            ),
            TelegramAuthorizationState.READY: TelegramAuthorizationStatus(
                state=self._state,
                detail="The mock flow reached ready. No Telegram session was created.",
                next_action="Replace the mock with TDLib after API credentials are available.",
                is_mock=True,
            ),
            TelegramAuthorizationState.ERROR: TelegramAuthorizationStatus(
                state=self._state,
                detail="The authorization adapter reported an error.",
                next_action="Inspect the adapter status before retrying.",
                is_mock=self._enabled,
            ),
        }
        return statuses[self._state]
