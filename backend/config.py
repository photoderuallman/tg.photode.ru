from __future__ import annotations

import os
from dataclasses import dataclass, field


def _environment_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Personal Telegram Gateway"
    app_version: str = "0.1.0"
    environment: str = "development"
    vpn_interface: str = "tg-vpn"
    telegram_probe_host: str = ""
    telegram_probe_port: int = 443
    telegram_auth_mode: str = "disabled"
    telegram_mock_require_password: bool = True
    telegram_api_id: int | None = None
    telegram_api_hash: str = field(default="", repr=False)
    tdlib_database_directory: str = "storage/tdlib"
    tdlib_files_directory: str = "storage/tdlib/files"
    tdlib_database_encryption_key: str = field(default="", repr=False)
    tdlib_library_path: str = ""

    @classmethod
    def from_environment(cls) -> "Settings":
        telegram_auth_mode = os.getenv("TELEGRAM_AUTH_MODE", "disabled").strip().lower()
        if telegram_auth_mode not in {"disabled", "mock", "tdlib"}:
            raise ValueError(
                "TELEGRAM_AUTH_MODE must be 'disabled', 'mock', or 'tdlib'."
            )

        api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        database_directory = os.getenv(
            "TDLIB_DATABASE_DIRECTORY",
            "storage/tdlib",
        ).strip()
        database_encryption_key = os.getenv(
            "TDLIB_DATABASE_ENCRYPTION_KEY",
            "",
        ).strip()

        try:
            api_id = int(api_id_raw) if api_id_raw else None
        except ValueError as error:
            raise ValueError("TELEGRAM_API_ID must be a positive integer.") from error

        if telegram_auth_mode == "tdlib":
            if api_id is None or api_id <= 0:
                raise ValueError("TELEGRAM_API_ID is required in tdlib mode.")
            if len(api_hash) != 32 or any(
                character not in "0123456789abcdefABCDEF" for character in api_hash
            ):
                raise ValueError("TELEGRAM_API_HASH must be a 32-character hexadecimal value.")
            if not database_directory:
                raise ValueError("TDLIB_DATABASE_DIRECTORY is required in tdlib mode.")
            if not database_encryption_key:
                raise ValueError(
                    "TDLIB_DATABASE_ENCRYPTION_KEY is required in tdlib mode."
                )

        return cls(
            environment=os.getenv("APP_ENV", "development"),
            vpn_interface=os.getenv("VPN_INTERFACE", "tg-vpn"),
            telegram_probe_host=os.getenv("TELEGRAM_PROBE_HOST", "").strip(),
            telegram_probe_port=int(os.getenv("TELEGRAM_PROBE_PORT", "443")),
            telegram_auth_mode=telegram_auth_mode,
            telegram_mock_require_password=_environment_flag(
                "TELEGRAM_MOCK_REQUIRE_PASSWORD",
                True,
            ),
            telegram_api_id=api_id,
            telegram_api_hash=api_hash,
            tdlib_database_directory=database_directory,
            tdlib_files_directory=os.getenv(
                "TDLIB_FILES_DIRECTORY",
                f"{database_directory}/files",
            ).strip(),
            tdlib_database_encryption_key=database_encryption_key,
            tdlib_library_path=os.getenv("TDLIB_LIBRARY_PATH", "").strip(),
        )
