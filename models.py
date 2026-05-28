from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class KimiPluginError(Exception):
    """Base error surfaced as a concise user-facing message."""


class OAuthError(KimiPluginError):
    pass


class OAuthUnauthorizedError(OAuthError):
    pass


class DeviceCodeTimeoutError(OAuthError):
    pass


class DatasourceError(KimiPluginError):
    pass


class DatasourceAuthError(DatasourceError):
    pass


class DatasourceHTTPError(DatasourceError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status} error: {body}")


class ToolInputError(DatasourceError):
    pass


@dataclass(frozen=True)
class DeviceAuthorization:
    user_code: str
    device_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int | None
    interval: int


@dataclass(frozen=True)
class TokenInfo:
    access_token: str
    refresh_token: str
    expires_at: int
    expires_in: int
    token_type: str
    scope: str


@dataclass(frozen=True)
class DevicePollResult:
    kind: str
    token: TokenInfo | None = None
    error_code: str = ""
    description: str = ""


def token_from_oauth_payload(payload: Mapping[str, Any], now: int) -> TokenInfo:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("OAuth response missing access_token")

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthError("OAuth response missing refresh_token")

    try:
        expires_in = int(payload.get("expires_in"))
    except (TypeError, ValueError):
        raise OAuthError("OAuth response missing or invalid expires_in") from None
    if expires_in <= 0:
        raise OAuthError("OAuth response missing or invalid expires_in")

    token_type = payload.get("token_type")
    scope = payload.get("scope")
    return TokenInfo(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=now + expires_in,
        expires_in=expires_in,
        token_type=token_type if isinstance(token_type, str) and token_type else "Bearer",
        scope=scope if isinstance(scope, str) else "",
    )


def token_from_credentials(credentials: Mapping[str, Any]) -> TokenInfo | None:
    if credentials.get("status") == "revoked":
        return None
    access_token = credentials.get("access_token")
    refresh_token = credentials.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    expires_at = credentials.get("expires_at")
    expires_in = credentials.get("expires_in")
    if not isinstance(expires_at, int | float):
        expires_at = 0
    if not isinstance(expires_in, int | float):
        expires_in = 0
    token_type = credentials.get("token_type")
    scope = credentials.get("scope")
    return TokenInfo(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=int(expires_at),
        expires_in=int(expires_in),
        token_type=token_type if isinstance(token_type, str) and token_type else "Bearer",
        scope=scope if isinstance(scope, str) else "",
    )
