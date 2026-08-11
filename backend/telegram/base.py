from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from backend.models import (
    TelegramAccountProfile,
    TelegramAuthorizationStatus,
    TelegramChatActionState,
    TelegramChatSummary,
    TelegramCustomEmoji,
    TelegramEvent,
    TelegramMessage,
    TelegramReadResult,
    TelegramTextEntity,
    TelegramTextMessage,
    TelegramUserProfile,
)


class TelegramServiceError(Exception):
    """A safe, normalized Telegram error suitable for an API response."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TelegramAuthorizationError(TelegramServiceError):
    """A safe error from the Telegram authorization state machine."""


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

    async def get_me(self) -> TelegramAccountProfile:
        """Return a normalized identity for the authorized Telegram account."""

    async def get_chats(self, *, limit: int) -> list[TelegramChatSummary]:
        """Return normalized summaries from the main Telegram chat list."""

    async def get_messages(
        self,
        chat_id: int,
        *,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[TelegramTextMessage]:
        """Return messages before an optional exclusive history cursor."""

    async def send_text_message(
        self,
        chat_id: int,
        text: str,
        entities: list[TelegramTextEntity] | None = None,
        client_request_id: str | None = None,
    ) -> TelegramTextMessage:
        """Send one plain-text message to a Telegram chat."""

    async def get_user(self, user_id: int) -> TelegramUserProfile:
        """Return a normalized Telegram user and current presence state."""

    async def mark_messages_read(
        self,
        chat_id: int,
        message_ids: list[int],
    ) -> TelegramReadResult:
        """Tell Telegram that the current user viewed specific messages."""

    async def open_message_content(self, chat_id: int, message_id: int) -> None:
        """Mark media content as opened/listened/viewed."""

    async def send_chat_action(
        self,
        chat_id: int,
        action: str,
        *,
        progress: int = 0,
    ) -> TelegramChatActionState:
        """Publish typing, recording, uploading, or cancel activity."""

    async def send_media_message(
        self,
        chat_id: int,
        *,
        kind: str,
        path: Path,
        client_request_id: str | None = None,
        caption: str = "",
        duration: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> TelegramMessage:
        """Send a prepared local photo, video, voice note, or video note."""

    async def download_file(self, file_id: int) -> Path:
        """Download a Telegram file and return its TDLib-managed local path."""

    async def get_custom_emoji(self, custom_emoji_id: int) -> TelegramCustomEmoji:
        """Resolve a Telegram custom emoji to a downloadable sticker asset."""

    async def open_chat(self, chat_id: int) -> None:
        """Tell TDLib that at least one client is actively displaying a chat."""

    async def close_chat(self, chat_id: int) -> None:
        """Release one active display reference for a chat."""

    def event_stream(
        self,
        after_event_id: int | None = None,
    ) -> AsyncIterator[TelegramEvent]:
        """Subscribe to normalized realtime Telegram events."""
