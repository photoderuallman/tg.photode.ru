import asyncio

from backend.models import ComponentState
from backend.telegram.mock import MockTelegramService


def test_mock_service_reports_authorization_boundary() -> None:
    status = asyncio.run(MockTelegramService().get_authorization_status())

    assert status.state is ComponentState.NOT_CONFIGURED
    assert "TDLib" in status.detail
