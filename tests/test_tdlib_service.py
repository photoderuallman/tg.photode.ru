import asyncio
from queue import Empty, Queue
from typing import Any

from backend.models import TelegramAuthorizationState, TelegramTextEntity
from backend.telegram.base import TelegramAuthorizationError
from backend.telegram.tdlib import TDLibTelegramService


class FakeTDJsonClient:
    def __init__(self) -> None:
        self.events: Queue[dict[str, Any]] = Queue()
        self.sent: list[dict[str, Any]] = []
        self.download_path = ""

    def create_client(self) -> int:
        return 7

    def execute(self, query: dict[str, Any]) -> dict[str, Any]:
        return {"@type": "ok"}

    def send(self, client_id: int, query: dict[str, Any]) -> None:
        assert client_id == 7
        self.sent.append(query.copy())
        query_type = query["@type"]

        if query_type == "getOption":
            self._authorization_update("authorizationStateWaitTdlibParameters")
        elif query_type == "setTdlibParameters":
            self._authorization_update("authorizationStateWaitPhoneNumber")
        elif query_type == "setAuthenticationPhoneNumber":
            self._response(query)
            self._authorization_update("authorizationStateWaitCode")
        elif query_type == "checkAuthenticationCode":
            self._response(query)
            self._authorization_update(
                "authorizationStateWaitPassword",
                password_hint="fake hint",
            )
        elif query_type == "checkAuthenticationPassword":
            self._response(query)
            self._authorization_update("authorizationStateReady")
        elif query_type == "getMe":
            self._response(
                query,
                {
                    "@type": "user",
                    "id": 1000,
                    "first_name": "Test",
                    "last_name": "Operator",
                    "usernames": {
                        "@type": "usernames",
                        "active_usernames": ["test_operator"],
                    },
                    "profile_photo": {
                        "small": self._file(201),
                        "big": self._file(202),
                    },
                },
            )
        elif query_type == "loadChats":
            self._response(query)
        elif query_type == "getChats":
            self._response(query, {"@type": "chats", "chat_ids": [42]})
        elif query_type == "getChat":
            self._response(
                query,
                {
                    "@type": "chat",
                    "id": 42,
                    "type": {"@type": "chatTypePrivate", "user_id": 2000},
                    "title": "Test chat",
                    "photo": {
                        "small": self._file(211),
                        "big": self._file(212),
                    },
                    "unread_count": 1,
                    "last_read_inbox_message_id": 7,
                    "last_read_outbox_message_id": 8,
                    "last_message": self._message(7, "incoming history"),
                },
            )
        elif query_type == "getChatHistory":
            self._response(
                query,
                {
                    "@type": "messages",
                    "total_count": 1,
                    "messages": [self._message(7, "incoming history")],
                },
            )
        elif query_type == "sendMessage":
            input_content = query["input_message_content"]
            if input_content["@type"] == "inputMessageText":
                content = {
                    "@type": "messageText",
                    "text": input_content["text"],
                    "link_preview": None,
                    "link_preview_options": None,
                }
            elif input_content["@type"] == "inputMessagePhoto":
                content = self._photo_content(input_content["caption"])
            elif input_content["@type"] == "inputMessageVideo":
                content = self._video_content(input_content["caption"])
            elif input_content["@type"] == "inputMessageVoiceNote":
                content = self._voice_content(input_content["caption"])
            elif input_content["@type"] == "inputMessageVideoNote":
                content = self._video_note_content()
            else:
                raise AssertionError(f"Unexpected media type: {input_content['@type']}")
            self._response(
                query,
                self._message_with_content(8, content, True),
            )
        elif query_type == "getUser":
            self._response(
                query,
                {
                    "@type": "user",
                    "id": 2000,
                    "first_name": "Test",
                    "last_name": "Contact",
                    "usernames": {
                        "@type": "usernames",
                        "active_usernames": ["test_contact"],
                    },
                    "status": {"@type": "userStatusOffline", "was_online": 1_700_000_000},
                    "is_contact": True,
                    "profile_photo": {
                        "small": self._file(221),
                        "big": self._file(222),
                    },
                    "is_premium": False,
                },
            )
        elif query_type in {
            "viewMessages",
            "openMessageContent",
            "sendChatAction",
            "openChat",
            "closeChat",
        }:
            self._response(query)
        elif query_type == "getCustomEmojiStickers":
            self._response(
                query,
                {
                    "@type": "stickers",
                    "stickers": [
                        {
                            "@type": "sticker",
                            "width": 100,
                            "height": 100,
                            "format": {"@type": "stickerFormatWebp"},
                            "sticker": self._file(501),
                        }
                    ],
                },
            )
        elif query_type == "downloadFile":
            file = self._file(query["file_id"])
            file["local"] = {
                "@type": "localFile",
                "path": self.download_path,
                "is_downloading_completed": True,
            }
            self._response(query, file)
        elif query_type == "close":
            self._authorization_update("authorizationStateClosed")

    def receive(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.events.get(timeout=timeout)
        except Empty:
            return None

    def _response(
        self,
        query: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        response = dict(result or {"@type": "ok"})
        response["@extra"] = query["@extra"]
        self.events.put(response)

    @staticmethod
    def _message(
        message_id: int,
        text: str,
        is_outgoing: bool = False,
    ) -> dict[str, Any]:
        return FakeTDJsonClient._message_with_content(
            message_id,
            {
                "@type": "messageText",
                "text": {
                    "@type": "formattedText",
                    "text": text,
                    "entities": [],
                },
            },
            is_outgoing,
        )

    @staticmethod
    def _message_with_content(
        message_id: int,
        content: dict[str, Any],
        is_outgoing: bool = False,
    ) -> dict[str, Any]:
        return {
            "@type": "message",
            "id": message_id,
            "sender_id": {"@type": "messageSenderUser", "user_id": 2000},
            "chat_id": 42,
            "is_outgoing": is_outgoing,
            "date": 1_700_000_000,
            "content": content,
        }

    @staticmethod
    def _file(file_id: int) -> dict[str, Any]:
        return {
            "@type": "file",
            "id": file_id,
            "size": 1234,
            "expected_size": 1234,
            "local": {"@type": "localFile", "path": ""},
            "remote": {"@type": "remoteFile", "id": "remote"},
        }

    @classmethod
    def _photo_content(cls, caption: dict[str, Any]) -> dict[str, Any]:
        return {
            "@type": "messagePhoto",
            "photo": {
                "@type": "photo",
                "sizes": [
                    {
                        "@type": "photoSize",
                        "type": "x",
                        "photo": cls._file(301),
                        "width": 800,
                        "height": 600,
                        "progressive_sizes": [],
                    }
                ],
            },
            "caption": caption,
            "show_caption_above_media": False,
            "has_spoiler": False,
            "is_secret": False,
        }

    @classmethod
    def _video_content(cls, caption: dict[str, Any]) -> dict[str, Any]:
        return {
            "@type": "messageVideo",
            "video": {
                "@type": "video",
                "duration": 12,
                "width": 1280,
                "height": 720,
                "file_name": "video.mp4",
                "mime_type": "video/mp4",
                "thumbnail": None,
                "video": cls._file(302),
            },
            "caption": caption,
            "show_caption_above_media": False,
            "has_spoiler": False,
            "is_secret": False,
        }

    @classmethod
    def _voice_content(cls, caption: dict[str, Any]) -> dict[str, Any]:
        return {
            "@type": "messageVoiceNote",
            "voice_note": {
                "@type": "voiceNote",
                "duration": 5,
                "mime_type": "audio/ogg",
                "voice": cls._file(303),
            },
            "caption": caption,
            "is_listened": False,
        }

    @classmethod
    def _video_note_content(cls) -> dict[str, Any]:
        return {
            "@type": "messageVideoNote",
            "video_note": {
                "@type": "videoNote",
                "duration": 9,
                "length": 480,
                "thumbnail": None,
                "video": cls._file(304),
            },
            "is_viewed": False,
            "is_secret": False,
        }

    def _authorization_update(self, state_type: str, **state: Any) -> None:
        self.events.put(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": state_type, **state},
            }
        )


async def _wait_for_state(
    service: TDLibTelegramService,
    expected: TelegramAuthorizationState,
) -> None:
    for _ in range(100):
        if (await service.get_authorization_status()).state is expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"TDLib service never reached {expected.value}")


