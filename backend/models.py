from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


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


class TelegramLoginCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_token: SecretStr
    code: SecretStr


class TelegramLoginPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_token: SecretStr
    password: SecretStr


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: int


class WebSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_key: SecretStr


class WebSessionResponse(BaseModel):
    token: str
    expires_at: datetime


class SystemStatus(BaseModel):
    generated_at: datetime
    app: ComponentStatus
    vpn: ComponentStatus
    telegram_network: ComponentStatus
    telegram_auth: ComponentStatus


class TelegramAccountProfile(BaseModel):
    id: int
    display_name: str
    username: str | None = None
    profile_photo_file_id: int | None = None
    profile_photo_url: str | None = None


class TelegramLoginResponse(BaseModel):
    state: TelegramAuthorizationState
    detail: str
    next_action: str | None = None
    password_hint: str | None = None
    flow_token: str | None = None
    token: str | None = None
    expires_at: datetime | None = None
    account: TelegramAccountProfile | None = None


class TelegramChatSummary(BaseModel):
    id: int
    title: str
    type: str
    unread_count: int
    last_message: str | None = None
    last_message_id: int = 0
    last_message_is_outgoing: bool = False
    peer_user_id: int | None = None
    is_saved_messages: bool = False
    profile_photo_file_id: int | None = None
    profile_photo_url: str | None = None
    last_read_inbox_message_id: int = 0
    last_read_outbox_message_id: int = 0


class TelegramTextEntity(BaseModel):
    offset: int = Field(ge=0)
    length: int = Field(ge=1)
    type: str
    custom_emoji_id: int | None = None


class TelegramMedia(BaseModel):
    kind: Literal["photo", "video", "voice_note", "video_note"]
    file_id: int
    download_url: str
    file_name: str | None = None
    mime_type: str | None = None
    size: int = 0
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    thumbnail_file_id: int | None = None
    is_opened: bool = False


class TelegramMessage(BaseModel):
    id: int
    chat_id: int
    sender_id: int | None = None
    sender_type: Literal["user", "chat", "unknown"] = "unknown"
    is_outgoing: bool
    sent_at: datetime
    kind: Literal[
        "text",
        "photo",
        "video",
        "voice_note",
        "video_note",
        "unsupported",
    ] = "text"
    text: str = ""
    entities: list[TelegramTextEntity] = Field(default_factory=list)
    media: TelegramMedia | None = None
    is_read: bool = False
    sending_state: Literal["pending", "sent", "failed"] = "sent"


# Compatibility name for the first terminal client. The payload is now a generalized
# message while existing text-only imports continue to work.
TelegramTextMessage = TelegramMessage


class TelegramTextMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4096)
    entities: list[TelegramTextEntity] = Field(default_factory=list, max_length=100)


class TelegramReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_ids: list[int] = Field(min_length=1, max_length=100)


class TelegramReadResult(BaseModel):
    chat_id: int
    message_ids: list[int]
    accepted: bool = True


class TelegramChatActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "typing",
        "recording_voice_note",
        "recording_video",
        "recording_video_note",
        "uploading_photo",
        "uploading_video",
        "uploading_voice_note",
        "uploading_video_note",
        "cancel",
    ]
    progress: int = Field(default=0, ge=0, le=100)


class TelegramUserPresence(BaseModel):
    user_id: int
    state: Literal[
        "online",
        "offline",
        "recently",
        "last_week",
        "last_month",
        "unknown",
    ]
    last_seen_at: datetime | None = None
    online_until: datetime | None = None


class TelegramUserProfile(BaseModel):
    id: int
    display_name: str
    username: str | None = None
    is_contact: bool = False
    is_premium: bool = False
    profile_photo_file_id: int | None = None
    profile_photo_url: str | None = None
    presence: TelegramUserPresence


class TelegramChatActionState(BaseModel):
    chat_id: int
    sender_id: int | None = None
    sender_type: Literal["user", "chat", "unknown"] = "unknown"
    action: str
    progress: int | None = None


class TelegramReadReceipt(BaseModel):
    chat_id: int
    direction: Literal["inbox", "outbox"]
    last_read_message_id: int
    unread_count: int | None = None


class TelegramCustomEmoji(BaseModel):
    custom_emoji_id: int
    file_id: int
    download_url: str
    format: Literal["webp", "tgs", "webm", "unknown"]
    width: int
    height: int


class TelegramEvent(BaseModel):
    type: Literal[
        "message.new",
        "message.sent",
        "message.failed",
        "presence.updated",
        "chat.action",
        "receipt.updated",
        "message.content_opened",
        "message.content_updated",
    ]
    chat_id: int | None = None
    message: TelegramMessage | None = None
    presence: TelegramUserPresence | None = None
    action: TelegramChatActionState | None = None
    receipt: TelegramReadReceipt | None = None
    message_id: int | None = None
    old_message_id: int | None = None
    kind: str | None = None
    text: str | None = None
    entities: list[TelegramTextEntity] | None = None
    media: TelegramMedia | None = None
