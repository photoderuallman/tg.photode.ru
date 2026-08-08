from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, SecretStr


class ComponentState(StrEnum):
    OK = "ok"
    WAITING = "waiting"
    NOT_CONFIGURED = "not_configured"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class ComponentStatus(BaseModel):
    state: ComponentState
    label: str
    detail: str
    next_action: str | None = None


class TelegramAuthorizationState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    WAIT_PHONE_NUMBER = "wait_phone_number"
    WAIT_CODE = "wait_code"
    WAIT_PASSWORD = "wait_password"
    READY = "ready"
    ERROR = "error"


class TelegramAuthorizationStatus(BaseModel):
    state: TelegramAuthorizationState
    detail: str
    next_action: str | None = None
    password_hint: str | None = None
    is_mock: bool = False


class TelegramPhoneNumberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_number: SecretStr


class TelegramCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: SecretStr


class TelegramPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: int


class SystemStatus(BaseModel):
    generated_at: datetime
    app: ComponentStatus
    vpn: ComponentStatus
    telegram_network: ComponentStatus
    telegram_auth: ComponentStatus
