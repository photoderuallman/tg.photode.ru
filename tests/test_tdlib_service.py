import asyncio
from queue import Empty, Queue
from typing import Any

from backend.models import TelegramAuthorizationState
from backend.telegram.base import TelegramAuthorizationError
from backend.telegram.tdlib import TDLibTelegramService


class FakeTDJsonClient:
    def __init__(self) -> None:
        self.events: Queue[dict[str, Any]] = Queue()
        self.sent: list[dict[str, Any]] = []

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
        elif query_type == "close":
            self._authorization_update("authorizationStateClosed")

    def receive(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.events.get(timeout=timeout)
        except Empty:
            return None

    def _response(self, query: dict[str, Any]) -> None:
        self.events.put({"@type": "ok", "@extra": query["@extra"]})

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
