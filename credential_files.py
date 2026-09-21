"""受管凭据的路径校验、原子落盘和未完成更新恢复。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import DEFAULT_KIMI_CODE_BASE_URL, DEFAULT_OAUTH_HOST
from .models import KimiPluginError
from .monthly import validate_monthly_reset

MAX_CREDENTIAL_BYTES = 64 * 1024
IMPORT_PREFIX = "files/account_settings/credential_imports/"
_UNSPECIFIED = object()
ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?")
DEFAULT_ENVIRONMENT = {"oauth_host": DEFAULT_OAUTH_HOST, "base_url": DEFAULT_KIMI_CODE_BASE_URL}


class CredentialFileError(KimiPluginError):
    pass


class AsyncRLock:
    """同一任务可重入，避免账号刷新与落盘互相死锁。"""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None
        self.depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if self.owner is not task:
            await self.lock.acquire()
            self.owner = task
        self.depth += 1
        return self

    async def __aexit__(self, *args):
        self.depth -= 1
        if not self.depth:
            self.owner = None
            self.lock.release()


async def file_io(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 后台线程退出前不能释放文件锁，否则迟到的写入可能覆盖新凭据。
        await task
        raise


def json_bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json_bytes(data)).hexdigest()


def token_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError, UnicodeError):
        return {}


def _load_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise CredentialFileError("凭据文件必须是有效且无重复字段的 JSON 对象。") from None


def credential_document(account_id: str, data: dict) -> dict:
    if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id):
        raise CredentialFileError("账号 ID 无效。")
    document = {key: value for key, value in data.items() if not key.startswith("_")}
    if type(document.get("schema_version", 1)) is not int or document.get("schema_version", 1) != 1:
        raise CredentialFileError("不支持的凭据文件版本。")
    document.update(schema_version=1, account_id=account_id)
    if any(not isinstance(document.get(key), str) or not document[key] for key in ("access_token", "refresh_token")):
        raise CredentialFileError("凭据文件缺少 access_token 或 refresh_token。")
    for key in ("access_token", "refresh_token"):
        if any(not 0x21 <= ord(char) < 0x7F for char in document[key]):
            raise CredentialFileError("凭据 token 包含无效字符。")
    for key in ("expires_at", "expires_in"):
        value = document.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 32503680000 or not math.isfinite(value):
            raise CredentialFileError("凭据文件的过期时间无效。")
        document[key] = value
    if not isinstance(document.get("status", "valid"), str) or document.get("status", "valid") not in {"valid", "revoked"}:
        raise CredentialFileError("凭据状态必须为 valid 或 revoked。")
    document.setdefault("status", "valid")
    for key in ("scope", "token_type", "local_credentials_path"):
        if key in document and not isinstance(document[key], str):
            raise CredentialFileError(f"凭据字段 {key} 必须是字符串。")
    device = document.get("device_id")
    if not isinstance(device, str) or not device.strip() or len(device) > 128 or any(not 0x20 <= ord(c) < 0x7F for c in device):
        raise CredentialFileError("凭据文件缺少有效 device_id。")
    if document.get("monthly_reset") is not None:
        document["monthly_reset"] = validate_monthly_reset(document["monthly_reset"])
    if "panel_sync" in document:
        link = document["panel_sync"]
        if (not isinstance(link, dict) or not isinstance(link.get("path"), str)
                or not isinstance(link.get("hash"), str)
                or link.get("base") is not None and not isinstance(link["base"], str)
                or "enabled" in link and type(link["enabled"]) is not bool):
            raise CredentialFileError("凭据文件的同步恢复记录无效。")
    try:
        if len(json_bytes(document)) > MAX_CREDENTIAL_BYTES:
            raise CredentialFileError("凭据文件超过 64 KiB。")
    except (ValueError, TypeError, OverflowError):
        raise CredentialFileError("凭据文件包含无法保存的字段。") from None
    return document


def env_credential_filename(oauth_host: str, base_url: str) -> str:
    payload = json.dumps({"oauthHost": oauth_host.strip().rstrip("/"), "baseUrl": base_url.strip().rstrip("/")}, separators=(",", ":"))
    return f"kimi-code-env-{hashlib.sha256(payload.encode()).hexdigest()[:16]}.json"


class CredentialFiles:
    def __init__(self, root: Path, *, environment: dict | None = None):
        self.root = root.resolve()
        self.environment = dict(DEFAULT_ENVIRONMENT if environment is None else environment)

    def _check_environment(self, document: dict) -> None:
        source = document.get("environment", DEFAULT_ENVIRONMENT)
        if source != self.environment:
            raise CredentialFileError("凭据的 OAuth/API 环境与当前插件配置不匹配。")

    @staticmethod
    def filename(account_id: str) -> str:
        if not ACCOUNT_ID_RE.fullmatch(account_id):
            raise CredentialFileError("账号 ID 无效。")
        suffix = hashlib.sha256(account_id.encode()).hexdigest()[:10]
        return f"credentials/{account_id[:40]}-{suffix}.json"

    def path(self, relative: str, *, uploaded: bool = False) -> Path:
        if not isinstance(relative, str) or "\\" in relative:
            raise CredentialFileError("凭据路径必须是受管目录中的相对路径。")
        parts = PurePosixPath(relative).parts
        prefix = ("files", "account_settings", "credential_imports") if uploaded else ("credentials",)
        if len(parts) != len(prefix) + 1 or parts[:-1] != prefix or PurePosixPath(relative).is_absolute():
            raise CredentialFileError("凭据路径不在允许的目录内。")
        name = parts[-1]
        if not name.lower().endswith(".json") or name.endswith((" ", ".")) or any(c in name for c in ':<>|?*\x00'):
            raise CredentialFileError("凭据文件名无效。")
        target = self.root
        for part in parts:
            if part in {".", ".."}:
                raise CredentialFileError("不允许路径穿越。")
            target = target / part
            if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
                raise CredentialFileError("凭据路径不能包含符号链接。")
        if not target.resolve().is_relative_to(self.root):
            raise CredentialFileError("凭据路径越界。")
        return target

    def _read(self, path: Path, *, limit: int = MAX_CREDENTIAL_BYTES) -> bytes:
        try:
            with path.open("rb") as handle:
                raw = handle.read(limit + 1)
        except OSError as exc:
            raise CredentialFileError(f"凭据文件无法读取：{path.name}（{type(exc).__name__}）。") from None
        if len(raw) > limit:
            raise CredentialFileError("凭据文件超过大小限制。")
        return raw

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _journal(path: Path) -> Path:
        journal = path.with_name(f".{path.name}.pending")
        if journal.is_symlink():
            raise CredentialFileError("凭据恢复记录不能是符号链接。")
        return journal

    def _recover(self, path: Path) -> None:
        journal = self._journal(path)
        if not journal.exists():
            return
        entry = _load_object(self._read(journal, limit=MAX_CREDENTIAL_BYTES * 2))
        data = entry.get("document")
        if not isinstance(data, dict):
            raise CredentialFileError("凭据恢复记录无效，请管理员检查。")
        document = credential_document(data.get("account_id"), data)
        raw = self._read(path) if path.exists() else None
        current_data = _load_object(raw) if raw is not None else None
        current = fingerprint(credential_document(current_data.get("account_id"), current_data)) if current_data else None
        if current == fingerprint(document):
            journal.unlink()
        elif current == entry.get("base_fingerprint"):
            self._atomic(path, json_bytes(document))
            journal.unlink()
        else:
            raise CredentialFileError("凭据恢复记录与当前文件冲突，已停止使用旧凭据。")

    def read_optional(self, relative: str) -> dict | None:
        path = self.path(relative)
        if not path.exists() and not self._journal(path).exists():
            return None
        return self.read(relative)


    def read(self, relative: str) -> dict:
        path = self.path(relative)
        try:
            self._recover(path)
            data = _load_object(self._read(path))
            document = credential_document(data.get("account_id"), data)
            self._check_environment(document)
            return document
        except OSError as exc:
            raise CredentialFileError(f"凭据恢复失败：{type(exc).__name__}。") from None

    def write(self, relative: str, account_id: str, data: dict, *, expected=_UNSPECIFIED) -> None:
        path = self.path(relative)
        document = credential_document(account_id, data)
        self._check_environment(document)
        try:
            self._recover(path)
            old = self._read(path) if path.exists() else None
            if old is not None and _load_object(old).get("account_id") != account_id:
                raise CredentialFileError("凭据文件属于另一个账号，拒绝覆盖。")
            previous = credential_document(account_id, _load_object(old)) if old is not None else None
            actual = fingerprint(previous) if previous else None
            base = actual if expected is _UNSPECIFIED else expected
            entry = {"base_fingerprint": base, "document": document}
            # 先持久化新 token；rename 失败时下次读取恢复，不能重用已轮换的旧 token。
            journal = self._journal(path)
            self._atomic(journal, json_bytes(entry))
            if actual != base:
                raise CredentialFileError("凭据文件在读取后发生变更，未覆盖；新凭据已保存在受管恢复记录中。")
            self._atomic(path, json_bytes(document))
            journal.unlink()
        except OSError as exc:
            raise CredentialFileError(f"凭据落盘失败：{type(exc).__name__}；请修复文件权限或磁盘空间后重试。") from None

    def import_document(self, relative: str) -> dict:
        return self._import_bytes(relative, self._read(self.path(relative, uploaded=True)))

    def _import_bytes(self, relative: str, raw: bytes) -> dict:
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise CredentialFileError("凭据文件超过大小限制。")
        data = _load_object(raw)
        account_id = data.get("account_id") or PurePosixPath(relative).stem
        claims = token_claims(str(data.get("access_token") or ""))
        name = PurePosixPath(relative).name
        if "environment" not in data:
            if name.startswith("kimi-code-env-"):
                if self.environment == DEFAULT_ENVIRONMENT or name != env_credential_filename(**self.environment):
                    raise CredentialFileError("上传的 CLI 凭据环境不匹配。")
            elif self.environment != DEFAULT_ENVIRONMENT:
                raise CredentialFileError("自定义环境需上传对应的 kimi-code-env 文件或带 environment 的插件凭据。")
        self._check_environment({"environment": data.get("environment", self.environment)})
        if claims.get("iss") == "account":
            raise CredentialFileError("这是网页登录令牌，请上传 Kimi Code OAuth 凭据。")
        fields = {key: data[key] for key in (
            "schema_version", "access_token", "refresh_token", "expires_at", "expires_in", "token_type", "scope", "status", "device_id", "monthly_reset"
        ) if key in data}
        fields.setdefault("device_id", claims.get("device_id"))
        fields["environment"] = dict(self.environment)
        # 上传文件不能指定本机 CLI 回写路径或会话归属。
        return credential_document(account_id, fields)

    def delete(self, relative: str) -> None:
        path = self.path(relative)
        self._journal(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)

    @classmethod
    def panel_filename(cls, account_id: str) -> str:
        return IMPORT_PREFIX + PurePosixPath(cls.filename(account_id)).name

    @staticmethod
    def panel_document(data: dict) -> dict:
        fields = {key: data[key] for key in (
            "schema_version", "account_id", "access_token", "refresh_token", "expires_at", "expires_in",
            "token_type", "scope", "status", "device_id", "monthly_reset", "environment",
        ) if key in data}
        return credential_document(data.get("account_id"), fields)

    def panel_fingerprint(self, relative: str) -> str | None:
        if not self.path(relative, uploaded=True).exists():
            return None
        return fingerprint(self.import_document(relative))

    def protect_panel(self, relative: str) -> None:
        if os.name != "nt":
            try:
                self.path(relative, uploaded=True).chmod(0o600)
            except OSError as exc:
                raise CredentialFileError(f"无法收紧凭据文件权限：{type(exc).__name__}。") from None


    def write_panel(self, relative: str, data: dict, *, expected: str | None, create: bool = False) -> None:
        path = self.path(relative, uploaded=True)
        current = self.panel_fingerprint(relative)
        if current is None and not create:
            raise CredentialFileError("凭据文件已被删除，账号已停止使用；不会自动重建。")
        if current != expected:
            raise CredentialFileError("凭据文件已被其他操作覆盖，已停止使用；请勿上传旧副本覆盖正在刷新的文件。")
        public = self.panel_document(data)
        raw = json_bytes(public)
        try:
            if current is None:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                # 对已存在的文件持有句柄写回，避免 rename 把界面刚删除的文件复活。
                with path.open("r+b") as stream:
                    if fingerprint(self._import_bytes(relative, stream.read(MAX_CREDENTIAL_BYTES + 1))) != expected:
                        raise CredentialFileError("凭据文件在同步前已改变，未覆盖。")
                    stream.seek(0)
                    stream.write(raw)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
            if self.panel_fingerprint(relative) != fingerprint(public):
                raise CredentialFileError("凭据文件在同步期间已被删除或修改，最新 token 已保留在内部恢复文件。")
        except OSError as exc:
            raise CredentialFileError(f"凭据文件同步失败：{type(exc).__name__}；最新 token 已保留在内部恢复文件。") from None

    def sync_panel(self, private: str, *, create: bool = False) -> dict:
        document = self.read(private)
        link = document.get("panel_sync")
        if not isinstance(link, dict) or not isinstance(link.get("path"), str):
            raise CredentialFileError("凭据尚未关联文件列表。")
        public = self.panel_document(document)
        wanted = fingerprint(public)
        current = self.panel_fingerprint(link["path"])
        if current is None and not create:
            raise CredentialFileError("凭据文件已被删除，账号已停止使用；不会自动重建。")
        if current != wanted:
            if current not in {link.get("base"), link.get("hash")} or (current is None and link.get("base") is not None):
                raise CredentialFileError("凭据文件内容冲突，已停止使用；内部最新凭据保留，请勿用旧副本覆盖。")
            self.write_panel(link["path"], public, expected=current, create=create)
        settled = {**link, "path": link["path"], "base": wanted, "hash": wanted}
        if link != settled:
            updated = {**document, "panel_sync": settled}
            self.write(private, document["account_id"], updated, expected=fingerprint(document))
            document = updated
        return document


    def remove_import(self, relative: str, *, expected: str | None = None) -> None:
        if expected is not None and fingerprint(self.import_document(relative)) != expected:
            raise CredentialFileError("上传暂存文件已更新，已保留新文件；请保存配置后重新导入。")
        self.path(relative, uploaded=True).unlink(missing_ok=True)