async def _run_authorization_sequence(tmp_path: Any) -> tuple[list[str], list[dict[str, Any]]]:
    fake = FakeTDJsonClient()
    service = TDLibTelegramService(
        api_id=12345,
        api_hash="0123456789abcdef0123456789abcdef",
        database_directory=str(tmp_path / "database"),
        files_directory=str(tmp_path / "files"),
        database_encryption_key="ZmFrZS1lbmNyeXB0aW9uLWtleQ==",
        native_client=fake,
    )

    await service.start()
    try:
        await _wait_for_state(service, TelegramAuthorizationState.WAIT_PHONE_NUMBER)
        states = [(await service.get_authorization_status()).state.value]
        states.append((await service.submit_phone_number("+12223334455")).state.value)
        states.append((await service.submit_code("12345")).state.value)
        states.append((await service.submit_password("fake-password")).state.value)
        return states, fake.sent
    finally:
        await service.stop()


def test_tdlib_service_runs_real_authorization_shape(tmp_path: Any) -> None:
    states, sent = asyncio.run(_run_authorization_sequence(tmp_path))

    assert states == [
        "wait_phone_number",
        "wait_code",
        "wait_password",
        "ready",
    ]
    parameters = next(query for query in sent if query["@type"] == "setTdlibParameters")
    assert parameters["use_test_dc"] is False
    assert parameters["database_encryption_key"]
    assert "parameters" not in parameters


