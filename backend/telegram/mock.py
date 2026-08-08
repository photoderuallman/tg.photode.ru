from __future__ import annotations

from backend.models import ComponentState, ComponentStatus


class MockTelegramService:
    async def get_authorization_status(self) -> ComponentStatus:
        return ComponentStatus(
            state=ComponentState.NOT_CONFIGURED,
            label="Telegram account",
            detail="TDLib is not installed yet. No Telegram credentials have been requested.",
            next_action="Install TDLib, then add the private Telegram API credentials.",
        )
