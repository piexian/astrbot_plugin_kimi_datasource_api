from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Protocol

from .identity import new_device_id
from .models import TokenInfo, token_from_credentials

DEVICE_ID_KEY = "kimi_code.device_id"
ACCOUNTS_KEY = "kimi_code.accounts"
ACCOUNT_CONFIG_SNAPSHOT_KEY = "kimi_code.account_config_snapshot"
ROTATION_CURSOR_KEY = "kimi_code.rotation_cursor"
ACCOUNT_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class KVOwner(Protocol):
    async def put_kv_data(self, key: str, value: Any) -> None: ...
    async def get_kv_data(self, key: str, default: Any) -> Any: ...
    async def delete_kv_data(self, key: str) -> None: ...


class KimiCredentialStore:
    def __init__(self, owner: KVOwner) -> None:
        self.owner = owner

    async def get_device_id(self) -> str:
        existing = await self.owner.get_kv_data(DEVICE_ID_KEY, None)
        if isinstance(existing, str) and existing.strip():
            return existing.strip()
        device_id = new_device_id()
        await self.owner.put_kv_data(DEVICE_ID_KEY, device_id)
        return device_id

    async def save_device_id(self, device_id: str) -> None:
        cleaned = str(device_id).strip()
        if cleaned:
            await self.owner.put_kv_data(DEVICE_ID_KEY, cleaned)

    async def list_accounts(self) -> dict[str, dict[str, Any]]:
        accounts = await self._load_accounts()
        normalized = normalize_accounts(accounts)
        if normalized != accounts:
            await self._save_accounts(normalized)
        return normalized

    async def list_account_ids(self, *, include_revoked: bool = True) -> list[str]:
        accounts = await self.list_accounts()
        ids = []
        for account_id, credentials in accounts.items():
            if include_revoked or credentials.get("status") != "revoked":
                ids.append(account_id)
        return ids

    async def load_credentials(self, account_id: str | None = None) -> dict[str, Any] | None:
        accounts = await self.list_accounts()
        if account_id is not None:
            credentials = accounts.get(normalize_account_id(account_id))
            return credentials.copy() if isinstance(credentials, dict) else None

        account_ids = [key for key, value in accounts.items() if value.get("status") != "revoked"]
        if not account_ids:
            return None
        selected = await self.next_account_id(account_ids)
        credentials = accounts.get(selected)
        if isinstance(credentials, dict):
            credentials = credentials.copy()
            credentials["account_id"] = selected
            return credentials
        return None

    async def load_token(self, account_id: str) -> TokenInfo | None:
        credentials = await self.load_credentials(account_id)
        if not credentials:
            return None
        return token_from_credentials(credentials)

    async def save_login_token(
        self,
        token: TokenInfo,
        *,
        account_id: str,
        device_id: str,
        session_id: str,
    ) -> str:
        normalized_id = await self.allocate_account_id(account_id)
        accounts = await self.list_accounts()
        accounts[normalized_id] = self._token_payload(
            token,
            device_id=device_id,
            last_login_session=session_id,
            last_refresh_at=None,
        )
        await self._save_accounts(accounts)
        return normalized_id

    async def save_refreshed_token(self, account_id: str, token: TokenInfo, *, device_id: str) -> None:
        account_id = normalize_account_id(account_id)
        accounts = await self.list_accounts()
        previous = accounts.get(account_id, {})
        accounts[account_id] = self._token_payload(
            token,
            device_id=device_id,
            last_login_session=str(previous.get("last_login_session") or ""),
            last_refresh_at=utc_now_iso(),
        )
        await self._save_accounts(accounts)

    async def mark_revoked(self, account_id: str) -> None:
        account_id = normalize_account_id(account_id)
        accounts = await self.list_accounts()
        credentials = accounts.get(account_id)
        if not credentials:
            return
        credentials["status"] = "revoked"
        credentials["updated_at"] = utc_now_iso()
        await self._save_accounts(accounts)

    async def delete_account(self, account_id: str) -> bool:
        account_id = normalize_account_id(account_id)
        accounts = await self.list_accounts()
        if account_id not in accounts:
            return False
        accounts.pop(account_id, None)
        await self._save_accounts(accounts)
        return True

    async def delete_accounts(self, account_ids: list[str]) -> list[str]:
        accounts = await self.list_accounts()
        removed: list[str] = []
        for account_id in account_ids:
            normalized = normalize_account_id(account_id)
            if normalized in accounts:
                accounts.pop(normalized, None)
                removed.append(normalized)
        if removed:
            await self._save_accounts(accounts)
        return removed

    async def delete_credentials(self) -> None:
        await self.owner.delete_kv_data(ACCOUNTS_KEY)
        await self.owner.delete_kv_data(ACCOUNT_CONFIG_SNAPSHOT_KEY)
        await self.owner.delete_kv_data(ROTATION_CURSOR_KEY)

    async def next_account_id(self, account_ids: list[str] | None = None) -> str:
        if account_ids is None:
            account_ids = await self.list_account_ids(include_revoked=False)
        account_ids = sorted(dict.fromkeys(normalize_account_id(item) for item in account_ids if item))
        if not account_ids:
            raise ValueError("No Kimi OAuth accounts are available.")

        cursor = await self.owner.get_kv_data(ROTATION_CURSOR_KEY, 0)
        if not isinstance(cursor, int):
            cursor = 0
        selected = account_ids[cursor % len(account_ids)]
        await self.owner.put_kv_data(ROTATION_CURSOR_KEY, (cursor + 1) % len(account_ids))
        return selected

    async def allocate_account_id(self, requested: str = "") -> str:
        requested = normalize_account_id(requested or "")
        accounts = await self.list_accounts()
        if requested:
            return requested

        index = 1
        while True:
            candidate = f"account-{index}"
            if candidate not in accounts:
                return candidate
            index += 1

    async def load_config_snapshot(self) -> list[str] | None:
        snapshot = await self.owner.get_kv_data(ACCOUNT_CONFIG_SNAPSHOT_KEY, None)
        if not isinstance(snapshot, list):
            return None
        return normalize_account_id_list(snapshot)

    async def save_config_snapshot(self, account_ids: list[str]) -> None:
        await self.owner.put_kv_data(ACCOUNT_CONFIG_SNAPSHOT_KEY, normalize_account_id_list(account_ids))

    async def _load_accounts(self) -> dict[str, dict[str, Any]]:
        data = await self.owner.get_kv_data(ACCOUNTS_KEY, {})
        return data if isinstance(data, dict) else {}

    async def _save_accounts(self, accounts: dict[str, dict[str, Any]]) -> None:
        await self.owner.put_kv_data(ACCOUNTS_KEY, normalize_accounts(accounts))

    def _token_payload(
        self,
        token: TokenInfo,
        *,
        device_id: str,
        last_login_session: str,
        last_refresh_at: str | None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        return {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "expires_in": token.expires_in,
            "token_type": token.token_type,
            "scope": token.scope,
            "status": "valid",
            "device_id": device_id,
            "updated_at": now,
            "last_refresh_at": last_refresh_at,
            "last_login_session": last_login_session,
        }


def normalize_accounts(accounts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for raw_id, credentials in accounts.items():
        account_id = normalize_account_id(str(raw_id))
        if not account_id or not isinstance(credentials, dict):
            continue
        normalized[account_id] = credentials
    return normalized


def normalize_account_id(value: str) -> str:
    cleaned = ACCOUNT_ID_PATTERN.sub("-", str(value).strip())
    cleaned = cleaned.strip(".-_")
    return cleaned[:64]


def normalize_account_id_list(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        account_id = normalize_account_id(value)
        if account_id and account_id not in seen:
            result.append(account_id)
            seen.add(account_id)
    return result


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def mask_token(token: str) -> str:
    if not token:
        return "none"
    if len(token) <= 12:
        return f"{token[:2]}...{token[-2:]}"
    return f"{token[:6]}...{token[-4:]}"
