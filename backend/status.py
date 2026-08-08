from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime

from backend.config import Settings
from backend.models import (
    ComponentState,
    ComponentStatus,
    SystemStatus,
    TelegramAuthorizationState,
    TelegramAuthorizationStatus,
)
from backend.telegram.base import TelegramService


def _interface_is_present(interface_name: str) -> bool:
    try:
        return interface_name in {name for _, name in socket.if_nameindex()}
    except OSError:
        return False


async def _tcp_probe(host: str, port: int, timeout_seconds: float = 3.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_seconds,
        )
    except (OSError, TimeoutError):
        return False

    writer.close()
    await writer.wait_closed()
    return True


async def build_system_status(
    settings: Settings,
    telegram_service: TelegramService,
) -> SystemStatus:
    vpn_ready = _interface_is_present(settings.vpn_interface)

    if vpn_ready:
        vpn = ComponentStatus(
            state=ComponentState.OK,
            label="Server VPN",
            detail=f"Linux interface {settings.vpn_interface} is active.",
        )
    else:
        vpn = ComponentStatus(
            state=ComponentState.WAITING,
            label="Server VPN",
            detail=f"Linux interface {settings.vpn_interface} is not active.",
            next_action="Install the VLESS/Reality client and verify its kill-safe route.",
        )

    if not vpn_ready:
        telegram_network = ComponentStatus(
            state=ComponentState.WAITING,
            label="Telegram network",
            detail="Network probing is paused until the server VPN is active.",
            next_action="Verify the VPN before allowing Telegram traffic.",
        )
    elif not settings.telegram_probe_host:
        telegram_network = ComponentStatus(
            state=ComponentState.NOT_CONFIGURED,
            label="Telegram network",
            detail="The VPN is active, but no Telegram probe target is configured.",
            next_action="Set TELEGRAM_PROBE_HOST after the TDLib network check is chosen.",
        )
    else:
        reachable = await _tcp_probe(
            settings.telegram_probe_host,
            settings.telegram_probe_port,
        )
        telegram_network = ComponentStatus(
            state=ComponentState.OK if reachable else ComponentState.OFFLINE,
            label="Telegram network",
            detail=(
                "The configured Telegram probe is reachable through the server route."
                if reachable
                else "The configured Telegram probe did not accept a connection."
            ),
            next_action=None if reachable else "Inspect the VPN service and outbound route.",
        )

    authorization = await telegram_service.get_authorization_status()

    return SystemStatus(
        generated_at=datetime.now(UTC),
        app=ComponentStatus(
            state=ComponentState.OK,
            label="Web application",
            detail=f"FastAPI {settings.app_version} is responding.",
        ),
        vpn=vpn,
        telegram_network=telegram_network,
        telegram_auth=_authorization_component(authorization),
    )


def _authorization_component(
    authorization: TelegramAuthorizationStatus,
) -> ComponentStatus:
    state_map = {
        TelegramAuthorizationState.NOT_CONFIGURED: ComponentState.NOT_CONFIGURED,
        TelegramAuthorizationState.WAIT_PHONE_NUMBER: ComponentState.WAITING,
        TelegramAuthorizationState.WAIT_CODE: ComponentState.WAITING,
        TelegramAuthorizationState.WAIT_PASSWORD: ComponentState.WAITING,
        TelegramAuthorizationState.READY: ComponentState.OK,
        TelegramAuthorizationState.ERROR: ComponentState.DEGRADED,
    }
    return ComponentStatus(
        state=state_map[authorization.state],
        label="Telegram account",
        detail=authorization.detail,
        next_action=authorization.next_action,
    )
