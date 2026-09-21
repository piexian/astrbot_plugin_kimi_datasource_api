from __future__ import annotations

import json
import re
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


def safe_error_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """限制错误长度并移除凭据，避免上游回显进入聊天或日志。"""
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"\beyJ[A-Za-z0-9_.-]+", "[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", value)
    value = re.sub(
        r"(?i)((?:access_token|refresh_token|device_code|user_code|api_key)[\\\"']*\s*[:=]\s*[\\\"']*)[^\\\s,\"'&}]+",
        r"\1[REDACTED]",
        value,
    )
    value = " ".join(value.split())
    return value[:600] + ("…" if len(value) > 600 else "")


def api_error_fields(body: str) -> tuple[str, str]:
    """提取官方错误类型与说明，兼容非 JSON 网关错误。"""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return "", body or "empty response"
    if not isinstance(payload, dict):
        return "", body or "empty response"
    error = payload.get("error")
    source = error if isinstance(error, dict) else payload
    error_type = source.get("type") or source.get("code")
    detail = next(
        (source[key] for key in ("message", "error_description", "detail")
         if isinstance(source.get(key), str) and source[key].strip()),
        error if isinstance(error, str) else "request failed",
    )
    return error_type if isinstance(error_type, str) else "", detail


class DatasourceHTTPError(DatasourceError):
    def __init__(self, status: int, body: str, *, secrets: tuple[str, ...] = ()) -> None:
        self.status = status
        self.body = safe_error_text(body, secrets=secrets)
        error_type, detail = api_error_fields(body)
        self.error_type = safe_error_text(error_type, secrets=secrets)[:80]
        self.detail = safe_error_text(detail, secrets=secrets)
        label = f" ({self.error_type})" if self.error_type else ""
        self.is_monthly_quota = (
            status == 403
            and self.error_type == "access_terminated_error"
            and "monthly usage limit" in detail.lower()
        )
        hint = ""
        if self.is_monthly_quota:
            hint = "本计费周期月度额度已用尽，重新登录无法恢复额度。"
        super().__init__(f"HTTP {status}{label}: {hint}{self.detail}")


class QuotaCooldownError(KimiPluginError):
    """账号月度额度冷却，不属于登录失效或抓取降级。"""


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
