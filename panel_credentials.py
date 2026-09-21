"""以原生文件列表启用账号，内部凭据仅用于原子更新和冲突恢复。"""

from __future__ import annotations

import asyncio


from .credential_files import CredentialFileError, file_io, fingerprint
from .models import KimiPluginError
from .storage import FILE_STORE_KEY, KimiCredentialStore, normalize_account_id


class KimiPanelCredentialStore(KimiCredentialStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.panel_ready = False
        self._selected_panels: list[str] = []
        self._panel_refs: dict[str, str] = {}
        self._selection_errors: dict[str, str] = {}

    async def _save_index(self) -> None:
        previous = await self.owner.get_kv_data(FILE_STORE_KEY, {})
        state = {
            "version": 1, "phase": "files", "files": self._managed_paths.copy(),
            "cleanup_pending": bool(previous.get("cleanup_pending", False)),
            "panel_ready": self.panel_ready or bool(previous.get("panel_ready")),
        }
        if previous != state:
            await self.owner.put_kv_data(FILE_STORE_KEY, state)

    def panel_paths(self) -> list[str]:
        return self._selected_panels.copy()

    def credential_path(self, account_id: str) -> str:
        return self._panel_refs.get(normalize_account_id(account_id), "") if self.panel_ready else super().credential_path(account_id)

    def inactive_files(self) -> dict[str, str]:
        if not self.panel_ready:
            return super().inactive_files()
        return {key: path for key, path in self._panel_refs.items() if path not in self._selected_panels}

    async def _export(self, private: str, selected: list[str]) -> str:
        document = await file_io(self.files.read, private)
        account_id = document["account_id"]
        public = self.files.panel_document(document)
        expected = fingerprint(public)
        link = document.get("panel_sync")
        if isinstance(link, dict):
            path = link["path"]
        else:
            path = self.files.panel_filename(account_id)
            for candidate in selected:
                try:
                    uploaded = await file_io(self.files.import_document, candidate)
                    if uploaded["account_id"] == account_id and fingerprint(uploaded) == expected:
                        path = candidate
                        break
                except KimiPluginError:
                    continue
        existing = await file_io(self.files.panel_fingerprint, path)
        if existing is None:
            await file_io(self.files.write_panel, path, public, expected=None, create=True)
        elif existing != expected:
            if link:
                await file_io(self.files.sync_panel, private)
            else:
                raise CredentialFileError(f"迁移目标文件 {path} 已有不同凭据，未覆盖。")
        self._panel_refs[account_id] = path
        return path

    async def synchronize(self, selected: list[str], legacy_paths: list[str], legacy_ids: list[str], save_selected) -> None:
        async with self._lock:
            self._check_open()
            selected = self._clean_paths(selected)
            state = await self.owner.get_kv_data(FILE_STORE_KEY, {})
            migrated = bool(state.get("panel_ready")) if isinstance(state, dict) else False
            published = selected.copy()

            async def publish_initial(private_paths):
                for private in private_paths:
                    path = await self._export(private, published)
                    if path not in published:
                        published.append(path)
                await save_selected(published)

            if not self.file_ready:
                await super().initialize_files(legacy_paths, legacy_ids, publish_initial)
            else:
                await self._finish_migration(state)
            if not migrated:
                # 兼容前版的独立文件列表；未启用文件也显示在原生文件对话框中。
                for private in self._active_paths:
                    document = await file_io(self.files.read, private)
                    self._managed_paths.setdefault(document["account_id"], private)
                old_active = set(self._active_paths)
                for account_id, private in self._managed_paths.items():
                    path = await self._export(private, published)
                    document = await file_io(self.files.read, private)
                    digest = fingerprint(self.files.panel_document(document))
                    link = {"path": path, "base": digest, "hash": digest}
                    if document.get("panel_sync") != link:
                        await file_io(self.files.write, private, account_id, {**document, "panel_sync": link}, expected=fingerprint(document))
                    if private in old_active and path not in published:
                        published.append(path)
                await save_selected(published)
                self.panel_ready = True
                await self._save_index()
            else:
                self.panel_ready = True
                published = selected
            self._selected_panels = published
            await self._bind_selection()

    async def _bind_selection(self) -> None:
        self._active_paths = []
        self._selection_errors = {}
        for account_id, private in self._managed_paths.items():
            try:
                document = await file_io(self.files.read, private)
                path = document.get("panel_sync", {}).get("path")
                if path:
                    link = document["panel_sync"]
                    if path not in self._selected_panels and link.get("enabled", True):
                        updated = {**document, "panel_sync": {**link, "enabled": False}}
                        await file_io(self.files.write, private, account_id, updated, expected=fingerprint(document))
                    self._panel_refs[account_id] = path
            except KimiPluginError as exc:
                self._selection_errors[private] = str(exc)
        seen = {}
        for path in self._selected_panels:
            try:
                uploaded = await file_io(self.files.import_document, path)
                await file_io(self.files.protect_panel, path)
                account_id = uploaded["account_id"]
                owner = next((key for key, ref in self._panel_refs.items() if ref == path), None)
                if owner is not None and owner != account_id:
                    raise CredentialFileError(f"文件已关联账号 {owner}，请为其他账号使用新文件名。")
                if account_id in seen:
                    self._selection_errors[seen[account_id]] = "账号 ID 重复，已停用。"
                    known = self._managed_paths.get(account_id)
                    self._active_paths = [item for item in self._active_paths if item != known]
                    raise CredentialFileError("账号 ID 重复，已停用。")
                seen[account_id] = path
                private = self._managed_paths.get(account_id) or self.files.filename(account_id)
                document = await file_io(self.files.read_optional, private)
                if document is not None:
                    link = document.get("panel_sync", {})
                    incoming = fingerprint(uploaded)
                    wanted = fingerprint(self.files.panel_document(document))
                    accepted = {wanted, link.get("base"), link.get("hash")}
                    if link.get("path") == path and incoming in accepted:
                        document = await file_io(self.files.sync_panel, private)
                        link = document["panel_sync"]
                        if not link.get("enabled", True):
                            await file_io(self.files.write, private, account_id, {**document, "panel_sync": {**link, "enabled": True}}, expected=fingerprint(document))
                    else:
                        if (link.get("enabled", True) and incoming != wanted) or (link.get("path") != path and link.get("path") in self._selected_panels):
                            raise CredentialFileError(f"账号 {account_id} 正在使用，拒绝覆盖；替换时先从文件列表删除并保存配置。")
                        guard = self.account_guard(account_id)
                        if guard.lock.locked() and guard.owner is not asyncio.current_task():
                            raise CredentialFileError("账号正在刷新，暂不替换凭据；完成后执行 kimi sync 或重新保存配置。")
                        # 重新启用另一个凭据是显式替换，不能继承旧 CLI 的回写权限。
                        updated = document if incoming == wanted else uploaded
                        updated = {**updated, "panel_sync": {"path": path, "base": incoming, "hash": incoming, "enabled": True}}
                        await file_io(self.files.write, private, account_id, updated, expected=fingerprint(document))
                else:
                    digest = fingerprint(uploaded)
                    document = {**uploaded, "panel_sync": {"path": path, "base": digest, "hash": digest}}
                    await file_io(self.files.write, private, account_id, document, expected=None)
                self._managed_paths[account_id] = private
                self._panel_refs[account_id] = path
                self._active_paths.append(private)
            except KimiPluginError as exc:
                self._selection_errors[path] = str(exc)
        await self._save_index()

    async def list_accounts(self):
        accounts = await super().list_accounts()
        self.file_errors.update(self._selection_errors)
        return accounts

    async def _read_managed(self, account_id: str, *, check_panel: bool = True):
        document = await super()._read_managed(account_id)
        if document is not None and self.panel_ready and check_panel:
            private = self._managed_paths[account_id]
            document = await file_io(self.files.sync_panel, private)
            self._panel_refs[account_id] = document["panel_sync"]["path"]
            return {**document, "_source_fingerprint": fingerprint(document), "_credential_file": document["panel_sync"]["path"]}
        return document

    async def _read_for_update(self, account_id: str):
        # 已收到刷新结果时优先保全新 token，文件界面的并发改动在发布阶段校验。
        return await super()._read_managed(account_id) if self.file_ready else await self.load_credentials(account_id)

    async def allocate_account_id(self, requested: str = "", *, reserve: bool = False) -> str:
        async with self._lock:
            account_id = await super().allocate_account_id(requested, reserve=False)
            if reserve:
                if account_id in self._reserved:
                    raise CredentialFileError("该账号已有登录流程，请先取消或等待完成。")
                if account_id in self._managed_paths:
                    document = await self._read_for_update(account_id)
                    path = document.get("panel_sync", {}).get("path") if document else None
                    if path and self.files.path(path, uploaded=True).exists():
                        await self._read_managed(account_id)
                elif self.files and self.files.path(self.files.filename(account_id)).exists():
                    raise CredentialFileError("同名内部凭据已有恢复记录，请先执行 kimi sync。")
                self._reserved.add(account_id)
            return account_id


    async def _put(self, account_id: str, payload: dict, *, activate: bool = False) -> None:
        if not self.panel_ready:
            await super()._put(account_id, payload, activate=activate)
            return
        link = payload.get("panel_sync", {})
        path = link.get("path") or self.files.panel_filename(account_id)
        public = self.files.panel_document({"account_id": account_id, "environment": self.files.environment, **payload})
        base = link.get("hash")
        if activate and not self.files.path(path, uploaded=True).exists():
            base = None
        document = {**payload, "panel_sync": {"path": path, "base": base, "hash": fingerprint(public), "enabled": activate or link.get("enabled", True)}}
        await super()._put(account_id, document, activate=activate)
        await file_io(self.files.sync_panel, self._managed_paths[account_id], create=activate)
        self._panel_refs[account_id] = path
        if activate and path not in self._selected_panels:
            self._selected_panels.append(path)

    async def delete_account(self, account_id: str) -> bool:
        account_id = normalize_account_id(account_id)
        async with self.account_guard(account_id):
            async with self._lock:
                private = self._managed_paths.get(account_id)
                document = await file_io(self.files.read_optional, private) if private else None
                path = document.get("panel_sync", {}).get("path") if document else None
                if path:
                    try:
                        current = await file_io(self.files.import_document, path)
                        if current["account_id"] == account_id:
                            await file_io(self.files.remove_import, path, expected=fingerprint(current))
                    except CredentialFileError:
                        pass
            removed = await super().delete_account(account_id)
            async with self._lock:
                self._panel_refs.pop(account_id, None)
                self._selected_panels = [item for item in self._selected_panels if item != path]
            return removed
