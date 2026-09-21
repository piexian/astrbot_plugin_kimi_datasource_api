from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


def stop_controller(controller: Any | None) -> None:
    if controller is not None:
        controller.stop()
        timer_event = getattr(controller, "current_event", None)
        if isinstance(timer_event, asyncio.Event):
            timer_event.set()


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
    initiator_id: str = ""
    monthly_reset: dict[str, Any] | None = None
    ask_monthly_reset: bool = True
    credentials_saved: bool = False
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

    def for_account(self, account_id: str) -> list[PendingLogin]:
        return [pending for pending in self._items.values() if pending.account_id == account_id]

    async def cancel_all(self) -> list[str]:
        tasks: list[asyncio.Task] = []
        account_ids = [pending.account_id for pending in self._items.values()]
        for pending in list(self._items.values()):
            if pending.poll_task and not pending.poll_task.done():
                pending.poll_task.cancel()
                tasks.append(pending.poll_task)
            if pending.waiter_task and not pending.waiter_task.done():
                pending.waiter_task.cancel()
                tasks.append(pending.waiter_task)
            stop_controller(pending.session_controller)
        self._items.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return account_ids
