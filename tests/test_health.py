import asyncio

import httpx

from backend.main import app


async def _get(path: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path)


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
