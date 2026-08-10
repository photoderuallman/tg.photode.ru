from __future__ import annotations

import asyncio

import httpx

from backend.main import app


def _configure_multi_account(monkeypatch, tmp_path, *, require_password: bool) -> None:
    monkeypatch.setenv("TELEGRAM_AUTH_MODE", "mock")
    monkeypatch.setenv(
        "TELEGRAM_MOCK_REQUIRE_PASSWORD",
        "true" if require_password else "false",
    )
    monkeypatch.setenv("TELEGRAM_MULTI_ACCOUNT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_MAX_ACCOUNT_SESSIONS", "3")
    monkeypatch.setenv("TDLIB_ACCOUNTS_DIRECTORY", str(tmp_path / "accounts"))
    monkeypatch.setenv("TELEGRAM_ACCOUNT_TOKEN_SECRET", "m" * 32)


async def _login(client: httpx.AsyncClient, phone: str) -> dict:
    phone_response = await client.post(
        "/api/auth/phone",
        json={"phone_number": phone},
    )
    assert phone_response.status_code == 200
    assert phone_response.json()["state"] == "wait_code"
    code_response = await client.post(
        "/api/auth/code",
        json={
            "flow_token": phone_response.json()["flow_token"],
            "code": "12345",
        },
    )
    assert code_response.status_code == 200
    return code_response.json()


async def _isolated_login_sequence() -> tuple[list[dict], int, int]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first = await _login(client, "+12223334455")
            second = await _login(client, "+12223334456")
            first_headers = {"Authorization": f"Bearer {first['token']}"}
            second_headers = {"Authorization": f"Bearer {second['token']}"}

            sent = await client.post(
                "/api/chats/1/messages",
                json={"text": "first account only"},
                headers=first_headers,
            )
            first_messages = await client.get(
                "/api/chats/1/messages?limit=50",
                headers=first_headers,
            )
            second_messages = await client.get(
                "/api/chats/1/messages?limit=50",
                headers=second_headers,
            )
            return first_messages.json(), len(second_messages.json()), sent.status_code


def test_phone_code_sessions_are_isolated(monkeypatch, tmp_path) -> None:
    _configure_multi_account(monkeypatch, tmp_path, require_password=False)

    first_messages, second_count, sent_status = asyncio.run(
        _isolated_login_sequence()
    )

    assert sent_status == 201
    assert first_messages[0]["text"] == "first account only"
    assert second_count == 0


async def _password_and_logout_sequence() -> tuple[dict, dict, int]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            code = await _login(client, "+12223334455")
            password = await client.post(
                "/api/auth/password",
                json={
                    "flow_token": code["flow_token"],
                    "password": "telegram-2fa",
                },
            )
            token = password.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            logout = await client.post("/api/auth/logout", headers=headers)
            after_logout = await client.get("/api/chats", headers=headers)
            return code, password.json(), after_logout.status_code


def test_optional_telegram_password_and_logout(monkeypatch, tmp_path) -> None:
    _configure_multi_account(monkeypatch, tmp_path, require_password=True)

    code, password, after_logout_status = asyncio.run(
        _password_and_logout_sequence()
    )

    assert code["state"] == "wait_password"
    assert code["token"] is None
    assert password["state"] == "ready"
    assert password["token"]
    assert password["account"]["id"] == 1000
    assert after_logout_status == 401
