import asyncio

import pytest

from backend.models import TelegramAuthorizationState
from backend.telegram.base import TelegramAuthorizationError
from backend.telegram.mock import MockTelegramService


def test_mock_service_reports_authorization_boundary() -> None:
    status = asyncio.run(MockTelegramService().get_authorization_status())

    assert status.state is TelegramAuthorizationState.NOT_CONFIGURED
    assert "TDLib" in status.detail


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