def test_tdlib_rate_limit_is_normalized_without_raw_error() -> None:
    error = TDLibTelegramService._authorization_error(
        {"@type": "error", "code": 429, "message": "FLOOD_WAIT_123"},
        "phone number",
    )

    assert isinstance(error, TelegramAuthorizationError)
    assert error.code == "telegram_rate_limited"
    assert error.status_code == 429
    assert "123" not in str(error)


async def _run_messaging_sequence(tmp_path: Any) -> dict[str, Any]:
    fake = FakeTDJsonClient()
    service = TDLibTelegramService(
        api_id=12345,
        api_hash="0123456789abcdef0123456789abcdef",
        database_directory=str(tmp_path / "database"),
        files_directory=str(tmp_path / "files"),
        database_encryption_key="ZmFrZS1lbmNyeXB0aW9uLWtleQ==",
        native_client=fake,
    )
    await service.start()
    try:
        await _wait_for_state(service, TelegramAuthorizationState.WAIT_PHONE_NUMBER)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("12345")
        await service.submit_password("fake-password")

        profile = await service.get_me()
        chats = await service.get_chats(limit=10)
        history = await service.get_messages(42, limit=10)
        older_history = await service.get_messages(
            42,
            limit=10,
            before_message_id=7,
        )
        sent = await service.send_text_message(42, "outgoing smoke test")

        stream = service.event_stream()
        event_task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        fake.events.put(
            {
                "@type": "updateNewMessage",
                "message": fake._message(9, "live incoming"),
            }
        )
        event = await asyncio.wait_for(event_task, timeout=1)
        await stream.aclose()
        return {
            "profile": profile,
            "chats": chats,
            "history": history,
            "older_history": older_history,
            "sent": sent,
            "event": event,
            "queries": fake.sent,
        }
    finally:
        await service.stop()


def test_tdlib_service_normalizes_terminal_messaging_flow(tmp_path: Any) -> None:
    result = asyncio.run(_run_messaging_sequence(tmp_path))

    assert result["profile"].display_name == "Test Operator"
    assert result["profile"].profile_photo_file_id == 201
    assert result["profile"].profile_photo_url == "/api/files/201"
    assert result["chats"][0].id == 42
    assert result["chats"][0].profile_photo_file_id == 211
    assert result["chats"][0].profile_photo_url == "/api/files/211"
    assert result["chats"][0].last_message == "incoming history"
    assert result["chats"][0].last_message_id == 7
    assert result["chats"][0].last_message_is_outgoing is False
    assert result["history"][0].text == "incoming history"
    assert result["older_history"] == []
    assert result["sent"].text == "outgoing smoke test"
    assert result["event"].message.text == "live incoming"
    send_query = next(
        query for query in result["queries"] if query["@type"] == "sendMessage"
    )
    assert send_query["input_message_content"]["@type"] == "inputMessageText"
    history_queries = [
        query for query in result["queries"] if query["@type"] == "getChatHistory"
    ]
    assert history_queries[0]["from_message_id"] == 0
    assert history_queries[0]["limit"] == 10
    assert history_queries[1]["from_message_id"] == 7
    assert history_queries[1]["limit"] == 11


def test_tdlib_service_hardcodes_self_chat_as_saved_messages(tmp_path: Any) -> None:
    service = TDLibTelegramService(
        api_id=12345,
        api_hash="0123456789abcdef0123456789abcdef",
        database_directory=str(tmp_path / "database"),
        files_directory=str(tmp_path / "files"),
        database_encryption_key="ZmFrZS1lbmNyeXB0aW9uLWtleQ==",
        native_client=FakeTDJsonClient(),
    )
    service._self_user_id = 1000
    summary = service._chat_summary(
        {
            "id": 99,
            "title": "Test Operator",
            "type": {"@type": "chatTypePrivate", "user_id": 1000},
            "unread_count": 0,
        }
    )

    assert summary.title == "Saved Messages"
    assert summary.is_saved_messages is True


