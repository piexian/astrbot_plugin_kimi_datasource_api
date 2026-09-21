from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .cooldown import DEFAULT_COOLDOWN_MINUTES, KimiQuotaCooldown
from .credential_files import AsyncRLock, CredentialFileError, CredentialFiles, credential_document, file_io, fingerprint
from .identity import new_device_id
from .models import KimiPluginError, TokenInfo, token_from_credentials
from .monthly import advance_monthly_reset, validate_monthly_reset

DEVICE_ID_KEY = "kimi_code.device_id"
ACCOUNTS_KEY = "kimi_code.accounts"
ACCOUNT_CONFIG_SNAPSHOT_KEY = "kimi_code.account_config_snapshot"
ROTATION_CURSOR_KEY = "kimi_code.rotation_cursor"
FILE_STORE_KEY = "kimi_code.file_store"
ACCOUNT_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class KVOwner(Protocol):
    async def put_kv_data(self, key: str, value: Any) -> None: ...
    async def get_kv_data(self, key: str, default: Any) -> Any: ...
    async def delete_kv_data(self, key: str) -> None: ...


class KimiCredentialStore:
    def __init__(self, owner: KVOwner, *, cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES, credential_root: Path | None = None, environment: dict | None = None) -> None:
        self.owner = owner
        self.files = CredentialFiles(credential_root, environment=environment) if credential_root is not None else None
        self._closed = False
        self.file_ready = False
        self.file_errors: dict[str, str] = {}
        self._active_paths: list[str] = []
        self._managed_paths: dict[str, str] = {}
        self._pending_updates: dict[str, dict] = {}
        self._lock = AsyncRLock()
        self._account_locks: dict[str, AsyncRLock] = {}
        self._reserved: set[str] = set()
        self.cooldown = KimiQuotaCooldown(
            owner, minutes=cooldown_minutes, reset_getter=self.get_monthly_reset,
            success_callback=self.advance_monthly_reset,
        )

    def _check_open(self) -> None:
        if self._closed:
            raise CredentialFileError("凭据存储正在停止，请稍后重试。")

    async def close(self) -> None:
        async with self._lock:
            for account_id in list(self._pending_updates):
                await self._read_managed(account_id)
            if self.file_ready:
                await self._save_index()
            self._closed = True


    def account_guard(self, account_id: str) -> AsyncRLock:
        return self._account_locks.setdefault(normalize_account_id(account_id), AsyncRLock())

    async def get_device_id(self, account_id: str | None = None) -> str:
        if account_id is not None:
            credentials = await self.load_credentials(account_id)
            if credentials and isinstance(credentials.get("device_id"), str) and credentials["device_id"].strip():
                return credentials["device_id"].strip()
        async with self._lock:
            existing = await self.owner.get_kv_data(DEVICE_ID_KEY, None)
            if isinstance(existing, str) and existing.strip():
                return existing.strip()
            device_id = new_device_id()
            await self.owner.put_kv_data(DEVICE_ID_KEY, device_id)
            return device_id

    async def save_device_id(self, device_id: str) -> None:
        async with self._lock:
            if cleaned := str(device_id).strip():
                await self.owner.put_kv_data(DEVICE_ID_KEY, cleaned)

    async def initialize_files(self, paths: list[str], legacy_ids: list[str], save_paths) -> None:
        """先准备文件，再提交配置与存储模式，最后移除旧 KV token。"""
        if self.files is None:
            return
        async with self._lock:
            self._check_open()
            state = await self.owner.get_kv_data(FILE_STORE_KEY, None)
            if self.file_ready:
                await self._finish_migration(state or {})
                self._active_paths = self._clean_paths(paths)
                return
            if state is not None and (not isinstance(state, dict) or state.get("version") != 1):
                raise CredentialFileError("凭据迁移状态无效，未启用旧凭据回退。")
            if state and state.get("phase") == "files":
                self._managed_paths = dict(state.get("files", {}))
                self._active_paths = self._clean_paths(paths)
                self.file_ready = True
                await self._finish_migration(state)
                return
            if state and state.get("phase") != "prepared":
                raise CredentialFileError("无法识别凭据迁移阶段。")
            legacy = copy.deepcopy(await self.owner.get_kv_data(ACCOUNTS_KEY, {}))
            if not isinstance(legacy, dict):
                raise CredentialFileError("旧 KV 凭据结构无效，迁移已暂停。")
            normalized = normalize_accounts(legacy)
            if len(normalized) != len(legacy):
                raise CredentialFileError("旧凭据包含无效或冲突账号 ID，迁移已暂停。")
            snapshot = await self.load_config_snapshot()
            removed = set(snapshot or []) - set(legacy_ids) if snapshot is not None else set()
            documents = {}
            device_id = await self.get_device_id()
            for account_id, data in normalized.items():
                if account_id in removed:
                    continue
                documents[account_id] = credential_document(account_id, {"device_id": device_id, "environment": self.files.environment, **data})
            managed = {account_id: self.files.filename(account_id) for account_id in documents}
            previous_hashes = state.get("hashes", {}) if state else {}
            hashes = {}
            # 预备记录仅存路径和摘要；已有独立改动的文件绝不覆盖。
            bases: dict[str, str | None] = {}
            for account_id, relative in managed.items():
                expected = fingerprint(documents[account_id])
                acceptable = [expected, *previous_hashes.get(relative, [])]
                bases[relative] = None
                existing = await file_io(self.files.read_optional, relative)
                if existing is not None:
                    actual = fingerprint(existing)
                    if actual != expected and actual not in previous_hashes.get(relative, []):
                        raise CredentialFileError(f"迁移目标已有不同凭据：{relative}，未覆盖。")
                    acceptable.append(actual)
                    bases[relative] = actual
                hashes[relative] = list(set(acceptable))
            prepared = {"version": 1, "phase": "prepared", "files": managed, "hashes": hashes}
            await self.owner.put_kv_data(FILE_STORE_KEY, prepared)
            for account_id, relative in managed.items():
                await file_io(self.files.write, relative, account_id, documents[account_id], expected=bases[relative])
                if await file_io(self.files.read, relative) != documents[account_id]:
                    raise CredentialFileError("迁移回读校验失败，旧 KV 已保留。")
            if await self.owner.get_kv_data(ACCOUNTS_KEY, {}) != legacy:
                raise CredentialFileError("迁移期间旧凭据发生变更，请停止旧调用后重试。")
            merged_paths = list(dict.fromkeys([*self._clean_paths(paths), *managed.values()]))
            await save_paths(merged_paths)
            committed = {"version": 1, "phase": "files", "files": managed, "cleanup_pending": True}
            await self.owner.put_kv_data(FILE_STORE_KEY, committed)
            self._managed_paths, self._active_paths = managed, merged_paths
            self.file_ready = True
            await self._finish_migration(committed)

    async def _finish_migration(self, state: dict) -> None:
        if state.get("cleanup_pending"):
            await self.owner.delete_kv_data(ACCOUNTS_KEY)
            await self.owner.delete_kv_data(ACCOUNT_CONFIG_SNAPSHOT_KEY)
            await self.owner.put_kv_data(FILE_STORE_KEY, {**state, "cleanup_pending": False})
        elif await self.owner.get_kv_data(ACCOUNTS_KEY, None):
            raise CredentialFileError("检测到旧版仍在写入 KV 凭据，请停止旧调用后处理；未使用旧 token。")

    @staticmethod
    def _clean_paths(paths: list[str]) -> list[str]:
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise CredentialFileError("凭据配置必须是文件路径列表。")
        return list(dict.fromkeys(paths))

    async def _save_index(self) -> None:
        previous = await self.owner.get_kv_data(FILE_STORE_KEY, {})
        await self.owner.put_kv_data(FILE_STORE_KEY, {
            "version": 1, "phase": "files", "files": self._managed_paths.copy(),
            "cleanup_pending": bool(previous.get("cleanup_pending", False)),
        })

    def credential_path(self, account_id: str) -> str:
        return self._managed_paths.get(normalize_account_id(account_id), "")

    def active_file_paths(self) -> list[str]:
        return self._active_paths.copy()

    def inactive_files(self) -> dict[str, str]:
        return {key: path for key, path in self._managed_paths.items() if path not in self._active_paths}


    async def _read_managed(self, account_id: str) -> dict | None:
        self._check_open()
        relative = self._managed_paths.get(account_id)
        if not relative or self.files is None:
            return None
        pending = self._pending_updates.get(account_id)
        if pending is not None:
            current = await file_io(self.files.read_optional, relative)
            if current != pending["document"]:
                await file_io(self.files.write, relative, account_id, pending["document"], expected=pending["base_fingerprint"])
            self._pending_updates.pop(account_id, None)
        document = await file_io(self.files.read, relative)
        if document["account_id"] != account_id:
            raise CredentialFileError("凭据文件的账号 ID 与索引不一致。")
        return {**document, "_credential_file": relative, "_source_fingerprint": fingerprint(document)}

    async def list_accounts(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            self._check_open()
            if not self.file_ready:
                data = await self.owner.get_kv_data(ACCOUNTS_KEY, {})
                return normalize_accounts(data if isinstance(data, dict) else {})
            assert self.files is not None
            accounts, self.file_errors = {}, {}
            previous_index = self._managed_paths.copy()
            seen = {}
            for relative in self._active_paths:
                try:
                    document = await file_io(self.files.read, relative)
                    account_id = document["account_id"]
                    if account_id in seen:
                        self.file_errors[seen[account_id]] = "账号 ID 重复，已停用。"
                        accounts.pop(account_id, None)
                        raise CredentialFileError("账号 ID 重复，已停用。")
                    seen[account_id] = relative
                    known = self._managed_paths.get(account_id)
                    if known and known != relative:
                        raise CredentialFileError("该账号已关联其他文件，请先明确退出旧账号。")
                    self._managed_paths[account_id] = relative
                    accounts[account_id] = await self._read_managed(account_id)
                except KimiPluginError as exc:
                    self.file_errors[relative] = str(exc)
            if self._managed_paths != previous_index:
                await self._save_index()
            return accounts

    async def list_account_ids(self, *, include_revoked: bool = True) -> list[str]:
        accounts = await self.list_accounts()
        if not accounts and self.file_errors:
            raise CredentialFileError("凭据文件不可用：" + "; ".join(self.file_errors.values()))
        return [key for key, value in accounts.items() if include_revoked or value.get("status") != "revoked"]

    async def load_credentials(self, account_id: str | None = None) -> dict[str, Any] | None:
        accounts = await self.list_accounts()
        if account_id is not None:
            account_id = normalize_account_id(account_id)
            relative = self._managed_paths.get(account_id)
            if self.file_ready and relative in self.file_errors:
                raise CredentialFileError(self.file_errors[relative])
            selected = accounts.get(account_id)
            return selected.copy() if selected else None
        ids = [key for key, value in accounts.items() if value.get("status") != "revoked"]
        if not ids:
            return None
        selected_id = await self.next_account_id(ids)
        return {**accounts[selected_id], "account_id": selected_id}

    async def load_token(self, account_id: str) -> TokenInfo | None:
        credentials = await self.load_credentials(account_id)
        return token_from_credentials(credentials) if credentials else None

    async def _put(self, account_id: str, payload: dict, *, activate: bool = False) -> None:
        self._check_open()
        if not self.file_ready:
            accounts = await self.list_accounts()
            accounts[account_id] = payload
            await self.owner.put_kv_data(ACCOUNTS_KEY, accounts)
            return
        assert self.files is not None
        relative = self._managed_paths.get(account_id) or self.files.filename(account_id)
        document = credential_document(account_id, {"environment": self.files.environment, **payload})
        self._managed_paths[account_id] = relative
        expected = payload.get("_source_fingerprint")
        self._pending_updates[account_id] = {"document": document, "base_fingerprint": expected}
        await file_io(self.files.write, relative, account_id, document, expected=expected)
        self._pending_updates.pop(account_id, None)
        if activate and relative not in self._active_paths:
            self._active_paths.append(relative)
        await self._save_index()

    async def save_login_token(self, token: TokenInfo, *, account_id: str, device_id: str, session_id: str, local_credentials_path: str = "", monthly_reset: dict | None = None) -> str:
        account_id = await self.allocate_account_id(account_id)
        async with self.account_guard(account_id), self._lock:
            previous = await self._read_for_update(account_id)
            payload = {**(previous or {}), **self._token_payload(token, device_id=device_id, last_login_session=session_id, last_refresh_at=None, local_credentials_path=local_credentials_path)}
            if monthly_reset is not None:
                payload["monthly_reset"] = validate_monthly_reset(monthly_reset)
            await self._put(account_id, payload, activate=True)
            self._reserved.discard(account_id)
        if monthly_reset is not None:
            await self.cooldown.rebind(account_id, monthly_reset)
        return account_id

    async def _read_for_update(self, account_id: str):
        return await self._read_managed(account_id) if self.file_ready else await self.load_credentials(account_id)


    async def save_refreshed_token(self, account_id: str, token: TokenInfo, *, device_id: str, monthly_reset: dict | None = None) -> None:
        account_id = normalize_account_id(account_id)
        async with self.account_guard(account_id), self._lock:
            previous = await self._read_for_update(account_id)
            if previous is None:
                raise CredentialFileError("账号已移除，拒绝重新创建凭据。")
            payload = {**previous, **self._token_payload(token, device_id=device_id, last_login_session=str(previous.get("last_login_session") or ""), last_refresh_at=utc_now_iso(), local_credentials_path=str(previous.get("local_credentials_path") or ""))}
            if monthly_reset is not None:
                payload["monthly_reset"] = validate_monthly_reset(monthly_reset)
            await self._put(account_id, payload)
        if monthly_reset is not None:
            await self.cooldown.rebind(account_id, monthly_reset)

    async def mark_revoked(self, account_id: str) -> None:
        account_id = normalize_account_id(account_id)
        async with self.account_guard(account_id), self._lock:
            credentials = await self._read_managed(account_id) if self.file_ready else await self.load_credentials(account_id)
            if credentials:
                await self._put(account_id, {**credentials, "status": "revoked", "updated_at": utc_now_iso()})

    async def get_monthly_reset(self, account_id: str) -> dict | None:
        credentials = await self.load_credentials(account_id)
        return validate_monthly_reset(credentials.get("monthly_reset")) if credentials else None

    async def set_monthly_reset(self, account_id: str, rule: dict) -> None:
        rule = validate_monthly_reset(rule)
        async with self.account_guard(account_id), self._lock:
            credentials = await self.load_credentials(account_id)
            if not credentials:
                raise CredentialFileError("账号不存在或未启用。")
            await self._put(account_id, {**credentials, "monthly_reset": rule})
        await self.cooldown.rebind(account_id, rule)

    async def advance_monthly_reset(self, account_id: str) -> None:
        async with self.account_guard(account_id), self._lock:
            credentials = await self.load_credentials(account_id)
            if not credentials or not credentials.get("monthly_reset"):
                return
            previous = validate_monthly_reset(credentials["monthly_reset"])
            updated = advance_monthly_reset(previous, now=self.cooldown.clock())
            if updated != previous:
                await self._put(account_id, {**credentials, "monthly_reset": updated})

    async def import_file(self, relative: str, save_paths) -> str:
        if not self.file_ready or self.files is None:
            raise CredentialFileError("凭据文件存储尚未就绪。")
        document = await file_io(self.files.import_document, relative)
        account_id = document["account_id"]
        receipt = {"source": relative, "fingerprint": fingerprint(document)}
        async with self.account_guard(account_id), self._lock:
            self._check_open()
            path = self._managed_paths.get(account_id) or self.files.filename(account_id)
            if self._managed_paths.get(account_id) or self.files.path(path).exists():
                saved = await file_io(self.files.read, path)
                if saved.get("import_receipt") != receipt:
                    raise CredentialFileError(f"账号 {account_id} 已存在，拒绝上传覆盖。")
                # 保存配置失败后的重试只重新登记，不回灌上传文件里的旧 token。
                self._managed_paths[account_id] = path
                if path not in self._active_paths:
                    self._active_paths.append(path)
                await self._save_index()
            else:
                await self._put(account_id, {**document, "import_receipt": receipt}, activate=True)
            await save_paths(self.active_file_paths())
            return account_id

    async def delete_account(self, account_id: str) -> bool:
        account_id = normalize_account_id(account_id)
        async with self.account_guard(account_id), self._lock:
            self._check_open()
            if self.file_ready:
                relative = self._managed_paths.get(account_id)
                if not relative or self.files is None:
                    return False
                await file_io(self.files.delete, relative)
                self._pending_updates.pop(account_id, None)
                self._managed_paths.pop(account_id, None)
                self._active_paths = [p for p in self._active_paths if p != relative]
                await self._save_index()
            else:
                accounts = await self.list_accounts()
                if account_id not in accounts:
                    return False
                accounts.pop(account_id)
                await self.owner.put_kv_data(ACCOUNTS_KEY, accounts)
            self._reserved.discard(account_id)
        await self.cooldown.forget([account_id])
        return True

    async def delete_accounts(self, account_ids: list[str]) -> list[str]:
        removed = []
        for account_id in account_ids:
            if await self.delete_account(account_id):
                removed.append(normalize_account_id(account_id))
        return removed

    async def delete_credentials(self) -> None:
        if self.file_ready:
            await self.delete_accounts(list(self._managed_paths))
        else:
            await self.owner.delete_kv_data(ACCOUNTS_KEY)
        await self.owner.delete_kv_data(ACCOUNT_CONFIG_SNAPSHOT_KEY)
        await self.owner.delete_kv_data(ROTATION_CURSOR_KEY)
        await self.cooldown.forget()

    async def next_account_id(self, account_ids: list[str] | None = None) -> str:
        if account_ids is None:
            account_ids = await self.list_account_ids(include_revoked=False)
        ids = sorted(dict.fromkeys(normalize_account_id(item) for item in account_ids if item))
        if not ids:
            raise ValueError("No Kimi OAuth accounts are available.")
        async with self._lock:
            cursor = await self.owner.get_kv_data(ROTATION_CURSOR_KEY, 0)
            cursor = cursor if isinstance(cursor, int) else 0
            selected = ids[cursor % len(ids)]
            await self.owner.put_kv_data(ROTATION_CURSOR_KEY, (cursor + 1) % len(ids))
            return selected

    async def allocate_account_id(self, requested: str = "", *, reserve: bool = False) -> str:
        async with self._lock:
            requested = normalize_account_id(requested or "")
            accounts = await self.list_accounts()
            if not requested:
                index = 1
                while f"account-{index}" in accounts or f"account-{index}" in self._managed_paths or f"account-{index}" in self._reserved:
                    index += 1
                requested = f"account-{index}"
            if reserve:
                if self.file_ready and self.files is not None:
                    if requested in self._managed_paths:
                        await self._read_managed(requested)
                    elif self.files.path(self.files.filename(requested)).exists():
                        raise CredentialFileError("同名受管文件尚未登记，请先在 account_files 中启用该文件。")
                if requested in self._reserved:
                    raise CredentialFileError("该账号已有登录流程，请先取消或等待完成。")
                self._reserved.add(requested)
            return requested

    def release_reservation(self, account_id: str) -> None:
        self._reserved.discard(account_id)

    async def load_config_snapshot(self) -> list[str] | None:
        snapshot = await self.owner.get_kv_data(ACCOUNT_CONFIG_SNAPSHOT_KEY, None)
        return normalize_account_id_list(snapshot) if isinstance(snapshot, list) else None

    async def save_config_snapshot(self, account_ids: list[str]) -> None:
        await self.owner.put_kv_data(ACCOUNT_CONFIG_SNAPSHOT_KEY, normalize_account_id_list(account_ids))

    async def _load_accounts(self) -> dict[str, dict[str, Any]]:
        return await self.list_accounts()

    async def _save_accounts(self, accounts: dict[str, dict[str, Any]]) -> None:
        if self.file_ready:
            raise CredentialFileError("不允许批量覆盖受管凭据。")
        await self.owner.put_kv_data(ACCOUNTS_KEY, normalize_accounts(accounts))

    def _token_payload(self, token: TokenInfo, *, device_id: str, last_login_session: str, last_refresh_at: str | None, local_credentials_path: str = "") -> dict[str, Any]:
        return {
            "access_token": token.access_token, "refresh_token": token.refresh_token,
            "expires_at": token.expires_at, "expires_in": token.expires_in,
            "token_type": token.token_type, "scope": token.scope, "status": "valid",
            "device_id": device_id, "updated_at": utc_now_iso(), "last_refresh_at": last_refresh_at,
            "last_login_session": last_login_session, "local_credentials_path": local_credentials_path,
        }


def normalize_accounts(accounts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized = {}
    for raw_id, credentials in accounts.items():
        account_id = normalize_account_id(str(raw_id))
        if account_id and isinstance(credentials, dict):
            normalized[account_id] = credentials
    return normalized


def normalize_account_id(value: str) -> str:
    cleaned = ACCOUNT_ID_PATTERN.sub("-", str(value).strip())
    return cleaned.strip(".-_")[:64]


def normalize_account_id_list(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(account_id for value in values if isinstance(value, str) and (account_id := normalize_account_id(value))))


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def mask_token(token: str) -> str:
    if not token:
        return "none"
    if len(token) <= 12:
        return f"{token[:2]}...{token[-2:]}"
    return f"{token[:6]}...{token[-4:]}"
