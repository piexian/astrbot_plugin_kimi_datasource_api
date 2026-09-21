"""查询并展示 Kimi Code 官方额度窗口。"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from typing import Any

import aiohttp

from .constants import DEFAULT_KIMI_CODE_BASE_URL, DEFAULT_REQUEST_TIMEOUT_SECONDS
from .identity import moonshot_headers
from .models import DatasourceError, DatasourceHTTPError
from .oauth import KimiOAuthClient
from .storage import KimiCredentialStore

QUOTA_WINDOWS = (
    ("limit_5h", "5 小时额度"),
    ("limit_7d", "7 天额度"),
    ("limit_month_total", "月度总额度"),
    ("limit_month_code", "月度代码额度"),
)


class KimiUsageClient:
    def __init__(
        self,
        store: KimiCredentialStore,
        oauth: KimiOAuthClient,
        *,
        base_url: str = DEFAULT_KIMI_CODE_BASE_URL,
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        proxy: str = "",
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.url = f"{base_url.rstrip('/')}/usages"
        self.timeout_seconds = max(1, min(10, timeout_seconds))
        self.proxy = proxy.strip() or None

    async def get_usage(self, account_id: str) -> dict[str, Any]:
        for force in (False, True):
            token = await self.oauth.ensure_fresh(account_id, force=force)
            device_id = await self.store.get_device_id(account_id)
            headers = moonshot_headers(token, device_id, accept="application/json")
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        self.url, headers=headers, proxy=self.proxy, allow_redirects=False
                    ) as response:
                        body = await response.text()
                        if response.status == 401 and not force:
                            continue
                        if response.status != 200:
                            raise DatasourceHTTPError(response.status, body, secrets=(token,))
            except asyncio.TimeoutError:
                raise DatasourceError(f"额度查询超时（{self.timeout_seconds} 秒）") from None
            except aiohttp.ClientError as exc:
                raise DatasourceError(f"额度查询网络异常：{type(exc).__name__}") from exc
            try:
                data = json.loads(body)
            except ValueError:
                raise DatasourceError("额度接口返回了无效 JSON") from None
            if not isinstance(data, dict):
                raise DatasourceError("额度接口返回了无效数据结构")
            return data
        raise DatasourceError("额度查询失败")


def format_usage(data: dict[str, Any]) -> list[str]:
    usages = data.get("usages")
    usages = usages if isinstance(usages, dict) else {}
    lines = []
    missing = False
    for key, label in QUOTA_WINDOWS:
        entry = usages.get(key)
        entry = entry if isinstance(entry, dict) else {}
        ratio = used_ratio(entry.get("used_ratio"))
        if ratio is None:
            lines.append(f"{label}：未知（接口未提供有效用量）")
            missing = True
            continue
        reset = format_reset_time(entry.get("reset_time"))
        lines.append(
            f"{label}：已用 {ratio * 100:.1f}%，剩余 {max(0, 1 - ratio) * 100:.1f}%；重置 {reset}"
        )
    if missing:
        lines.append("未返回的额度不代表仍可用；实际调用可能另受月度或套餐限制。")
    return lines


def used_ratio(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        ratio = float(value)
    except (ValueError, OverflowError):
        return None
    return ratio if math.isfinite(ratio) and ratio >= 0 and math.isfinite(ratio * 100) else None


def format_reset_time(value: Any) -> str:
    if not isinstance(value, str):
        return "未知"
    try:
        reset = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if reset.tzinfo is None:
            return "未知"
        return reset.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (ValueError, OverflowError, OSError):
        return "未知"
