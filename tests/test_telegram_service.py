import asyncio

import pytest

from backend.models import TelegramAuthorizationState
from backend.telegram.base import TelegramAuthorizationError
from backend.telegram.mock import MockTelegramService


def test_mock_service_reports_authorization_boundary() -> None:
    status = asyncio.run(MockTelegramService().get_authorization_status())

    assert status.state is TelegramAuthorizationState.NOT_CONFIGURED
    assert "TDLib" in status.detail


async def _replay_mock_events() -> tuple[int | None, int | None, str | None]:
    service = MockTelegramService(enabled=True, require_password=False)
    await service.submit_phone_number("+12223334455")
    await service.submit_code("mock-code")
    first = await service.send_text_message(
        1,
        "first",
        client_request_id="11111111-1111-1111-1111-111111111111",
    )
    first_stream = service.event_stream(after_event_id=0)
    first_event = await anext(first_stream)
    await first_stream.aclose()

    await service.send_text_message(
        1,
        "second",
        client_request_id="22222222-2222-2222-2222-222222222222",
    )
    replay = service.event_stream(after_event_id=first_event.event_id)
    second_event = await anext(replay)
    await replay.aclose()
    return first_event.event_id, second_event.event_id, first.client_request_id


def test_mock_event_cursor_replays_gap_and_preserves_send_identity() -> None:
    first_id, second_id, client_request_id = asyncio.run(_replay_mock_events())

    assert first_id is not None
    assert second_id is not None
    assert second_id == first_id + 1
    assert client_request_id == "11111111-1111-1111-1111-111111111111"


async def _repeat_one_send_request() -> tuple[int, int, int]:
    service = MockTelegramService(enabled=True, require_password=False)
    await service.submit_phone_number("+12223334455")
    await service.submit_code("mock-code")
    request_id = "33333333-3333-3333-3333-333333333333"
    first = await service.send_text_message(
        1,
        "send exactly once",
        client_request_id=request_id,
    )
    repeated = await service.send_text_message(
        1,
        "send exactly once",
        client_request_id=request_id,
    )
    return first.id, repeated.id, len(service._messages)


def test_client_request_id_makes_text_send_idempotent() -> None:
    first_id, repeated_id, message_count = asyncio.run(_repeat_one_send_request())

    assert repeated_id == first_id
    assert message_count == 1


async def _complete_mock_authorization() -> list[TelegramAuthorizationState]:
    service = MockTelegramService(enabled=True, require_password=True)
    states = [(await service.get_authorization_status()).state]
    states.append((await service.submit_phone_number("+12223334455")).state)
    states.append((await service.submit_code("mock-code")).state)
    states.append((await service.submit_password("mock-password")).state)
    return states


def test_mock_service_runs_phone_code_and_password_sequence() -> None:
    states = asyncio.run(_complete_mock_authorization())

    assert states == [
        TelegramAuthorizationState.WAIT_PHONE_NUMBER,
        TelegramAuthorizationState.WAIT_CODE,
        TelegramAuthorizationState.WAIT_PASSWORD,
        TelegramAuthorizationState.READY,
    ]


def test_mock_service_rejects_out_of_order_secret() -> None:
    async def submit_too_early() -> None:
        service = MockTelegramService(enabled=True)
        await service.submit_code("mock-code")

    with pytest.raises(TelegramAuthorizationError) as error:
        asyncio.run(submit_too_early())

    assert error.value.code == "invalid_authorization_state"


def test_mock_service_does_not_retain_submitted_values() -> None:
    async def submit_values() -> MockTelegramService:
        service = MockTelegramService(enabled=True)
        await service.submit_phone_number("+12223334455")
        await service.submit_code("mock-code")
        await service.submit_password("mock-password")
        return service

    service = asyncio.run(submit_values())
    retained_state = repr(vars(service))

    assert "+12223334455" not in retained_state
    assert "mock-code" not in retained_state
    assert "mock-password" not in retained_state
