from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .constants import (
    DEFAULT_CLIENT_ID,
    DEFAULT_OAUTH_HOST,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    KIMI_DATASOURCE_VERSION,
)
from .identity import oauth_device_headers
from .models import (
    DeviceAuthorization,
    DevicePollResult,
    OAuthError,
    OAuthUnauthorizedError,
    TokenInfo,
    token_from_credentials,
    token_from_oauth_payload,
)
from .storage import KimiCredentialStore

RETRYABLE_REFRESH_STATUSES = {429, 500, 502, 503, 504}
MIN_REFRESH_THRESHOLD_SECONDS = 300
REFRESH_THRESHOLD_RATIO = 0.5


class KimiOAuthClient:
    def __init__(
        self,
        store: KimiCredentialStore,
        *,
        oauth_host: str = DEFAULT_OAUTH_HOST,
        client_id: str = DEFAULT_CLIENT_ID,
        version: str = KIMI_DATASOURCE_VERSION,
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        proxy: str = "",
        max_refresh_retries: int = 3,
    ) -> None:
        self.store = store
        self.oauth_host = oauth_host.rstrip("/")
        self.client_id = client_id
        self.version = version
        self.timeout_seconds = timeout_seconds
        self.proxy = proxy.strip() or None
        self.max_refresh_retries = max(1, max_refresh_retries)
        self._refresh_lock = asyncio.Lock()

    async def request_device_authorization(self) -> DeviceAuthorization:
        status, data = await self._post_form(
            "/api/oauth/device_authorization",
            {"client_id": self.client_id},
        )
        if status != 200:
            raise OAuthError(f"Device authorization failed (HTTP {status}): {pick_error_detail(data)}")

        user_code = data.get("user_code")
        device_code = data.get("device_code")
        verification_uri_complete = data.get("verification_uri_complete")
        if not isinstance(user_code, str) or not user_code:
            raise OAuthError("Device authorization response missing user_code")
        if not isinstance(device_code, str) or not device_code:
            raise OAuthError("Device authorization response missing device_code")
        if not isinstance(verification_uri_complete, str) or not verification_uri_complete:
            raise OAuthError("Device authorization response missing verification_uri_complete")

        verification_uri = data.get("verification_uri")
        expires_in = to_optional_int(data.get("expires_in"))
        interval = to_optional_int(data.get("interval")) or DEFAULT_POLL_INTERVAL_SECONDS
        return DeviceAuthorization(
            user_code=user_code,
            device_code=device_code,
            verification_uri=verification_uri if isinstance(verification_uri, str) else "",
            verification_uri_complete=verification_uri_complete,
            expires_in=expires_in,
            interval=interval,
        )

    async def poll_device_token(self, device_code: str) -> DevicePollResult:
        status, data = await self._post_form(
            "/api/oauth/token",
            {
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if status == 200 and isinstance(data.get("access_token"), str):
            return DevicePollResult("success", token=token_from_oauth_payload(data, now_seconds()))
        if status >= 500:
            raise OAuthError(f"Device token polling server error (HTTP {status}): {pick_error_detail(data)}")

        error_code = data.get("error")
        error_code = error_code if isinstance(error_code, str) else "unknown_error"
        description = data.get("error_description")
        description = description if isinstance(description, str) else pick_error_detail(data)
        if error_code in {"authorization_pending", "slow_down"}:
            return DevicePollResult("pending", error_code=error_code, description=description)
        if error_code == "expired_token":
            return DevicePollResult("expired")
        if error_code == "access_denied":
            return DevicePollResult("denied", description=description)
        raise OAuthError(f"Device token polling failed (HTTP {status}): {error_code} {description}".strip())

    async def refresh_access_token(self, refresh_token: str) -> TokenInfo:
        last_error: Exception | None = None
        for attempt in range(self.max_refresh_retries):
            try:
                status, data = await self._post_form(
                    "/api/oauth/token",
                    {
                        "client_id": self.client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                )
            except OAuthError as exc:
                last_error = exc
                if attempt < self.max_refresh_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = OAuthError(f"OAuth refresh request failed: {exc}")
                if attempt < self.max_refresh_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise last_error from exc

            if status == 200 and isinstance(data.get("access_token"), str):
                return token_from_oauth_payload(data, now_seconds())

            error_code = data.get("error")
            if status in {401, 403} or error_code == "invalid_grant":
                raise OAuthUnauthorizedError(pick_error_detail(data) or "Token refresh unauthorized.")
            if status in RETRYABLE_REFRESH_STATUSES and attempt < self.max_refresh_retries - 1:
                last_error = OAuthError(pick_error_detail(data) or f"Token refresh failed (HTTP {status}).")
                await asyncio.sleep(2**attempt)
                continue
            raise OAuthError(pick_error_detail(data) or f"Token refresh failed (HTTP {status}).")

        raise OAuthError(str(last_error or "Token refresh failed."))

    async def ensure_fresh(self, account_id: str | None = None, *, force: bool = False) -> str:
        account_id, token = await self._load_account_token(account_id)
        if token is None:
            credentials = await self.store.load_credentials(account_id)
            if credentials and credentials.get("status") == "revoked":
                raise OAuthUnauthorizedError(f"Kimi account {account_id} was rejected; re-login required.")
            raise OAuthError(f"No Kimi token stored for account {account_id}. Ask an administrator to run kimi login.")
        if not self._should_refresh(token, force):
            return token.access_token

        async with self._refresh_lock:
            account_id, token = await self._load_account_token(account_id)
            if token is None:
                raise OAuthUnauthorizedError(f"Kimi account {account_id} is missing or revoked; re-login required.")
            if not self._should_refresh(token, force):
                return token.access_token
            if not token.refresh_token:
                raise OAuthError(f"Kimi account {account_id} has no refresh_token; re-login required.")

            try:
                refreshed = await self.refresh_access_token(token.refresh_token)
                device_id = await self.store.get_device_id()
                await self.store.save_refreshed_token(account_id, refreshed, device_id=device_id)
                return refreshed.access_token
            except OAuthUnauthorizedError:
                recovery = await self.store.load_token(account_id)
                if recovery and recovery.refresh_token != token.refresh_token:
                    return recovery.access_token
                await self.store.mark_revoked(account_id)
                raise

    async def _load_account_token(self, account_id: str | None) -> tuple[str, TokenInfo | None]:
        credentials = await self.store.load_credentials(account_id)
        if not credentials:
            if account_id:
                return account_id, None
            ids = await self.store.list_account_ids(include_revoked=False)
            return (ids[0] if ids else "default"), None
        selected_id = str(credentials.get("account_id") or account_id or "")
        return selected_id, token_from_credentials(credentials)

    def _should_refresh(self, token: TokenInfo, force: bool) -> bool:
        if force:
            return True
        if token.expires_at == 0:
            return False
        threshold = max(MIN_REFRESH_THRESHOLD_SECONDS, token.expires_in * REFRESH_THRESHOLD_RATIO)
        return token.expires_at - now_seconds() < threshold

    async def _post_form(self, path: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        device_id = await self.store.get_device_id()
        headers = {
            **oauth_device_headers(device_id, self.version),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        body = urlencode(params)
        timeout = aiohttp.ClientTimeout(total=max(1, self.timeout_seconds))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.oauth_host}{path}",
                    data=body,
                    headers=headers,
                    proxy=self.proxy,
                ) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except Exception:
                        payload = {}
                    return response.status, payload if isinstance(payload, dict) else {}
        except asyncio.TimeoutError:
            raise OAuthError(f"OAuth request timed out after {self.timeout_seconds} seconds.") from None
        except aiohttp.ClientError as exc:
            raise OAuthError(f"OAuth request failed: {exc}") from exc


def pick_error_detail(data: dict[str, Any]) -> str:
    for key in ("message", "error_description", "error"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    detail = data.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return "unknown"


def to_optional_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def now_seconds() -> int:
    return int(time.time())
