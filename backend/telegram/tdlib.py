from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from ctypes import CDLL, c_char_p, c_double, c_int
from ctypes.util import find_library
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any, Protocol

from backend.models import (
    TelegramAccountProfile,
    TelegramAuthorizationState,
    TelegramAuthorizationStatus,
    TelegramChatActionState,
    TelegramChatSummary,
    TelegramCustomEmoji,
    TelegramEvent,
    TelegramMedia,
    TelegramMessage,
    TelegramReadReceipt,
    TelegramReadResult,
    TelegramTextEntity,
    TelegramTextMessage,
    TelegramUserPresence,
    TelegramUserProfile,
)
from backend.telegram.base import TelegramAuthorizationError, TelegramServiceError

logger = logging.getLogger(__name__)

TEXT_ENTITY_TYPES = {
    "bold": "textEntityTypeBold",
    "italic": "textEntityTypeItalic",
    "underline": "textEntityTypeUnderline",
    "strikethrough": "textEntityTypeStrikethrough",
    "spoiler": "textEntityTypeSpoiler",
    "code": "textEntityTypeCode",
    "block_quote": "textEntityTypeBlockQuote",
    "expandable_block_quote": "textEntityTypeExpandableBlockQuote",
    "custom_emoji": "textEntityTypeCustomEmoji",
}

