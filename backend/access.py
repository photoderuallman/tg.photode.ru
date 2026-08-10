from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class ScopedTokenClaims:
    subject: str
    scope: str
    expires_at: datetime


def create_session_token(secret: str, ttl_seconds: int) -> tuple[str, datetime]:
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    payload = {
        "exp": int(expires_at.timestamp()),
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = _signature(secret, encoded)
    return f"{encoded}.{signature}", expires_at


def validate_session_token(token: str, secret: str) -> bool:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _signature(secret, encoded)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return False
        payload = json.loads(_decode(encoded))
        expires_at = int(payload["exp"])
        nonce = str(payload["nonce"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    return bool(nonce) and expires_at > int(datetime.now(UTC).timestamp())


def create_scoped_token(
    secret: str,
    *,
    subject: str,
    scope: str,
    ttl_seconds: int,
) -> tuple[str, datetime]:
    """Create a signed, purpose-bound token without exposing account credentials."""

    if not subject or not scope:
        raise ValueError("A scoped token requires a subject and scope.")
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    payload = {
        "exp": int(expires_at.timestamp()),
        "nonce": secrets.token_urlsafe(12),
        "sub": subject,
        "scope": scope,
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = _signature(secret, encoded)
    return f"{encoded}.{signature}", expires_at


def read_scoped_token(
    token: str,
    secret: str,
    *,
    expected_scope: str,
) -> ScopedTokenClaims | None:
    """Validate a scoped token and return its non-secret routing claims."""

    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _signature(secret, encoded)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_decode(encoded))
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        nonce = str(payload["nonce"])
        subject = str(payload["sub"])
        scope = str(payload["scope"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        not nonce
        or not subject
        or scope != expected_scope
        or expires_at <= datetime.now(UTC)
    ):
        return None
    return ScopedTokenClaims(
        subject=subject,
        scope=scope,
        expires_at=expires_at,
    )


def _signature(secret: str, encoded: str) -> str:
    digest = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return _encode(digest)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
