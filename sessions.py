from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingLogin:
    session_id: str
    account_id: str
    device_code: str
    verification_uri_complete: str
    user_code: str
    started_at: float
    deadline_at: float
    interval: int
    state: str = "requested"
    poll_task: asyncio.Task | None = None
    waiter_task: asyncio.Task | None = None
    session_controller: Any | None = None

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.deadline_at - time.time()))


class PendingLoginRegistry:
    def __init__(self) -> None:
        self._items: dict[str, PendingLogin] = {}

    def get(self, session_id: str) -> PendingLogin | None:
        return self._items.get(session_id)

    def set(self, pending: PendingLogin) -> None:
        self._items[pending.session_id] = pending

    def pop(self, session_id: str) -> PendingLogin | None:
        return self._items.pop(session_id, None)

    def is_current(self, pending: PendingLogin) -> bool:
        return self._items.get(pending.session_id) is pending

    async def cancel_all(self) -> None:
        tasks: list[asyncio.Task] = []
        for pending in list(self._items.values()):
            if pending.poll_task and not pending.poll_task.done():
                pending.poll_task.cancel()
                tasks.append(pending.poll_task)
            if pending.waiter_task and not pending.waiter_task.done():
                pending.waiter_task.cancel()
                tasks.append(pending.waiter_task)
            if pending.session_controller:
                pending.session_controller.stop()
        self._items.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
