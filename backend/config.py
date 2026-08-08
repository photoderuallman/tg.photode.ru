from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Personal Telegram Gateway"
    app_version: str = "0.1.0"
    environment: str = "development"
    vpn_interface: str = "tg-vpn"
    telegram_probe_host: str = ""
    telegram_probe_port: int = 443

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            environment=os.getenv("APP_ENV", "development"),
            vpn_interface=os.getenv("VPN_INTERFACE", "tg-vpn"),
            telegram_probe_host=os.getenv("TELEGRAM_PROBE_HOST", "").strip(),
            telegram_probe_port=int(os.getenv("TELEGRAM_PROBE_PORT", "443")),
        )
