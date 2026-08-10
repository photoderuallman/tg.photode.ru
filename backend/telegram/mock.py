from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from backend.models import (
    TelegramAccountProfile,
    TelegramAuthorizationState,
    TelegramAuthorizationStatus,
    TelegramChatActionState,
    TelegramChatSummary,
    TelegramCustomEmoji,
    TelegramEvent,
    TelegramMedia,
    TelegramReadReceipt,
    TelegramReadResult,
    TelegramTextEntity,
    TelegramTextMessage,
    TelegramUserPresence,
    TelegramUserProfile,
)
from backend.telegram.base import TelegramAuthorizationError, TelegramServiceError


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
        self._messages: list[TelegramTextMessage] = []
        self._message_id = 0
        self._file_id = 100
        self._read_inbox_message_id = 0
        self._read_outbox_message_id = 0
        self._open_chat_counts: dict[int, int] = {}
        self._event_queues: set[asyncio.Queue[TelegramEvent]] = set()

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

    async def get_me(self) -> TelegramAccountProfile:
        self._require_ready()
        return TelegramAccountProfile(
            id=1000,
            display_name="Mock Operator",
            username="mock_operator",
        )

    async def get_chats(self, *, limit: int) -> list[TelegramChatSummary]:
        self._require_ready()
        return [
            TelegramChatSummary(
                id=1,
                title="Mock conversation",
                type="private",
                unread_count=0,
                last_message=self._messages[-1].text if self._messages else None,
                peer_user_id=2000,
                last_read_inbox_message_id=self._read_inbox_message_id,
                last_read_outbox_message_id=self._read_outbox_message_id,
            )
        ][:limit]

    async def get_messages(
        self,
        chat_id: int,
        *,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[TelegramTextMessage]:
        self._require_ready()
        if chat_id != 1:
            raise TelegramServiceError(
                "chat_not_found",
                "The requested Telegram chat was not found.",
                status_code=404,
            )
        eligible = self._messages
        if before_message_id is not None:
            eligible = [message for message in eligible if message.id < before_message_id]
        return eligible[-limit:][::-1]

    async def send_text_message(
        self,
        chat_id: int,
        text: str,
        entities: list[TelegramTextEntity] | None = None,
    ) -> TelegramTextMessage:
        self._require_ready()
        if chat_id != 1:
            raise TelegramServiceError(
                "chat_not_found",
                "The requested Telegram chat was not found.",
                status_code=404,
            )
        self._message_id += 1
        message = TelegramTextMessage(
            id=self._message_id,
            chat_id=chat_id,
            sender_id=1000,
            sender_type="user",
            is_outgoing=True,
            sent_at=datetime.now(UTC),
            kind="text",
            text=text,
            entities=entities or [],
        )
        self._messages.append(message)
        self._publish(
            TelegramEvent(type="message.new", chat_id=chat_id, message=message)
        )
        return message

    async def get_user(self, user_id: int) -> TelegramUserProfile:
        self._require_ready()
        if user_id != 2000:
            raise TelegramServiceError(
                "user_not_found",
                "The requested Telegram user was not found.",
                status_code=404,
            )
        return TelegramUserProfile(
            id=user_id,
            display_name="Mock Contact",
            username="mock_contact",
            is_contact=True,
            presence=TelegramUserPresence(user_id=user_id, state="online"),
        )

    async def mark_messages_read(
        self,
        chat_id: int,
        message_ids: list[int],
    ) -> TelegramReadResult:
        self._require_chat(chat_id)
        self._read_inbox_message_id = max(
            self._read_inbox_message_id,
            max(message_ids),
        )
        receipt = TelegramReadReceipt(
            chat_id=chat_id,
            direction="inbox",
            last_read_message_id=self._read_inbox_message_id,
            unread_count=0,
        )
        self._publish(
            TelegramEvent(
                type="receipt.updated",
                chat_id=chat_id,
                receipt=receipt,
            )
        )
        return TelegramReadResult(chat_id=chat_id, message_ids=message_ids)

    async def open_message_content(self, chat_id: int, message_id: int) -> None:
        self._require_chat(chat_id)
        self._publish(
            TelegramEvent(
                type="message.content_opened",
                chat_id=chat_id,
                message_id=message_id,
            )
        )

    async def send_chat_action(
        self,
        chat_id: int,
        action: str,
        *,
        progress: int = 0,
    ) -> TelegramChatActionState:
        self._require_chat(chat_id)
        state = TelegramChatActionState(
            chat_id=chat_id,
            sender_id=1000,
            sender_type="user",
            action=action,
            progress=progress if action.startswith("uploading_") else None,
        )
        self._publish(
            TelegramEvent(type="chat.action", chat_id=chat_id, action=state)
        )
        return state

    async def send_media_message(
        self,
        chat_id: int,
        *,
        kind: str,
        path: Path,
        caption: str = "",
        duration: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> TelegramTextMessage:
        self._require_chat(chat_id)
        if kind not in {"photo", "video", "voice_note", "video_note"}:
            raise TelegramServiceError(
                "unsupported_media_kind",
                "This media kind is not supported.",
                status_code=400,
            )
        self._message_id += 1
        self._file_id += 1
        message = TelegramTextMessage(
            id=self._message_id,
            chat_id=chat_id,
            sender_id=1000,
            sender_type="user",
            is_outgoing=True,
            sent_at=datetime.now(UTC),
            kind=kind,
            text=caption,
            media=TelegramMedia(
                kind=kind,
                file_id=self._file_id,
                download_url=f"/api/files/{self._file_id}",
                file_name=path.name,
                size=path.stat().st_size if path.exists() else 0,
                width=width or None,
                height=height or None,
                duration=duration or None,
            ),
        )
        self._messages.append(message)
        self._publish(
            TelegramEvent(type="message.new", chat_id=chat_id, message=message)
        )
        return message

    async def download_file(self, file_id: int) -> Path:
        self._require_ready()
        raise TelegramServiceError(
            "mock_file_unavailable",
            "Mock Telegram files are not downloadable.",
            status_code=404,
        )

    async def get_custom_emoji(self, custom_emoji_id: int) -> TelegramCustomEmoji:
        self._require_ready()
        return TelegramCustomEmoji(
            custom_emoji_id=custom_emoji_id,
            file_id=500,
            download_url="/api/files/500",
            format="webp",
            width=100,
            height=100,
        )

    async def open_chat(self, chat_id: int) -> None:
        self._require_chat(chat_id)
        self._open_chat_counts[chat_id] = self._open_chat_counts.get(chat_id, 0) + 1

    async def close_chat(self, chat_id: int) -> None:
        count = self._open_chat_counts.get(chat_id, 0)
        if count <= 1:
            self._open_chat_counts.pop(chat_id, None)
        else:
            self._open_chat_counts[chat_id] = count - 1

    async def event_stream(self) -> AsyncIterator[TelegramEvent]:
        self._require_ready()
        queue: asyncio.Queue[TelegramEvent] = asyncio.Queue(maxsize=100)
        self._event_queues.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._event_queues.discard(queue)

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

    def _require_ready(self) -> None:
        if self._state is not TelegramAuthorizationState.READY:
            raise TelegramServiceError(
                "telegram_not_ready",
                "Telegram must be authorized before using chats and messages.",
                status_code=409,
            )

    def _require_chat(self, chat_id: int) -> None:
        self._require_ready()
        if chat_id != 1:
            raise TelegramServiceError(
                "chat_not_found",
                "The requested Telegram chat was not found.",
                status_code=404,
            )

    def _publish(self, event: TelegramEvent) -> None:
        for queue in tuple(self._event_queues):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

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
