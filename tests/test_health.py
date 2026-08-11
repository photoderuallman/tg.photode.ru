import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx

from backend.main import app
from backend.models import TelegramEvent, TelegramTextMessage
from backend.telegram.mock import MockTelegramService


async def _get(path: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path)


async def _mock_authorization_sequence() -> list[httpx.Response]:
    async with app.router.lifespan_context(app):
        app.state.telegram_service = MockTelegramService(
            enabled=True,
            require_password=True,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            requests: list[tuple[str, str, dict[str, Any] | None]] = [
                ("GET", "/api/telegram/auth", None),
                ("POST", "/api/telegram/auth/phone", {"phone_number": "+12223334455"}),
                ("POST", "/api/telegram/auth/code", {"code": "mock-code"}),
                ("POST", "/api/telegram/auth/password", {"password": "mock-password"}),
            ]
            return [
                await client.request(method, path, json=payload)
                for method, path, payload in requests
            ]


async def _mock_messaging_sequence() -> list[httpx.Response]:
    async with app.router.lifespan_context(app):
        service = MockTelegramService(enabled=True, require_password=False)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("mock-code")
        app.state.telegram_service = service
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return [
                await client.get("/api/telegram/me"),
                await client.get("/api/chats?limit=10"),
                await client.post(
                    "/api/chats/1/messages",
                    json={"text": "terminal smoke test"},
                ),
                await client.get("/api/chats/1/messages?limit=10"),
                await client.get("/api/chats/999/messages"),
            ]


async def _mock_history_pagination() -> tuple[httpx.Response, httpx.Response]:
    async with app.router.lifespan_context(app):
        service = MockTelegramService(enabled=True, require_password=False)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("mock-code")
        app.state.telegram_service = service
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            for number in range(1, 36):
                response = await client.post(
                    "/api/chats/1/messages",
                    json={"text": f"message {number}"},
                )
                assert response.status_code == 201

            latest = await client.get("/api/chats/1/messages?limit=30")
            oldest_loaded_id = latest.json()[-1]["id"]
            older = await client.get(
                "/api/chats/1/messages",
                params={
                    "limit": 30,
                    "before_message_id": oldest_loaded_id,
                },
            )
            return latest, older


async def _mock_filtered_event_response() -> httpx.Response:
    async with app.router.lifespan_context(app):
        service = MockTelegramService(enabled=True, require_password=False)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("mock-code")
        app.state.telegram_service = service

        async def event_stream():
            for chat_id, text in ((2, "other chat"), (1, "selected chat")):
                yield TelegramEvent(
                    type="message.new",
                    chat_id=chat_id,
                    message=TelegramTextMessage(
                        id=chat_id,
                        chat_id=chat_id,
                        sender_id=2000,
                        sender_type="user",
                        is_outgoing=False,
                        sent_at=datetime.now(UTC),
                        text=text,
                    ),
                )

        service.event_stream = event_stream  # type: ignore[method-assign]
        app.state.telegram_service = service
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/api/events?chat_id=1")


async def _mock_long_poll_event_response() -> tuple[httpx.Response, httpx.Response]:
    async with app.router.lifespan_context(app):
        service = MockTelegramService(enabled=True, require_password=False)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("mock-code")
        app.state.telegram_service = service

        async def event_stream():
            yield TelegramEvent(
                type="message.new",
                chat_id=1,
                message=TelegramTextMessage(
                    id=7,
                    chat_id=1,
                    sender_id=2000,
                    sender_type="user",
                    is_outgoing=False,
                    sent_at=datetime.now(UTC),
                    text="long poll message",
                ),
            )

        async def empty_event_stream():
            if False:
                yield TelegramEvent(type="unused")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            service.event_stream = event_stream  # type: ignore[method-assign]
            event_response = await client.get(
                "/api/events/next?chat_id=1&timeout_seconds=1"
            )
            service.event_stream = empty_event_stream  # type: ignore[method-assign]
            timeout_response = await client.get(
                "/api/events/next?chat_id=1&timeout_seconds=1"
            )
            return event_response, timeout_response


async def _mock_active_chat_unfiltered_response() -> tuple[httpx.Response, int]:
    async with app.router.lifespan_context(app):
        service = MockTelegramService(enabled=True, require_password=False)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("mock-code")
        app.state.telegram_service = service

        async def event_stream():
            yield TelegramEvent(
                type="message.new",
                chat_id=2,
                message=TelegramTextMessage(
                    id=8,
                    chat_id=2,
                    sender_id=3000,
                    sender_type="user",
                    is_outgoing=False,
                    sent_at=datetime.now(UTC),
                    text="another chat stays live",
                ),
            )

        service.event_stream = event_stream  # type: ignore[method-assign]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/events/next?active_chat_id=1&timeout_seconds=1"
            )
        return response, service._open_chat_counts.get(1, 0)


