from __future__ import annotations

import os
from dataclasses import dataclass, field


def _environment_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def web_allowed_origins_from_environment() -> tuple[str, ...]:
    raw = os.getenv(
        "WEB_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    )
    return tuple(origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Personal Telegram Gateway"
    app_version: str = "0.1.0"
    environment: str = "development"
    vpn_interface: str = "tg-vpn"
    vpn_check_trigger_path: str = ""
    telegram_probe_host: str = ""
    telegram_probe_port: int = 443
    telegram_auth_mode: str = "disabled"
    telegram_mock_require_password: bool = True
    telegram_multi_account_enabled: bool = False
    telegram_max_account_sessions: int = 3
    telegram_api_id: int | None = None
    telegram_api_hash: str = field(default="", repr=False)
    tdlib_database_directory: str = "storage/tdlib"
    tdlib_files_directory: str = "storage/tdlib/files"
    tdlib_database_encryption_key: str = field(default="", repr=False)
    tdlib_library_path: str = ""
    tdlib_accounts_directory: str = "storage/tdlib-accounts"
    media_upload_max_bytes: int = 100 * 1024 * 1024
    media_ffmpeg_path: str = "ffmpeg"
    web_auth_required: bool = False
    web_access_key: str = field(default="", repr=False)
    web_session_secret: str = field(default="", repr=False)
    web_session_ttl_seconds: int = 30 * 24 * 60 * 60
    telegram_login_flow_ttl_seconds: int = 10 * 60
    telegram_account_session_ttl_seconds: int = 30 * 24 * 60 * 60
    telegram_account_token_secret: str = field(default="", repr=False)
    ios_device_access_token: str = field(default="", repr=False)
    web_allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    )

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
        accounts_directory = os.getenv(
            "TDLIB_ACCOUNTS_DIRECTORY",
            "storage/tdlib-accounts",
        ).strip()
        media_upload_max_bytes = int(
            os.getenv("MEDIA_UPLOAD_MAX_BYTES", str(100 * 1024 * 1024))
        )
        if media_upload_max_bytes <= 0:
            raise ValueError("MEDIA_UPLOAD_MAX_BYTES must be a positive integer.")

        web_auth_required = _environment_flag("WEB_AUTH_REQUIRED", False)
        web_access_key = os.getenv("WEB_ACCESS_KEY", "").strip()
        web_session_secret = os.getenv("WEB_SESSION_SECRET", "").strip()
        web_session_ttl_seconds = int(
            os.getenv("WEB_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60))
        )
        web_allowed_origins = web_allowed_origins_from_environment()
        multi_account_enabled = _environment_flag(
            "TELEGRAM_MULTI_ACCOUNT_ENABLED",
            False,
        )
        max_account_sessions = int(os.getenv("TELEGRAM_MAX_ACCOUNT_SESSIONS", "3"))
        login_flow_ttl_seconds = int(
            os.getenv("TELEGRAM_LOGIN_FLOW_TTL_SECONDS", "600")
        )
        account_session_ttl_seconds = int(
            os.getenv("TELEGRAM_ACCOUNT_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60))
        )
        account_token_secret = os.getenv(
            "TELEGRAM_ACCOUNT_TOKEN_SECRET",
            web_session_secret,
        ).strip()
        ios_device_access_token = os.getenv("IOS_DEVICE_ACCESS_TOKEN", "").strip()
        if ios_device_access_token and len(ios_device_access_token) < 32:
            raise ValueError("IOS_DEVICE_ACCESS_TOKEN must contain at least 32 characters.")
        if web_session_ttl_seconds < 300 or web_session_ttl_seconds > 31_536_000:
            raise ValueError("WEB_SESSION_TTL_SECONDS must be between 300 and 31536000.")
        if web_auth_required:
            if len(web_access_key) < 20:
                raise ValueError("WEB_ACCESS_KEY must contain at least 20 characters.")
            if len(web_session_secret) < 32:
                raise ValueError("WEB_SESSION_SECRET must contain at least 32 characters.")
            if not web_allowed_origins:
                raise ValueError("WEB_ALLOWED_ORIGINS is required when web auth is enabled.")
        if multi_account_enabled:
            if telegram_auth_mode not in {"mock", "tdlib"}:
                raise ValueError(
                    "TELEGRAM_MULTI_ACCOUNT_ENABLED requires mock or tdlib mode."
                )
            if not accounts_directory:
                raise ValueError("TDLIB_ACCOUNTS_DIRECTORY is required in multi-account mode.")
            if len(account_token_secret) < 32:
                raise ValueError(
                    "TELEGRAM_ACCOUNT_TOKEN_SECRET must contain at least 32 characters."
                )
            if not 1 <= max_account_sessions <= 32:
                raise ValueError("TELEGRAM_MAX_ACCOUNT_SESSIONS must be between 1 and 32.")
            if not 300 <= login_flow_ttl_seconds <= 3600:
                raise ValueError(
                    "TELEGRAM_LOGIN_FLOW_TTL_SECONDS must be between 300 and 3600."
                )
            if not 300 <= account_session_ttl_seconds <= 31_536_000:
                raise ValueError(
                    "TELEGRAM_ACCOUNT_SESSION_TTL_SECONDS must be between 300 and 31536000."
                )

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
            vpn_check_trigger_path=os.getenv(
                "VPN_CHECK_TRIGGER_PATH",
                "",
            ).strip(),
            telegram_probe_host=os.getenv("TELEGRAM_PROBE_HOST", "").strip(),
            telegram_probe_port=int(os.getenv("TELEGRAM_PROBE_PORT", "443")),
            telegram_auth_mode=telegram_auth_mode,
            telegram_mock_require_password=_environment_flag(
                "TELEGRAM_MOCK_REQUIRE_PASSWORD",
                True,
            ),
            telegram_multi_account_enabled=multi_account_enabled,
            telegram_max_account_sessions=max_account_sessions,
            telegram_api_id=api_id,
            telegram_api_hash=api_hash,
            tdlib_database_directory=database_directory,
            tdlib_files_directory=os.getenv(
                "TDLIB_FILES_DIRECTORY",
                f"{database_directory}/files",
            ).strip(),
            tdlib_database_encryption_key=database_encryption_key,
            tdlib_library_path=os.getenv("TDLIB_LIBRARY_PATH", "").strip(),
            tdlib_accounts_directory=accounts_directory,
            media_upload_max_bytes=media_upload_max_bytes,
            media_ffmpeg_path=os.getenv("MEDIA_FFMPEG_PATH", "ffmpeg").strip()
            or "ffmpeg",
            web_auth_required=web_auth_required,
            web_access_key=web_access_key,
            web_session_secret=web_session_secret,
            web_session_ttl_seconds=web_session_ttl_seconds,
            telegram_login_flow_ttl_seconds=login_flow_ttl_seconds,
            telegram_account_session_ttl_seconds=account_session_ttl_seconds,
            telegram_account_token_secret=account_token_secret,
            ios_device_access_token=ios_device_access_token,
            web_allowed_origins=web_allowed_origins,
        )
