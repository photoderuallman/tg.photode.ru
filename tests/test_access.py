import asyncio

import httpx

from backend.access import (
    create_scoped_token,
    create_session_token,
    read_scoped_token,
    validate_session_token,
)
from backend.main import app


async def _protected_sequence(monkeypatch) -> list[httpx.Response]:
    monkeypatch.setenv("WEB_AUTH_REQUIRED", "true")
    monkeypatch.setenv("WEB_ACCESS_KEY", "test-access-key-1234567890")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret-that-is-long-enough")
    monkeypatch.setenv("WEB_ALLOWED_ORIGINS", "https://photode.ru")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = await client.get("/api/health")
            locked = await client.get("/api/status")
            rejected = await client.post("/api/session", json={"access_key": "wrong-key"})
            session = await client.post(
                "/api/session",
                json={"access_key": "test-access-key-1234567890"},
            )
            authorized = await client.get(
                "/api/status",
                headers={"Authorization": f"Bearer {session.json()['token']}"},
            )
            return [health, locked, rejected, session, authorized]


async def _single_device_sequence(monkeypatch) -> tuple[httpx.Response, httpx.Response]:
    monkeypatch.setenv("WEB_AUTH_REQUIRED", "true")
    monkeypatch.setenv("WEB_ACCESS_KEY", "test-access-key-1234567890")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret-that-is-long-enough")
    monkeypatch.setenv("WEB_ALLOWED_ORIGINS", "https://photode.ru")
    monkeypatch.setenv("IOS_DEVICE_ACCESS_TOKEN", "device-token-that-is-at-least-32-characters")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            rejected = await client.get(
                "/api/status",
                headers={"Authorization": "Bearer wrong-device-token"},
            )
            authorized = await client.get(
                "/api/status",
                headers={
                    "Authorization": (
                        "Bearer device-token-that-is-at-least-32-characters"
                    )
                },
            )
            return rejected, authorized


def test_signed_web_session_round_trip() -> None:
    token, expires_at = create_session_token("a" * 32, 300)

    assert expires_at.timestamp() > 0
    assert validate_session_token(token, "a" * 32) is True
    assert validate_session_token(token, "b" * 32) is False
    assert validate_session_token(f"{token}x", "a" * 32) is False


def test_scoped_session_tokens_are_purpose_bound() -> None:
    token, expires_at = create_scoped_token(
        "a" * 32,
        subject="0123456789abcdef0123456789abcdef",
        scope="telegram_session",
        ttl_seconds=300,
    )

    claims = read_scoped_token(
        token,
        "a" * 32,
        expected_scope="telegram_session",
    )
    assert claims is not None
    assert claims.subject == "0123456789abcdef0123456789abcdef"
    assert claims.expires_at == expires_at.replace(microsecond=0)
    assert (
        read_scoped_token(token, "a" * 32, expected_scope="telegram_login")
        is None
    )
    assert read_scoped_token(token, "b" * 32, expected_scope="telegram_session") is None


def test_private_api_requires_a_signed_browser_session(monkeypatch) -> None:
    health, locked, rejected, session, authorized = asyncio.run(
        _protected_sequence(monkeypatch)
    )

    assert health.status_code == 200
    assert locked.status_code == 401
    assert locked.json()["detail"]["code"] == "web_session_required"
    assert rejected.status_code == 401
    assert "token" not in rejected.text
    assert session.status_code == 200
    assert session.json()["token"]
    assert authorized.status_code == 200


def test_permanent_ios_device_token_routes_to_single_account(monkeypatch) -> None:
    rejected, authorized = asyncio.run(_single_device_sequence(monkeypatch))

    assert rejected.status_code == 401
    assert authorized.status_code == 200