async def _trigger_transport_check(directory: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        trigger = Path(directory) / "request"
        app.state.settings = replace(
            app.state.settings,
            vpn_check_trigger_path=str(trigger),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post("/api/transport/check", json={})
        assert trigger.is_file()
        return response


async def _mock_extended_api_sequence() -> list[httpx.Response]:
    with TemporaryDirectory() as directory:
        async with app.router.lifespan_context(app):
            app.state.settings = replace(
                app.state.settings,
                tdlib_files_directory=directory,
            )
            service = MockTelegramService(enabled=True, require_password=False)
            await service.submit_phone_number("+12223334455")
            await service.submit_code("mock-code")
            app.state.telegram_service = service
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return [
                    await client.get("/api/users/2000"),
                    await client.post(
                        "/api/chats/1/actions",
                        json={"action": "typing"},
                    ),
                    await client.post(
                        "/api/chats/1/read",
                        json={"message_ids": [1]},
                    ),
                    await client.post("/api/chats/1/messages/1/open"),
                    await client.post(
                        "/api/chats/1/messages",
                        json={
                            "text": "🙂",
                            "entities": [
                                {
                                    "offset": 0,
                                    "length": 2,
                                    "type": "custom_emoji",
                                    "custom_emoji_id": 9999,
                                }
                            ],
                        },
                    ),
                    await client.get("/api/emojis/custom/9999"),
                    await client.post(
                        "/api/chats/1/media",
                        data={"kind": "photo", "caption": "photo caption"},
                        files={
                            "file": (
                                "photo.jpg",
                                b"\xff\xd8\xffmock-jpeg",
                                "image/jpeg",
                            )
                        },
                    ),
                ]


def test_health_endpoint() -> None:
    response = asyncio.run(_get("/api/health"))

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "0.1.0"


def test_status_waits_for_vpn_before_telegram() -> None:
    response = asyncio.run(_get("/api/status"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["app"]["state"] == "ok"
    assert payload["vpn"]["state"] == "waiting"
    assert payload["telegram_network"]["state"] == "waiting"
    assert payload["telegram_auth"]["state"] == "not_configured"


def test_frontend_is_served() -> None:
    response = asyncio.run(_get("/"))

    assert response.status_code == 200
    assert "Lucius P." in response.text
    assert 'id="chat-screen"' in response.text


def test_authorization_endpoint_is_locked_by_default() -> None:
    response = asyncio.run(_get("/api/telegram/auth"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"] == "not_configured"


def test_mock_authorization_api_follows_tdlib_shaped_states() -> None:
    responses = asyncio.run(_mock_authorization_sequence())

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert [response.json()["state"] for response in responses] == [
        "wait_phone_number",
        "wait_code",
        "wait_password",
        "ready",
    ]
    combined_responses = " ".join(response.text for response in responses)
    assert "+12223334455" not in combined_responses
    assert "mock-code" not in combined_responses
    assert "mock-password" not in combined_responses


def test_mock_messaging_api_lists_reads_and_sends_text() -> None:
    profile, chats, sent, history, missing = asyncio.run(_mock_messaging_sequence())

    assert profile.status_code == 200
    assert profile.json()["username"] == "mock_operator"
    assert chats.status_code == 200
    assert chats.json()[0]["id"] == 1
    assert sent.status_code == 201
    assert sent.json()["text"] == "terminal smoke test"
    assert history.status_code == 200
    assert history.json()[0]["text"] == "terminal smoke test"
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "chat_not_found"


def test_mock_history_pagination_uses_an_exclusive_message_cursor() -> None:
    latest, older = asyncio.run(_mock_history_pagination())

    assert latest.status_code == 200
    assert older.status_code == 200
    assert [message["text"] for message in latest.json()] == [
        f"message {number}" for number in range(35, 5, -1)
    ]
    assert [message["text"] for message in older.json()] == [
        f"message {number}" for number in range(5, 0, -1)
    ]
    assert {message["id"] for message in latest.json()}.isdisjoint(
        {message["id"] for message in older.json()}
    )


def test_event_stream_filters_updates_by_selected_chat() -> None:
    response = asyncio.run(_mock_filtered_event_response())

    assert response.status_code == 200
    assert "selected chat" in response.text
    assert "other chat" not in response.text


def test_long_poll_returns_an_event_and_clean_timeout() -> None:
    event_response, timeout_response = asyncio.run(_mock_long_poll_event_response())

    assert event_response.status_code == 200
    assert event_response.json()["message"]["text"] == "long poll message"
    assert timeout_response.status_code == 204


def test_active_chat_enables_typing_without_filtering_other_chats() -> None:
    response, remaining_open_count = asyncio.run(
        _mock_active_chat_unfiltered_response()
    )

    assert response.status_code == 200
    assert response.json()["message"]["text"] == "another chat stays live"
    assert remaining_open_count == 0


def test_authenticated_client_can_queue_an_immediate_transport_check() -> None:
    with TemporaryDirectory() as directory:
        response = asyncio.run(_trigger_transport_check(directory))

    assert response.status_code == 202
    assert response.json() == {"accepted": True}


def test_extended_backend_api_contracts() -> None:
    user, action, read, opened, text, emoji, media = asyncio.run(
        _mock_extended_api_sequence()
    )

    assert user.status_code == 200
    assert user.json()["presence"]["state"] == "online"
    assert action.status_code == 200
    assert action.json()["action"] == "typing"
    assert read.status_code == 200
    assert read.json()["message_ids"] == [1]
    assert opened.status_code == 204
    assert text.status_code == 201
    assert text.json()["entities"][0]["custom_emoji_id"] == 9999
    assert emoji.status_code == 200
    assert emoji.json()["download_url"] == "/api/files/500"
    assert media.status_code == 201
    assert media.json()["kind"] == "photo"
    assert media.json()["media"]["file_id"] == 101
