from __future__ import annotations

from typing import Protocol

from backend.models import TelegramAuthorizationStatus


class TelegramAuthorizationError(Exception):
    """A safe, normalized authorization error suitable for an API response."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TelegramService(Protocol):
    """Browser-independent boundary for Telegram capabilities."""

    async def get_authorization_status(self) -> TelegramAuthorizationStatus:
        """Return our normalized authorization state, never a raw TDLib object."""

    async def submit_phone_number(
        self,
        phone_number: str,
    ) -> TelegramAuthorizationStatus:
        """Submit an international phone number for the active authorization flow."""

    async def submit_code(self, code: str) -> TelegramAuthorizationStatus:
        """Submit the one-time authorization code requested by Telegram."""

    async def submit_password(self, password: str) -> TelegramAuthorizationStatus:
        """Submit the optional Telegram two-step verification password."""