CHAT_ACTION_TYPES = {
    "typing": "chatActionTyping",
    "recording_voice_note": "chatActionRecordingVoiceNote",
    "recording_video": "chatActionRecordingVideo",
    "recording_video_note": "chatActionRecordingVideoNote",
    "uploading_photo": "chatActionUploadingPhoto",
    "uploading_video": "chatActionUploadingVideo",
    "uploading_voice_note": "chatActionUploadingVoiceNote",
    "uploading_video_note": "chatActionUploadingVideoNote",
    "cancel": "chatActionCancel",
}


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
        self._event_queues: set[asyncio.Queue[TelegramEvent]] = set()
        self._event_history: deque[TelegramEvent] = deque(maxlen=256)
        self._event_ids = count(1)
        self._client_request_by_message_id: dict[int, str] = {}
        self._last_read_inbox_by_chat: dict[int, int] = {}
        self._last_read_outbox_by_chat: dict[int, int] = {}
        self._private_chat_by_user: dict[int, int] = {}
        self._self_user_id: int | None = None
        self._open_chat_counts: dict[int, int] = {}
        self._open_chat_lock = asyncio.Lock()

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

    async def get_me(self) -> TelegramAccountProfile:
        self._require_ready()
        user = await self._tdlib_request({"@type": "getMe"}, action="account profile")
        first_name = str(user.get("first_name", "")).strip()
        last_name = str(user.get("last_name", "")).strip()
        display_name = " ".join(part for part in (first_name, last_name) if part)
        usernames = user.get("usernames") or {}
        active_usernames = usernames.get("active_usernames") or []
        user_id = int(user["id"])
        self._self_user_id = user_id
        photo_file_id, photo_url = self._profile_photo_reference(
            user.get("profile_photo") or {}
        )
        return TelegramAccountProfile(
            id=user_id,
            display_name=display_name or "Telegram account",
            username=str(active_usernames[0]) if active_usernames else None,
            profile_photo_file_id=photo_file_id,
            profile_photo_url=photo_url,
        )

    async def get_chats(self, *, limit: int) -> list[TelegramChatSummary]:
        self._require_ready()
        if self._self_user_id is None:
            await self.get_me()
        chat_list = {"@type": "chatListMain"}
        try:
            await self._tdlib_request(
                {"@type": "loadChats", "chat_list": chat_list, "limit": limit},
                action="chat-list load",
            )
        except TelegramServiceError as error:
            if error.code != "telegram_not_found":
                raise

        result = await self._tdlib_request(
            {"@type": "getChats", "chat_list": chat_list, "limit": limit},
            action="chat list",
        )
        chats: list[TelegramChatSummary] = []
        for raw_chat_id in result.get("chat_ids", []):
            try:
                chat = await self._tdlib_request(
                    {"@type": "getChat", "chat_id": int(raw_chat_id)},
                    action="chat details",
                )
            except TelegramServiceError:
                continue
            self._cache_chat(chat)
            chats.append(self._chat_summary(chat))
        return chats

    async def get_messages(
        self,
        chat_id: int,
        *,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[TelegramTextMessage]:
        self._require_ready()
        chat = await self._tdlib_request(
            {"@type": "getChat", "chat_id": chat_id},
            action="chat details",
        )
        self._cache_chat(chat)
        result = await self._tdlib_request(
            {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": before_message_id or 0,
                "offset": 0,
                # offset=0 includes from_message_id, so request one extra row
                # when using an exclusive cursor and remove the cursor below.
                "limit": min(limit + (1 if before_message_id else 0), 100),
                "only_local": False,
            },
            action="chat history",
        )
        messages: list[TelegramTextMessage] = []
        for message in result.get("messages", []):
            normalized = self._message(message)
            if before_message_id is None or normalized.id < before_message_id:
                messages.append(normalized)
        return messages[:limit]

    async def send_text_message(
        self,
        chat_id: int,
        text: str,
        entities: list[TelegramTextEntity] | None = None,
        client_request_id: str | None = None,
    ) -> TelegramTextMessage:
        self._require_ready()
        if not text.strip() or len(text) > 4096:
            raise TelegramServiceError(
                "invalid_message_text",
                "Enter between 1 and 4096 characters of message text.",
                status_code=400,
            )
        message = await self._send_message_content(
            chat_id,
            {
                "@type": "inputMessageText",
                "text": self._formatted_text(text, entities or []),
                "link_preview_options": None,
                "clear_draft": True,
            },
            action="text-message send",
        )
        message_id = int(message.get("id", 0))
        if client_request_id and message_id:
            self._client_request_by_message_id[message_id] = client_request_id
        return self._message(message, client_request_id=client_request_id)

    async def get_user(self, user_id: int) -> TelegramUserProfile:
        self._require_ready()
        user = await self._tdlib_request(
            {"@type": "getUser", "user_id": user_id},
            action="user profile",
        )
        first_name = str(user.get("first_name", "")).strip()
        last_name = str(user.get("last_name", "")).strip()
        usernames = (user.get("usernames") or {}).get("active_usernames") or []
        photo_file_id, photo_url = self._profile_photo_reference(
            user.get("profile_photo") or {}
        )
        return TelegramUserProfile(
            id=int(user["id"]),
            display_name=" ".join(
                part for part in (first_name, last_name) if part
            )
            or "Telegram user",
            username=str(usernames[0]) if usernames else None,
            is_contact=bool(user.get("is_contact", False)),
            is_premium=bool(user.get("is_premium", False)),
            profile_photo_file_id=photo_file_id,
            profile_photo_url=photo_url,
            presence=self._presence(int(user["id"]), user.get("status") or {}),
        )

    async def mark_messages_read(
        self,
        chat_id: int,
        message_ids: list[int],
    ) -> TelegramReadResult:
        self._require_ready()
        unique_ids = list(dict.fromkeys(message_ids))
        if not unique_ids or len(unique_ids) > 100 or any(
            message_id == 0 for message_id in unique_ids
        ):
            raise TelegramServiceError(
                "invalid_message_ids",
                "Provide between 1 and 100 valid Telegram message identifiers.",
                status_code=400,
            )
        await self._tdlib_request(
            {
                "@type": "viewMessages",
                "chat_id": chat_id,
                "message_ids": unique_ids,
                "source": None,
                "force_read": True,
            },
            action="message read",
        )
        return TelegramReadResult(chat_id=chat_id, message_ids=unique_ids)

    async def open_message_content(self, chat_id: int, message_id: int) -> None:
        self._require_ready()
        await self._tdlib_request(
            {
                "@type": "openMessageContent",
                "chat_id": chat_id,
                "message_id": message_id,
            },
            action="message content open",
        )

    async def send_chat_action(
        self,
        chat_id: int,
        action: str,
        *,
        progress: int = 0,
    ) -> TelegramChatActionState:
        self._require_ready()
        td_type = CHAT_ACTION_TYPES.get(action)
        if td_type is None:
            raise TelegramServiceError(
                "unsupported_chat_action",
                "This chat activity is not supported.",
                status_code=400,
            )
        raw_action: dict[str, Any] = {"@type": td_type}
        if action.startswith("uploading_"):
            raw_action["progress"] = max(0, min(progress, 100))
        await self._tdlib_request(
            {
                "@type": "sendChatAction",
                "chat_id": chat_id,
                "message_thread_id": 0,
                "business_connection_id": "",
                "action": raw_action,
            },
            action="chat activity",
        )
        return TelegramChatActionState(
            chat_id=chat_id,
            action=action,
            progress=raw_action.get("progress"),
        )

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
        self._require_ready()
        resolved_path = path.resolve(strict=True)
        input_file = {"@type": "inputFileLocal", "path": str(resolved_path)}
        formatted_caption = self._formatted_text(caption, [])

        if kind == "photo":
            content: dict[str, Any] = {
                "@type": "inputMessagePhoto",
                "photo": input_file,
                "thumbnail": None,
                "added_sticker_file_ids": [],
                "width": max(width, 0),
                "height": max(height, 0),
                "caption": formatted_caption,
                "show_caption_above_media": False,
                "self_destruct_type": None,
                "has_spoiler": False,
            }
        elif kind == "video":
            content = {
                "@type": "inputMessageVideo",
                "video": input_file,
                "thumbnail": None,
                "added_sticker_file_ids": [],
                "duration": max(duration, 0),
                "width": max(width, 0),
                "height": max(height, 0),
                "supports_streaming": True,
                "caption": formatted_caption,
                "show_caption_above_media": False,
                "self_destruct_type": None,
                "has_spoiler": False,
            }
        elif kind == "voice_note":
            content = {
                "@type": "inputMessageVoiceNote",
                "voice_note": input_file,
                "duration": max(duration, 0),
                "waveform": "",
                "caption": formatted_caption,
                "self_destruct_type": None,
            }
        elif kind == "video_note":
            length = max(width, height, 1)
            if duration > 60 or length > 640:
                raise TelegramServiceError(
                    "invalid_video_note",
                    "Video notes must be at most 60 seconds and 640 pixels square.",
                    status_code=400,
                )
            content = {
                "@type": "inputMessageVideoNote",
                "video_note": input_file,
                "thumbnail": None,
                "duration": max(duration, 0),
                "length": length,
                "self_destruct_type": None,
            }
        else:
            raise TelegramServiceError(
                "unsupported_media_kind",
                "Use photo, video, voice_note, or video_note.",
                status_code=400,
            )

        message = await self._send_message_content(
            chat_id,
            content,
            action=f"{kind} send",
        )
        message_id = int(message.get("id", 0))
        if client_request_id and message_id:
            self._client_request_by_message_id[message_id] = client_request_id
        return self._message(message, client_request_id=client_request_id)

    async def download_file(self, file_id: int) -> Path:
        self._require_ready()
        if file_id <= 0:
            raise TelegramServiceError(
                "invalid_file_id",
                "Use a valid Telegram file identifier.",
                status_code=400,
            )
        file = await self._tdlib_request(
            {
                "@type": "downloadFile",
                "file_id": file_id,
                "priority": 16,
                "offset": 0,
                "limit": 0,
                "synchronous": True,
            },
            action="file download",
        )
        local = file.get("local") or {}
        path = str(local.get("path") or "")
        if not local.get("is_downloading_completed") or not path:
            raise TelegramServiceError(
                "file_download_incomplete",
                "Telegram did not finish downloading this file.",
                status_code=502,
            )
        return Path(path)

    async def get_custom_emoji(self, custom_emoji_id: int) -> TelegramCustomEmoji:
        self._require_ready()
        result = await self._tdlib_request(
            {
                "@type": "getCustomEmojiStickers",
                "custom_emoji_ids": [custom_emoji_id],
            },
            action="custom emoji",
        )
        stickers = result.get("stickers") or []
        if not stickers:
            raise TelegramServiceError(
                "custom_emoji_not_found",
                "Telegram couldn't find that custom emoji.",
                status_code=404,
            )
        sticker = stickers[0]
        file = sticker.get("sticker") or {}
        file_id = int(file["id"])
        format_name = self._snake_name(
            str((sticker.get("format") or {}).get("@type", ""))
            .removeprefix("stickerFormat")
        )
        if format_name not in {"webp", "tgs", "webm"}:
            format_name = "unknown"
        return TelegramCustomEmoji(
            custom_emoji_id=custom_emoji_id,
            file_id=file_id,
            download_url=f"/api/files/{file_id}",
            format=format_name,
            width=int(sticker.get("width", 0)),
            height=int(sticker.get("height", 0)),
        )

    async def open_chat(self, chat_id: int) -> None:
        self._require_ready()
        async with self._open_chat_lock:
            count = self._open_chat_counts.get(chat_id, 0)
            if count == 0:
                await self._tdlib_request(
                    {"@type": "openChat", "chat_id": chat_id},
                    action="chat open",
                )
            self._open_chat_counts[chat_id] = count + 1

    async def close_chat(self, chat_id: int) -> None:
        async with self._open_chat_lock:
            count = self._open_chat_counts.get(chat_id, 0)
            if count <= 0:
                return
            if count == 1:
                self._open_chat_counts.pop(chat_id, None)
                await self._tdlib_request(
                    {"@type": "closeChat", "chat_id": chat_id},
                    action="chat close",
                )
            else:
                self._open_chat_counts[chat_id] = count - 1

    async def event_stream(
        self,
        after_event_id: int | None = None,
    ) -> AsyncIterator[TelegramEvent]:
        self._require_ready()
        queue: asyncio.Queue[TelegramEvent] = asyncio.Queue(maxsize=100)
        self._event_queues.add(queue)
        backlog = (
            [
                event
                for event in self._event_history
                if event.event_id is not None and event.event_id > after_event_id
            ]
            if after_event_id is not None
            else []
        )
        try:
            for event in backlog:
                yield event
            while True:
                yield await queue.get()
        finally:
            self._event_queues.discard(queue)

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
        elif event_type == "updateNewMessage":
            raw_message = event.get("message", {})
            message_id = int(raw_message.get("id", 0))
            message = self._message(
                raw_message,
                client_request_id=self._client_request_by_message_id.get(message_id),
            )
            self._publish(
                TelegramEvent(
                    type="message.new",
                    chat_id=message.chat_id,
                    message=message,
                )
            )
        elif event_type == "updateMessageSendSucceeded":
            raw_message = event.get("message", {})
            old_message_id = int(event.get("old_message_id", 0))
            client_request_id = self._client_request_by_message_id.pop(
                old_message_id,
                None,
            )
            new_message_id = int(raw_message.get("id", 0))
            if client_request_id and new_message_id:
                self._client_request_by_message_id[new_message_id] = client_request_id
            message = self._message(
                raw_message,
                client_request_id=client_request_id,
            )
            self._publish(
                TelegramEvent(
                    type="message.sent",
                    chat_id=message.chat_id,
                    message=message,
                    old_message_id=old_message_id or None,
                )
            )
        elif event_type == "updateMessageSendFailed":
            raw_message = event.get("message", {})
            old_message_id = int(event.get("old_message_id", 0))
            client_request_id = self._client_request_by_message_id.pop(
                old_message_id,
                None,
            )
            message = self._message(
                raw_message,
                client_request_id=client_request_id,
            )
            self._publish(
                TelegramEvent(
                    type="message.failed",
                    chat_id=message.chat_id,
                    message=message,
                    old_message_id=old_message_id or None,
                )
            )
        elif event_type == "updateUserStatus":
            user_id = int(event["user_id"])
            presence = self._presence(user_id, event.get("status") or {})
            self._publish(
                TelegramEvent(
                    type="presence.updated",
                    chat_id=self._private_chat_by_user.get(user_id),
                    presence=presence,
                )
            )
        elif event_type == "updateChatAction":
            action = self._chat_action(event)
            self._publish(
                TelegramEvent(
                    type="chat.action",
                    chat_id=action.chat_id,
                    action=action,
                )
            )
        elif event_type == "updateChatReadInbox":
            chat_id = int(event["chat_id"])
            last_read_message_id = int(event["last_read_inbox_message_id"])
            self._last_read_inbox_by_chat[chat_id] = last_read_message_id
            receipt = TelegramReadReceipt(
                chat_id=chat_id,
                direction="inbox",
                last_read_message_id=last_read_message_id,
                unread_count=int(event.get("unread_count", 0)),
            )
            self._publish(
                TelegramEvent(
                    type="receipt.updated",
                    chat_id=chat_id,
                    receipt=receipt,
                )
            )
        elif event_type == "updateChatReadOutbox":
            chat_id = int(event["chat_id"])
            last_read_message_id = int(event["last_read_outbox_message_id"])
            self._last_read_outbox_by_chat[chat_id] = last_read_message_id
            receipt = TelegramReadReceipt(
                chat_id=chat_id,
                direction="outbox",
                last_read_message_id=last_read_message_id,
            )
            self._publish(
                TelegramEvent(
                    type="receipt.updated",
                    chat_id=chat_id,
                    receipt=receipt,
                )
            )
        elif event_type == "updateMessageContentOpened":
            chat_id = int(event["chat_id"])
            self._publish(
                TelegramEvent(
                    type="message.content_opened",
                    chat_id=chat_id,
                    message_id=int(event["message_id"]),
                )
            )
        elif event_type == "updateMessageContent":
            chat_id = int(event["chat_id"])
            kind, text, entities, media = self._message_content(
                event.get("new_content") or {}
            )
            self._publish(
                TelegramEvent(
                    type="message.content_updated",
                    chat_id=chat_id,
                    message_id=int(event["message_id"]),
                    kind=kind,
                    text=text,
                    entities=entities,
                    media=media,
                )
            )
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

        if state_type == "authorizationStateWaitRegistration":
            self._set_error(
                "This phone number does not have a Telegram account.",
                "Create the account in an official Telegram app before signing in here.",
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
        authorization: bool = True,
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
            if authorization:
                raise TelegramAuthorizationError(
                    "telegram_timeout",
                    f"Telegram did not finish the {action} step in time.",
                    status_code=504,
                ) from error
            raise TelegramServiceError(
                "telegram_timeout",
                f"Telegram did not finish the {action} request in time.",
                status_code=504,
            ) from error

        if response.get("@type") == "error":
            if authorization:
                raise self._authorization_error(response, action)
            raise self._service_error(response, action)
        return response

    async def _tdlib_request(
        self,
        query: dict[str, Any],
        *,
        action: str,
    ) -> dict[str, Any]:
        return await self._request(query, action=action, authorization=False)

    async def _send_message_content(
        self,
        chat_id: int,
        content: dict[str, Any],
        *,
        action: str,
    ) -> dict[str, Any]:
        return await self._tdlib_request(
            {
                "@type": "sendMessage",
                "chat_id": chat_id,
                "message_thread_id": 0,
                "reply_to": None,
                "options": None,
                "reply_markup": None,
                "input_message_content": content,
            },
            action=action,
        )

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

    def _require_ready(self) -> None:
        if self._state is not TelegramAuthorizationState.READY:
            raise TelegramServiceError(
                "telegram_not_ready",
                "Telegram must be authorized before using chats and messages.",
                status_code=409,
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

    @staticmethod
    def _service_error(
        response: dict[str, Any],
        action: str,
    ) -> TelegramServiceError:
        code = int(response.get("code", 500))
        message = str(response.get("message", "")).upper()
        if code == 404:
            return TelegramServiceError(
                "telegram_not_found",
                f"Telegram couldn't find the requested {action} resource.",
                status_code=404,
            )
        if code == 429 or "FLOOD" in message:
            return TelegramServiceError(
                "telegram_rate_limited",
                "Telegram rate-limited this request. Wait before trying again.",
                status_code=429,
            )
        if code in {401, 403}:
            return TelegramServiceError(
                "telegram_permission_denied",
                f"Telegram doesn't allow this {action} request.",
                status_code=403,
            )
        if code == 406:
            return TelegramServiceError(
                "telegram_rejected_request",
                f"Telegram rejected the {action} request.",
                status_code=400,
            )
        return TelegramServiceError(
            "telegram_request_failed",
            f"Telegram couldn't complete the {action} request.",
            status_code=502,
        )

    def _chat_summary(self, chat: dict[str, Any]) -> TelegramChatSummary:
        raw_chat_type = chat.get("type") or {}
        chat_type = str(raw_chat_type.get("@type", "chatTypeUnknown"))
        last_message = chat.get("last_message") or {}
        peer_user_id = (
            int(raw_chat_type["user_id"])
            if chat_type == "chatTypePrivate" and raw_chat_type.get("user_id")
            else None
        )
        is_saved_messages = bool(
            peer_user_id is not None and peer_user_id == self._self_user_id
        )
        photo_file_id, photo_url = self._profile_photo_reference(
            chat.get("photo") or {}
        )
        normalized_chat_type = self._snake_name(
            chat_type.removeprefix("chatType")
        )
        if chat_type == "chatTypeSupergroup" and raw_chat_type.get("is_channel"):
            normalized_chat_type = "channel"
        return TelegramChatSummary(
            id=int(chat["id"]),
            title=(
                "Saved Messages"
                if is_saved_messages
                else str(chat.get("title") or "Untitled chat")
            ),
            type=normalized_chat_type,
            unread_count=int(chat.get("unread_count", 0)),
            last_message=self._message_preview(last_message),
            last_message_id=int(last_message.get("id", 0)),
            last_message_is_outgoing=bool(last_message.get("is_outgoing", False)),
            peer_user_id=peer_user_id,
            is_saved_messages=is_saved_messages,
            profile_photo_file_id=photo_file_id,
            profile_photo_url=photo_url,
            last_read_inbox_message_id=int(
                chat.get("last_read_inbox_message_id", 0)
            ),
            last_read_outbox_message_id=int(
                chat.get("last_read_outbox_message_id", 0)
            ),
        )

    @staticmethod
    def _profile_photo_reference(
        photo: dict[str, Any],
    ) -> tuple[int | None, str | None]:
        file = photo.get("small") or photo.get("big") or {}
        file_id = int(file.get("id", 0))
        if not file_id:
            return None, None
        return file_id, f"/api/files/{file_id}"

    def _message(
        self,
        message: dict[str, Any],
        *,
        client_request_id: str | None = None,
    ) -> TelegramMessage:
        chat_id = int(message["chat_id"])
        message_id = int(message["id"])
        is_outgoing = bool(message.get("is_outgoing", False))
        sender_id, sender_type = self._sender(message.get("sender_id") or {})
        kind, text, entities, media = self._message_content(
            message.get("content") or {}
        )
        sending_state_type = str(
            (message.get("sending_state") or {}).get("@type", "")
        )
        if sending_state_type == "messageSendingStatePending":
            sending_state = "pending"
        elif sending_state_type == "messageSendingStateFailed":
            sending_state = "failed"
        else:
            sending_state = "sent"
        read_marker = (
            self._last_read_outbox_by_chat.get(chat_id, 0)
            if is_outgoing
            else self._last_read_inbox_by_chat.get(chat_id, 0)
        )
        return TelegramMessage(
            id=int(message["id"]),
            chat_id=chat_id,
            sender_id=sender_id,
            sender_type=sender_type,
            is_outgoing=is_outgoing,
            sent_at=datetime.fromtimestamp(int(message.get("date", 0)), tz=UTC),
            kind=kind,
            text=text,
            entities=entities,
            media=media,
            is_read=bool(read_marker and message_id <= read_marker),
            sending_state=sending_state,
            client_request_id=client_request_id,
        )

    @classmethod
    def _message_content(
        cls,
        content: dict[str, Any],
    ) -> tuple[str, str, list[TelegramTextEntity], TelegramMedia | None]:
        content_type = str(content.get("@type", ""))
        if content_type == "messageText":
            formatted = content.get("text") or {}
            return (
                "text",
                str(formatted.get("text") or ""),
                cls._text_entities(formatted.get("entities") or []),
                None,
            )
        if content_type == "messagePhoto":
            formatted = content.get("caption") or {}
            return (
                "photo",
                str(formatted.get("text") or ""),
                cls._text_entities(formatted.get("entities") or []),
                cls._photo_media(content.get("photo") or {}),
            )
        if content_type == "messageVideo":
            formatted = content.get("caption") or {}
            return (
                "video",
                str(formatted.get("text") or ""),
                cls._text_entities(formatted.get("entities") or []),
                cls._video_media(content.get("video") or {}),
            )
        if content_type == "messageVoiceNote":
            formatted = content.get("caption") or {}
            return (
                "voice_note",
                str(formatted.get("text") or ""),
                cls._text_entities(formatted.get("entities") or []),
                cls._voice_media(
                    content.get("voice_note") or {},
                    is_opened=bool(content.get("is_listened", False)),
                ),
            )
        if content_type == "messageVideoNote":
            return (
                "video_note",
                "",
                [],
                cls._video_note_media(
                    content.get("video_note") or {},
                    is_opened=bool(content.get("is_viewed", False)),
                ),
            )
        return "unsupported", "", [], None

    @classmethod
    def _photo_media(cls, photo: dict[str, Any]) -> TelegramMedia | None:
        sizes = photo.get("sizes") or []
        if not sizes:
            return None
        size = max(
            sizes,
            key=lambda item: int(item.get("width", 0)) * int(item.get("height", 0)),
        )
        file = size.get("photo") or {}
        return cls._media(
            "photo",
            file,
            mime_type="image/jpeg",
            width=int(size.get("width", 0)),
            height=int(size.get("height", 0)),
        )

    @classmethod
    def _video_media(cls, video: dict[str, Any]) -> TelegramMedia | None:
        thumbnail = (video.get("thumbnail") or {}).get("file") or {}
        return cls._media(
            "video",
            video.get("video") or {},
            file_name=str(video.get("file_name") or "") or None,
            mime_type=str(video.get("mime_type") or "") or None,
            width=int(video.get("width", 0)),
            height=int(video.get("height", 0)),
            duration=int(video.get("duration", 0)),
            thumbnail_file_id=int(thumbnail.get("id", 0)) or None,
        )

    @classmethod
    def _voice_media(
        cls,
        voice: dict[str, Any],
        *,
        is_opened: bool,
    ) -> TelegramMedia | None:
        return cls._media(
            "voice_note",
            voice.get("voice") or {},
            mime_type=str(voice.get("mime_type") or "") or None,
            duration=int(voice.get("duration", 0)),
            is_opened=is_opened,
        )

    @classmethod
    def _video_note_media(
        cls,
        video_note: dict[str, Any],
        *,
        is_opened: bool,
    ) -> TelegramMedia | None:
        thumbnail = (video_note.get("thumbnail") or {}).get("file") or {}
        length = int(video_note.get("length", 0))
        return cls._media(
            "video_note",
            video_note.get("video") or {},
            mime_type="video/mp4",
            width=length,
            height=length,
            duration=int(video_note.get("duration", 0)),
            thumbnail_file_id=int(thumbnail.get("id", 0)) or None,
            is_opened=is_opened,
        )

    @staticmethod
    def _media(
        kind: str,
        file: dict[str, Any],
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        thumbnail_file_id: int | None = None,
        is_opened: bool = False,
    ) -> TelegramMedia | None:
        file_id = int(file.get("id", 0))
        if not file_id:
            return None
        return TelegramMedia(
            kind=kind,
            file_id=file_id,
            download_url=f"/api/files/{file_id}",
            file_name=file_name,
            mime_type=mime_type,
            size=int(file.get("size", 0)),
            width=width or None,
            height=height or None,
            duration=duration or None,
            thumbnail_file_id=thumbnail_file_id,
            is_opened=is_opened,
        )

    @classmethod
    def _text_entities(
        cls,
        entities: list[dict[str, Any]],
    ) -> list[TelegramTextEntity]:
        normalized: list[TelegramTextEntity] = []
        for entity in entities:
            entity_type = entity.get("type") or {}
            raw_name = str(entity_type.get("@type", "")).removeprefix(
                "textEntityType"
            )
            normalized.append(
                TelegramTextEntity(
                    offset=int(entity.get("offset", 0)),
                    length=int(entity.get("length", 0)),
                    type=cls._snake_name(raw_name),
                    custom_emoji_id=(
                        int(entity_type["custom_emoji_id"])
                        if entity_type.get("custom_emoji_id") is not None
                        else None
                    ),
                )
            )
        return normalized

    @staticmethod
    def _formatted_text(
        text: str,
        entities: list[TelegramTextEntity],
    ) -> dict[str, Any]:
        raw_entities: list[dict[str, Any]] = []
        utf16_length = len(text.encode("utf-16-le")) // 2
        for entity in entities:
            if entity.offset + entity.length > utf16_length:
                raise TelegramServiceError(
                    "invalid_text_entity_range",
                    "A text entity extends beyond the UTF-16 message length.",
                    status_code=400,
                )
            td_type = TEXT_ENTITY_TYPES.get(entity.type)
            if td_type is None:
                raise TelegramServiceError(
                    "unsupported_text_entity",
                    f"Text entity '{entity.type}' is not supported.",
                    status_code=400,
                )
            type_payload: dict[str, Any] = {"@type": td_type}
            if entity.type == "custom_emoji":
                if entity.custom_emoji_id is None:
                    raise TelegramServiceError(
                        "missing_custom_emoji_id",
                        "Custom emoji entities require custom_emoji_id.",
                        status_code=400,
                    )
                type_payload["custom_emoji_id"] = entity.custom_emoji_id
            raw_entities.append(
                {
                    "@type": "textEntity",
                    "offset": entity.offset,
                    "length": entity.length,
                    "type": type_payload,
                }
            )
        return {
            "@type": "formattedText",
            "text": text,
            "entities": raw_entities,
        }

    @classmethod
    def _message_preview(cls, message: dict[str, Any]) -> str | None:
        kind, text, _, _ = cls._message_content(message.get("content") or {})
        if text:
            return text
        labels = {
            "photo": "[Photo]",
            "video": "[Video]",
            "voice_note": "[Voice message]",
            "video_note": "[Video message]",
        }
        return labels.get(kind)

    @staticmethod
    def _sender(sender: dict[str, Any]) -> tuple[int | None, str]:
        sender_type_name = sender.get("@type")
        if sender_type_name == "messageSenderUser":
            return int(sender["user_id"]), "user"
        if sender_type_name == "messageSenderChat":
            return int(sender["chat_id"]), "chat"
        return None, "unknown"

    @classmethod
    def _presence(
        cls,
        user_id: int,
        status: dict[str, Any],
    ) -> TelegramUserPresence:
        status_type = str(status.get("@type", ""))
        if status_type == "userStatusOnline":
            expires = int(status.get("expires", 0))
            return TelegramUserPresence(
                user_id=user_id,
                state="online",
                online_until=(
                    datetime.fromtimestamp(expires, tz=UTC) if expires else None
                ),
            )
        if status_type == "userStatusOffline":
            was_online = int(status.get("was_online", 0))
            return TelegramUserPresence(
                user_id=user_id,
                state="offline",
                last_seen_at=(
                    datetime.fromtimestamp(was_online, tz=UTC)
                    if was_online
                    else None
                ),
            )
        states = {
            "userStatusRecently": "recently",
            "userStatusLastWeek": "last_week",
            "userStatusLastMonth": "last_month",
        }
        return TelegramUserPresence(
            user_id=user_id,
            state=states.get(status_type, "unknown"),
        )

    @classmethod
    def _chat_action(cls, event: dict[str, Any]) -> TelegramChatActionState:
        sender_id, sender_type = cls._sender(event.get("sender_id") or {})
        action = event.get("action") or {}
        raw_name = str(action.get("@type", "")).removeprefix("chatAction")
        action_name = cls._snake_name(raw_name) or "cancel"
        return TelegramChatActionState(
            chat_id=int(event["chat_id"]),
            sender_id=sender_id,
            sender_type=sender_type,
            action=action_name,
            progress=(
                int(action["progress"])
                if action.get("progress") is not None
                else None
            ),
        )

    def _cache_chat(self, chat: dict[str, Any]) -> None:
        chat_id = int(chat["id"])
        self._last_read_inbox_by_chat[chat_id] = int(
            chat.get("last_read_inbox_message_id", 0)
        )
        self._last_read_outbox_by_chat[chat_id] = int(
            chat.get("last_read_outbox_message_id", 0)
        )
        chat_type = chat.get("type") or {}
        if chat_type.get("@type") == "chatTypePrivate" and chat_type.get("user_id"):
            self._private_chat_by_user[int(chat_type["user_id"])] = chat_id

    @staticmethod
    def _snake_name(value: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()

    def _publish(self, event: TelegramEvent) -> None:
        event = event.model_copy(update={"event_id": next(self._event_ids)})
        self._event_history.append(event)
        for queue in tuple(self._event_queues):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

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
