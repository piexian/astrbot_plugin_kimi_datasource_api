"""月度额度拒绝的持久化冷却与单次按需复核。"""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from .models import DatasourceHTTPError, KimiPluginError, OAuthUnauthorizedError, QuotaCooldownError, safe_error_text
from .monthly import describe_monthly_reset, reset_probe_boundary, validate_monthly_reset

MONTHLY_COOLDOWNS_KEY = "kimi_code.monthly_cooldowns"
DEFAULT_COOLDOWN_MINUTES = 60


class KimiQuotaCooldown:
    def __init__(self, owner: Any, *, minutes: int = DEFAULT_COOLDOWN_MINUTES, clock: Callable[[], float] | None = None, reset_getter=None, success_callback=None):
        self.owner = owner
        self.seconds = max(1, min(1440, minutes)) * 60
        self.clock = clock or time.time
        self._lock = asyncio.Lock()
        self._inflight: set[str] = set()
        self.reset_getter = reset_getter
        self.success_callback = success_callback

    async def _load(self) -> dict[str, Any]:
        data = await self.owner.get_kv_data(MONTHLY_COOLDOWNS_KEY, {})
        return dict(data) if isinstance(data, dict) else {}

    @staticmethod
    def _record(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        retry_at = value.get("retry_at")
        if isinstance(retry_at, bool) or not isinstance(retry_at, (int, float)):
            return None
        if not 0 < retry_at < 32503680000 or not math.isfinite(retry_at):
            return None
        return value.copy()

    async def get(self, account_id: str) -> dict[str, Any] | None:
        async with self._lock:
            states = await self._load()
            record = self._record(states.get(account_id))
            if record is not None:
                await self._sync_binding(account_id, states, record)
            return record

    async def _sync_binding(self, account_id: str, states: dict, record: dict) -> None:
        if self.reset_getter is None:
            return
        binding = await self.reset_getter(account_id)
        if record.get("monthly_reset") == binding:
            return
        boundary = reset_probe_boundary(binding)
        now = self.clock()
        record.update(
            monthly_reset=binding, generation=str(uuid4()),
            retry_at=boundary if boundary is not None and boundary > now else now + self.seconds,
        )
        states[account_id] = record
        await self.owner.put_kv_data(MONTHLY_COOLDOWNS_KEY, states)

    def describe(self, account_id: str, record: dict[str, Any]) -> str:
        retry_at = record["retry_at"]
        retry_text = datetime.fromtimestamp(retry_at).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        state = "冷却中" if retry_at > self.clock() else "待复核"
        binding = record.get("monthly_reset")
        reset_text = describe_monthly_reset(binding) if binding is not None else "官方月度重置时间：未提供"
        return (
            f"账号 {account_id} 月度额度{state}：最近一次拒绝为本计费周期月度额度已用尽。"
            f"{reset_text}；下次允许复核：{retry_text}（复核边界，非额度重置时间，不代表额度已恢复）。"
            "5 小时/周额度有余量也不会解除；到期后下一次业务调用单次复核，重新登录不会重置月度额度。"
        )

    async def _admit(self, account_id: str) -> str | None:
        async with self._lock:
            states = await self._load()
            record = self._record(states.get(account_id))
            if record is None:
                return None
            await self._sync_binding(account_id, states, record)
            if account_id in self._inflight:
                raise QuotaCooldownError(self.describe(account_id, record) + " 当前已有一次复核正在进行。")
            now = self.clock()
            if record["retry_at"] > now:
                raise QuotaCooldownError(self.describe(account_id, record))
            # 先持久化下一次复核时间，阻止并发调用和重载后重复探测。
            generation = str(uuid4())
            record.update(retry_at=now + self.seconds, last_probe_at=now, generation=generation)
            states[account_id] = record
            await self.owner.put_kv_data(MONTHLY_COOLDOWNS_KEY, states)
            self._inflight.add(account_id)
            return generation

    async def block(self, account_id: str, error: DatasourceHTTPError) -> dict[str, Any]:
        async with self._lock:
            binding = await self.reset_getter(account_id) if self.reset_getter else None
            boundary = reset_probe_boundary(binding)
            states = await self._load()
            now = self.clock()
            record = {
                "reason": "monthly_quota",
                "detected_at": now,
                "retry_at": boundary if boundary is not None and boundary > now else now + self.seconds,
                "monthly_reset": binding,
                "generation": str(uuid4()),
                "last_error": str(error),
            }
            states[account_id] = record
            await self.owner.put_kv_data(MONTHLY_COOLDOWNS_KEY, states)
            return record

    async def rebind(self, account_id: str, rule: dict | None) -> None:
        binding = validate_monthly_reset(rule)
        boundary = reset_probe_boundary(binding)
        async with self._lock:
            if self.reset_getter is not None:
                binding = await self.reset_getter(account_id)
                boundary = reset_probe_boundary(binding)
            states = await self._load()
            record = self._record(states.get(account_id))
            if record is None:
                return
            now = self.clock()
            record.update(
                monthly_reset=binding,
                retry_at=boundary if boundary is not None and boundary > now else now + self.seconds,
                generation=str(uuid4()),
            )
            states[account_id] = record
            await self.owner.put_kv_data(MONTHLY_COOLDOWNS_KEY, states)


    async def _confirm_success(self, account_id: str, generation: str) -> bool:
        async with self._lock:
            states = await self._load()
            record = self._record(states.get(account_id))
            # 较早发出的成功请求不能抹掉较新的月度拒绝。
            if record is not None and record.get("generation") == generation:
                states.pop(account_id, None)
                await self.owner.put_kv_data(MONTHLY_COOLDOWNS_KEY, states)
                return True
            return False

    @asynccontextmanager
    async def request(self, account_id: str):
        generation = await self._admit(account_id)
        try:
            yield
        except OAuthUnauthorizedError:
            raise
        except KimiPluginError as exc:
            if isinstance(exc, DatasourceHTTPError) and exc.is_monthly_quota:
                record = await self.block(account_id, exc)
                raise QuotaCooldownError(self.describe(account_id, record) + f" 最近错误：{exc}") from None
            if generation is not None:
                record = await self.get(account_id)
                if record is not None:
                    raise QuotaCooldownError(
                        self.describe(account_id, record) + f" 本次复核失败：{safe_error_text(str(exc))}"
                    ) from None
            raise
        else:
            if generation is not None and await self._confirm_success(account_id, generation):
                # 普通请求成功可能仍在消费本期余量，只有冷却复核成功才推进周期。
                if self.success_callback is not None and await self.get(account_id) is None:
                    await self.success_callback(account_id)
        finally:
            if generation is not None:
                self._inflight.discard(account_id)

    async def forget(self, account_ids: list[str] | None = None) -> None:
        async with self._lock:
            if account_ids is None:
                await self.owner.delete_kv_data(MONTHLY_COOLDOWNS_KEY)
                return
            states = await self._load()
            for account_id in account_ids:
                states.pop(account_id, None)
            await self.owner.put_kv_data(MONTHLY_COOLDOWNS_KEY, states)
