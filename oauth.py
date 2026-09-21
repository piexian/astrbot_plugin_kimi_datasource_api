from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

from astrbot.api import logger
from .constants import (
    DEFAULT_CLIENT_ID,
    DEFAULT_OAUTH_HOST,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    KIMI_CODE_CLI_VERSION,
    PLUGIN_NAME,
)
from .identity import oauth_device_headers
from .local_credentials import (
    CredentialRefreshLock,
    local_credentials_file,
    local_refresh_token,
    parse_local_tokens,
    write_local_tokens,
)
from .models import (
    DeviceAuthorization,
    DevicePollResult,
    OAuthError,
    OAuthUnauthorizedError,
    TokenInfo,
    token_from_credentials,
    token_from_oauth_payload,
)
from .monthly import validate_monthly_reset
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
        version: str = KIMI_CODE_CLI_VERSION,
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
        self._request_device_id: ContextVar[str | None] = ContextVar("kimi_oauth_device", default=None)
        self._closing = False

    async def close(self) -> None:
        self._closing = True
        async with self._refresh_lock:
            pass

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
                # 对齐官方：响应没带新 refresh_token 时沿用本次请求里的那个
                payload = dict(data)
                if not isinstance(payload.get("refresh_token"), str) or not payload["refresh_token"]:
                    payload["refresh_token"] = refresh_token
                return token_from_oauth_payload(payload, now_seconds())

            error_code = data.get("error")
            if status in {401, 403} or error_code == "invalid_grant":
                raise OAuthUnauthorizedError(pick_error_detail(data) or "Token refresh unauthorized.")
            if status in RETRYABLE_REFRESH_STATUSES and attempt < self.max_refresh_retries - 1:
                last_error = OAuthError(pick_error_detail(data) or f"Token refresh failed (HTTP {status}).")
                await asyncio.sleep(2**attempt)
                continue
            raise OAuthError(pick_error_detail(data) or f"Token refresh failed (HTTP {status}).")

        raise OAuthError(str(last_error or "Token refresh failed."))

    async def ensure_fresh(
        self, account_id: str | None = None, *, force: bool = False, allow_revoked: bool = False,
        monthly_reset: dict | None = None,
    ) -> str:
        task = asyncio.create_task(self._ensure_fresh(
            account_id, force=force, allow_revoked=allow_revoked, monthly_reset=monthly_reset,
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 刷新会消耗旧 token，取消调用也要等待新凭据落盘。
            await task
            raise

    async def _ensure_fresh(
        self, account_id: str | None = None, *, force: bool = False, allow_revoked: bool = False,
        monthly_reset: dict | None = None,
    ) -> str:
        if self._closing:
            raise OAuthError("插件正在停止，请稍后重试。")
        if monthly_reset is not None:
            if not account_id or not force:
                raise OAuthError("绑定月重置时间需指定账号并显式刷新。")
            monthly_reset = validate_monthly_reset(monthly_reset)
        if allow_revoked and (not account_id or not force):
            raise OAuthError("恢复账号必须显式指定账号 ID 并强制刷新。")
        account_id, token = await self._load_account_token(account_id, allow_revoked=allow_revoked)
        if token is None:
            credentials = await self.store.load_credentials(account_id)
            if credentials and credentials.get("status") == "revoked":
                raise OAuthUnauthorizedError(f"Kimi account {account_id} was rejected; re-login required.")
            raise OAuthError(f"No Kimi token stored for account {account_id}. Ask an administrator to run kimi login.")
        if not self._should_refresh(token, force):
            return token.access_token

        async with self._refresh_lock, self.store.account_guard(account_id):
            if self._closing:
                raise OAuthError("插件正在停止，请稍后重试。")
            account_id, token = await self._load_account_token(account_id, allow_revoked=allow_revoked)
            if token is None:
                raise OAuthUnauthorizedError(f"Kimi account {account_id} is missing or revoked; re-login required.")
            if not self._should_refresh(token, force):
                return token.access_token
            if not token.refresh_token:
                raise OAuthError(f"Kimi account {account_id} has no refresh_token; re-login required.")

            credentials = await self.store.load_credentials(account_id) or {}
            device_id = str(credentials.get("device_id") or await self.store.get_device_id())
            local_path = local_credentials_file(credentials)
            lock: CredentialRefreshLock | None = None
            if local_path is not None:
                # 与同机 kimi-code CLI 抢同一把锁，避免两边互相吊销 refresh_token
                lock = CredentialRefreshLock(local_path)
                if not await lock.acquire():
                    raise OAuthError(f"Kimi 凭证刷新锁被占用，账号 {account_id} 请稍后重试。")
            device_context = self._request_device_id.set(device_id)
            try:
                adopted = await self._adopt_local_token(
                    account_id, local_path, token, persist=not allow_revoked
                )
                if adopted is not None:
                    token = adopted
                    # 显式恢复需先通过服务端刷新，不能仅凭本机副本解除 revoked。
                    if not allow_revoked and (force or not self._should_refresh(token, force)):
                        if monthly_reset is not None:
                            await self.store.set_monthly_reset(account_id, monthly_reset)
                        return token.access_token
                try:
                    refreshed = await self.refresh_access_token(token.refresh_token)
                except OAuthUnauthorizedError:
                    recovery = await self.store.load_token(account_id)
                    if recovery and recovery.refresh_token != token.refresh_token:
                        if monthly_reset is not None:
                            await self.store.set_monthly_reset(account_id, monthly_reset)
                        return recovery.access_token
                    recovery = await self._adopt_local_token(
                        account_id, local_path, token, persist=not allow_revoked
                    )
                    if not allow_revoked and recovery is not None and recovery.expires_at > now_seconds():
                        if monthly_reset is not None:
                            await self.store.set_monthly_reset(account_id, monthly_reset)
                        return recovery.access_token
                    await self.store.mark_revoked(account_id)
                    raise
                try:
                    await self.store.save_refreshed_token(
                        account_id, refreshed, device_id=device_id, monthly_reset=monthly_reset
                    )
                finally:
                    # 即使受管文件暂时不可写，也尽力保全已轮换的 CLI 凭据。
                    if local_path is not None and local_refresh_token(local_path) == token.refresh_token:
                        if not write_local_tokens(local_path, refreshed):
                            logger.warning(
                                f"[{PLUGIN_NAME}] 回写本机凭证 {local_path} 失败，同机 kimi-code CLI 登录态可能失效"
                            )
                return refreshed.access_token
            finally:
                self._request_device_id.reset(device_context)
                if lock is not None:
                    await lock.release()

    async def _adopt_local_token(
        self, account_id: str, local_path: Path | None, token: TokenInfo, *, persist: bool = True
    ) -> TokenInfo | None:
        """本机 CLI 已轮换过凭证时采纳它，避免用掉被吊销的 refresh_token。"""
        if local_path is None:
            return None
        local_token = parse_local_tokens(local_path)
        if local_token is None or local_token.refresh_token == token.refresh_token:
            return None
        if persist:
            device_id = await self.store.get_device_id(account_id)
            await self.store.save_refreshed_token(account_id, local_token, device_id=device_id)
        return local_token

    async def _load_account_token(
        self, account_id: str | None, *, allow_revoked: bool = False
    ) -> tuple[str, TokenInfo | None]:
        credentials = await self.store.load_credentials(account_id)
        if not credentials:
            if account_id:
                return account_id, None
            ids = await self.store.list_account_ids(include_revoked=False)
            return (ids[0] if ids else "default"), None
        selected_id = str(credentials.get("account_id") or account_id or "")
        if allow_revoked:
            credentials = {**credentials, "status": "valid"}
        return selected_id, token_from_credentials(credentials)

    def _should_refresh(self, token: TokenInfo, force: bool) -> bool:
        if force:
            return True
        if token.expires_at == 0:
            return False
        threshold = max(MIN_REFRESH_THRESHOLD_SECONDS, token.expires_in * REFRESH_THRESHOLD_RATIO)
        return token.expires_at - now_seconds() < threshold

    async def _post_form(self, path: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        device_id = self._request_device_id.get() or await self.store.get_device_id()
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