async def _run_extended_capabilities(tmp_path: Any) -> dict[str, Any]:
    fake = FakeTDJsonClient()
    downloaded = tmp_path / "files" / "downloaded-photo.jpg"
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(b"downloaded")
    fake.download_path = str(downloaded)
    service = TDLibTelegramService(
        api_id=12345,
        api_hash="0123456789abcdef0123456789abcdef",
        database_directory=str(tmp_path / "database"),
        files_directory=str(tmp_path / "files"),
        database_encryption_key="ZmFrZS1lbmNyeXB0aW9uLWtleQ==",
        native_client=fake,
    )
    media_file = tmp_path / "upload.bin"
    media_file.write_bytes(b"media")

    await service.start()
    try:
        await _wait_for_state(service, TelegramAuthorizationState.WAIT_PHONE_NUMBER)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("12345")
        await service.submit_password("fake-password")

        chats = await service.get_chats(limit=10)
        user = await service.get_user(2000)
        custom_text = await service.send_text_message(
            42,
            "🙂",
            [
                TelegramTextEntity(
                    offset=0,
                    length=2,
                    type="custom_emoji",
                    custom_emoji_id=9_999,
                )
            ],
        )
        read = await service.mark_messages_read(42, [7])
        await service.open_message_content(42, 7)
        action = await service.send_chat_action(42, "typing")
        media = {
            kind: await service.send_media_message(
                42,
                kind=kind,
                path=media_file,
                caption="caption" if kind != "video_note" else "",
                duration=5,
                width=480,
                height=480,
            )
            for kind in ("photo", "video", "voice_note", "video_note")
        }
        emoji = await service.get_custom_emoji(9_999)
        downloaded_path = await service.download_file(301)
        await service.open_chat(42)
        await service.open_chat(42)
        await service.close_chat(42)
        await service.close_chat(42)

        stream = service.event_stream()

        async def collect_events() -> list[Any]:
            return [await anext(stream) for _ in range(5)]

        events_task = asyncio.create_task(collect_events())
        await asyncio.sleep(0)
        for update in (
            {
                "@type": "updateUserStatus",
                "user_id": 2000,
                "status": {"@type": "userStatusOnline", "expires": 1_800_000_000},
            },
            {
                "@type": "updateChatAction",
                "chat_id": 42,
                "message_thread_id": 0,
                "sender_id": {"@type": "messageSenderUser", "user_id": 2000},
                "action": {"@type": "chatActionRecordingVoiceNote"},
            },
            {
                "@type": "updateChatReadOutbox",
                "chat_id": 42,
                "last_read_outbox_message_id": 8,
            },
            {
                "@type": "updateMessageContentOpened",
                "chat_id": 42,
                "message_id": 7,
            },
            {
                "@type": "updateMessageContent",
                "chat_id": 42,
                "message_id": 8,
                "new_content": {
                    **fake._voice_content(
                        {"@type": "formattedText", "text": "", "entities": []}
                    ),
                    "is_listened": True,
                },
            },
        ):
            fake.events.put(update)
        events = await asyncio.wait_for(events_task, timeout=1)
        await stream.aclose()

        return {
            "chats": chats,
            "user": user,
            "custom_text": custom_text,
            "read": read,
            "action": action,
            "media": media,
            "emoji": emoji,
            "downloaded_path": downloaded_path,
            "events": events,
            "queries": fake.sent,
        }
    finally:
        await service.stop()


def test_tdlib_extended_capabilities_match_exact_schema(tmp_path: Any) -> None:
    result = asyncio.run(_run_extended_capabilities(tmp_path))

    assert result["chats"][0].peer_user_id == 2000
    assert result["chats"][0].last_read_outbox_message_id == 8
    assert result["user"].presence.state == "offline"
    assert result["custom_text"].entities[0].custom_emoji_id == 9_999
    assert result["custom_text"].is_read is True
    assert result["read"].message_ids == [7]
    assert result["action"].action == "typing"
    assert set(result["media"]) == {"photo", "video", "voice_note", "video_note"}
    assert result["media"]["photo"].media.file_id == 301
    assert result["media"]["video"].media.mime_type == "video/mp4"
    assert result["media"]["voice_note"].media.duration == 5
    assert result["media"]["video_note"].media.width == 480
    assert result["emoji"].format == "webp"
    assert result["downloaded_path"].name == "downloaded-photo.jpg"
    assert [event.type for event in result["events"]] == [
        "presence.updated",
        "chat.action",
        "receipt.updated",
        "message.content_opened",
        "message.content_updated",
    ]
    assert result["events"][0].chat_id == 42
    assert result["events"][4].media.is_opened is True

    view_query = next(
        query for query in result["queries"] if query["@type"] == "viewMessages"
    )
    assert view_query["message_ids"] == [7]
    assert view_query["force_read"] is True
    action_query = next(
        query for query in result["queries"] if query["@type"] == "sendChatAction"
    )
    assert action_query["action"]["@type"] == "chatActionTyping"
    sent_types = [
        query["input_message_content"]["@type"]
        for query in result["queries"]
        if query["@type"] == "sendMessage"
    ]
    assert sent_types == [
        "inputMessageText",
        "inputMessagePhoto",
        "inputMessageVideo",
        "inputMessageVoiceNote",
        "inputMessageVideoNote",
    ]
    assert sum(query["@type"] == "openChat" for query in result["queries"]) == 1
    assert sum(query["@type"] == "closeChat" for query in result["queries"]) == 1
