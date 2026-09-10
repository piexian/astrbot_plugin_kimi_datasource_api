"""与本机 kimi-code CLI 凭证文件的共存层。

本插件的账号池是从 `kimi import-local` 导入的副本，而 refresh 会轮换 refresh_token：
不处理就会和同机的 kimi-code CLI 互相吊销（CLI 遇到 invalid_grant 会直接删掉凭证文件）。
这里提供三件事：读回、原子回写、以及与官方 `proper-lockfile` 兼容的跨进程刷新锁。
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import time
from pathlib import Path
from typing import Any

from .models import TokenInfo, token_from_credentials

# 与官方 oauth-manager 一致：stale 5s，重试 120 次、500~1000ms 退避
LOCK_STALE_SECONDS = 5.0
LOCK_RETRIES = 120
LOCK_MIN_INTERVAL = 0.5
LOCK_MAX_INTERVAL = 1.0
# proper-lockfile 以 stale/2 周期刷新锁 mtime，取 2s 留余量
LOCK_TOUCH_INTERVAL_SECONDS = 2.0



def parse_local_tokens(path: Path) -> TokenInfo | None:
    """读取 CLI 凭证文件；缺失/损坏一律按“无凭证”处理，与官方 storage.load 同语义。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return token_from_credentials(data)


def local_credentials_file(credentials: dict[str, Any]) -> Path | None:
    """账号记录的本机凭证路径；路径失效（换机器/未导入）时按无来源处理。"""
    raw = str(credentials.get("local_credentials_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def local_refresh_token(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("refresh_token")
    return value if isinstance(value, str) else ""


def write_local_tokens(path: Path, token: TokenInfo) -> bool:
    """原子回写（tmp + fsync + rename, 0600）。仅更新凭证字段，保留文件里的其他键。"""
    try:
        existing: dict[str, Any] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
    except (OSError, json.JSONDecodeError):
        existing = {}

    payload = dict(existing)
    payload.update(
        {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "expires_in": token.expires_in,
            "scope": token.scope,
            "token_type": token.token_type,
        }
    )
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def lock_dir_for(credential_path: Path) -> Path | None:
    """官方 lock 目标为 `{home}/oauth/{name}`，proper-lockfile 实际创建 `{...}.lock` 目录。"""
    name = credential_path.stem
    if not name or name.startswith("."):
        return None
    home = credential_path.parent.parent
    if home == credential_path.parent:
        return None
    return home / "oauth" / f"{name}.lock"


class CredentialRefreshLock:
    """与 kimi-code CLI 互斥的刷新锁；Windows 与官方一样跳过。"""

    def __init__(self, credential_path: Path) -> None:
        self.path = lock_dir_for(credential_path)
        self._acquired = False
        self._touch_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None and os.name != "nt"

    async def acquire(self) -> bool:
        if not self.enabled or self.path is None:
            return True
        if await asyncio.to_thread(self._try_acquire):
            self._on_acquired()
            return True
        deadline = time.monotonic() + LOCK_RETRIES * LOCK_MAX_INTERVAL
        interval = LOCK_MIN_INTERVAL
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            interval = min(LOCK_MAX_INTERVAL, interval * 1.5)
            if self._is_stale():
                await asyncio.to_thread(self._steal_stale)
            if await asyncio.to_thread(self._try_acquire):
                self._on_acquired()
                return True
        return False

    def _on_acquired(self) -> None:
        self._acquired = True
        # proper-lockfile 持有者会持续 touch 锁目录 mtime；不跟进会被 CLI 以 stale(5s) 抢锁
        self._touch_task = asyncio.create_task(self._touch_loop())

    async def _touch_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(LOCK_TOUCH_INTERVAL_SECONDS)
                if self.path is not None:
                    await asyncio.to_thread(self._touch_once)
        except asyncio.CancelledError:
            pass

    def _touch_once(self) -> None:
        if self.path is None:
            return
        try:
            os.utime(self.path)
        except OSError:
            pass

    async def release(self) -> None:
        task, self._touch_task = self._touch_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not self._acquired or self.path is None:
            return
        self._acquired = False
        try:
            await asyncio.to_thread(self.path.rmdir)
        except OSError:
            pass

    def _try_acquire(self) -> bool:
        if self.path is None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.mkdir()
            return True
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.EACCES, errno.EPERM):
                return False
            raise

    def _is_stale(self) -> bool:
        if self.path is None:
            return False
        try:
            return (time.time() - self.path.stat().st_mtime) > LOCK_STALE_SECONDS
        except OSError:
            return False

    def _steal_stale(self) -> None:
        if self.path is None:
            return
        try:
            self.path.rmdir()
        except OSError:
            pass
