from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


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
