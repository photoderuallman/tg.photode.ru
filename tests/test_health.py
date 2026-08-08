import asyncio
from typing import Any

import httpx

from backend.main import app
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
    assert "Connection readiness" in response.text
    assert "Controlled handshake" in response.text


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
