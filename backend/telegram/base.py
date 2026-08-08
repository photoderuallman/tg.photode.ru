from __future__ import annotations

from typing import Protocol

from backend.models import ComponentStatus


class TelegramService(Protocol):
    """Browser-independent boundary for Telegram capabilities."""

    async def get_authorization_status(self) -> ComponentStatus:
        """Return our normalized authorization state, never a raw TDLib object."""
