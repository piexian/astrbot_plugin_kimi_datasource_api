"""单文件列表、兼容迁移及文件界面冲突的离线回归。"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from conftest import MemoryOwner
from astrbot_plugin_kimi_datasource_api.credential_files import CredentialFileError
from astrbot_plugin_kimi_datasource_api.panel_credentials import KimiPanelCredentialStore
from astrbot_plugin_kimi_datasource_api.storage import ACCOUNTS_KEY, FILE_STORE_KEY, KimiCredentialStore
from astrbot_plugin_kimi_datasource_api.oauth import KimiOAuthClient
from astrbot_plugin_kimi_datasource_api.monthly import parse_monthly_reset
from test_file_store import new_token


async def panel_store(tmp_path, owner=None):
    owner = owner or MemoryOwner()
    store = KimiPanelCredentialStore(owner, credential_root=tmp_path)
    config = {"selected": []}
    async def save(paths):
        config["selected"] = paths.copy()
    await store.synchronize([], [], ["test"], save)
    return store, owner, config, save


@pytest.mark.asyncio
async def test_old_kv_appears_in_file_widget_without_relogin(tmp_path):
    owner = MemoryOwner()
    owner.data[ACCOUNTS_KEY]["test"].update(local_credentials_path="trusted-cli.json", status="revoked")
    store, owner, config, _ = await panel_store(tmp_path, owner)
    assert ACCOUNTS_KEY not in owner.data
    assert len(config["selected"]) == 1
    visible = store.files.import_document(config["selected"][0])
    assert visible["refresh_token"] == "test-refresh-secret"
    assert visible["status"] == "revoked"
    assert not visible.get("local_credentials_path")
    assert (await store.load_credentials("test"))["local_credentials_path"] == "trusted-cli.json"


@pytest.mark.asyncio
async def test_previous_two_list_store_migrates_active_and_inactive_files(tmp_path):
    owner = MemoryOwner()
    old = KimiCredentialStore(owner, credential_root=tmp_path)
    private_paths = []
    async def save_old(paths):
        private_paths[:] = paths
    await old.initialize_files([], ["test"], save_old)
    await old.save_login_token(new_token("inactive"), account_id="inactive", device_id="device", session_id="session")
    store = KimiPanelCredentialStore(owner, credential_root=tmp_path)
    selected = []
    async def save(paths):
        selected[:] = paths
    await store.synchronize([], private_paths, [], save)
    assert len(selected) == 1
    assert (await store.load_credentials("test"))["refresh_token"] == "test-refresh-secret"
    assert store.files.path(store.files.panel_filename("inactive"), uploaded=True).exists()
    assert await store.load_credentials("inactive") is None
    # 在同一个原生控件把目录中的文件加入配置，即可启用。
    selected.append(store.files.panel_filename("inactive"))
    await store.synchronize(selected, [], [], save)
    assert await store.load_token("inactive") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["config", "commit", "cleanup"])
async def test_legacy_migration_retries_without_losing_credentials(tmp_path, monkeypatch, stage):
    owner = MemoryOwner()
    original = copy.deepcopy(owner.data[ACCOUNTS_KEY])
    store = KimiPanelCredentialStore(owner, credential_root=tmp_path)
    selected = []
    pending = True
    original_put, original_delete = owner.put_kv_data, owner.delete_kv_data
    async def save(paths):
        nonlocal pending
        if stage == "config" and pending:
            pending = False
            raise CredentialFileError("injected config failure")
        selected[:] = paths
    async def put(key, value):
        nonlocal pending
        if stage == "commit" and pending and key == FILE_STORE_KEY and value.get("phase") == "files":
            pending = False
            raise CredentialFileError("injected marker failure")
        await original_put(key, value)
    async def delete(key):
        nonlocal pending
        if stage == "cleanup" and pending and key == ACCOUNTS_KEY:
            pending = False
            raise CredentialFileError("injected cleanup failure")
        await original_delete(key)
    monkeypatch.setattr(owner, "put_kv_data", put)
    monkeypatch.setattr(owner, "delete_kv_data", delete)
    with pytest.raises(CredentialFileError):
        await store.synchronize([], [], ["test"], save)
    assert owner.data[ACCOUNTS_KEY] == original
    await store.synchronize(selected, [], ["test"], save)
    assert (await store.load_credentials("test"))["refresh_token"] == "test-refresh-secret"
    assert ACCOUNTS_KEY not in owner.data


@pytest.mark.asyncio
async def test_refresh_and_monthly_binding_update_visible_file(tmp_path):
    store, _, config, _ = await panel_store(tmp_path)
    rule = parse_monthly_reset("22")
    await store.save_refreshed_token("test", new_token(), device_id="device", monthly_reset=rule)
    visible = store.files.import_document(config["selected"][0])
    assert visible["refresh_token"] == "refresh-new"
    assert visible["monthly_reset"]["day"] == 22
    assert visible["monthly_reset"]["time"] is None


@pytest.mark.asyncio
async def test_delete_in_widget_disables_immediately_and_never_recreates_file(tmp_path):
    store, owner, config, save = await panel_store(tmp_path)
    path = store.files.path(config["selected"][0], uploaded=True)
    path.unlink()
    with pytest.raises(CredentialFileError):
        await store.list_account_ids()
    reloaded = KimiPanelCredentialStore(owner, credential_root=tmp_path)
    await reloaded.synchronize(config["selected"], [], [], save)
    with pytest.raises(CredentialFileError):
        await reloaded.list_account_ids()
    assert not path.exists()


@pytest.mark.asyncio
async def test_changed_file_cannot_silently_replace_running_credentials(tmp_path):
    store, _, config, save = await panel_store(tmp_path)
    path = store.files.path(config["selected"][0], uploaded=True)
    changed = store.files.import_document(config["selected"][0])
    changed["refresh_token"] = "older-uploaded-token"
    path.write_text(json.dumps(changed), encoding="utf-8")
    await store.synchronize(config["selected"], [], [], save)
    with pytest.raises(CredentialFileError):
        await store.list_account_ids()
    private = store.files.read(store._managed_paths["test"])
    assert private["refresh_token"] == "test-refresh-secret"


@pytest.mark.asyncio
async def test_remove_save_then_upload_allows_explicit_replacement(tmp_path):
    store, _, config, save = await panel_store(tmp_path)
    ref = config["selected"][0]
    await store.synchronize([], [], [], save)
    changed = store.files.import_document(ref)
    changed["refresh_token"] = "new-user-uploaded-token"
    changed["local_credentials_path"] = "/do/not/write"
    store.files.path(ref, uploaded=True).write_text(json.dumps(changed), encoding="utf-8")
    await store.synchronize([ref], [], [], save)
    assert (await store.load_credentials("test"))["refresh_token"] == "new-user-uploaded-token"
    assert not (await store.load_credentials("test")).get("local_credentials_path")


@pytest.mark.asyncio
async def test_panel_write_failure_keeps_latest_token_and_recovers_on_reload(tmp_path, monkeypatch):
    store, owner, config, save = await panel_store(tmp_path)
    ref = config["selected"][0]
    original = store.files.write_panel
    def denied(*args, **kwargs):
        raise CredentialFileError("injected panel write failure")
    monkeypatch.setattr(store.files, "write_panel", denied)
    with pytest.raises(CredentialFileError):
        await store.save_refreshed_token("test", new_token(), device_id="device")
    assert store.files.read(store._managed_paths["test"])["refresh_token"] == "refresh-new"
    monkeypatch.setattr(store.files, "write_panel", original)
    reloaded = KimiPanelCredentialStore(owner, credential_root=tmp_path)
    await reloaded.synchronize(config["selected"], [], [], save)
    assert (await reloaded.load_credentials("test"))["refresh_token"] == "refresh-new"
    assert reloaded.files.import_document(ref)["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_deleted_panel_during_refresh_keeps_new_token_without_recreating(tmp_path):
    store, _, config, _ = await panel_store(tmp_path)
    path = store.files.path(config["selected"][0], uploaded=True)
    oauth = KimiOAuthClient(store)
    async def grant(token):
        path.unlink()
        return new_token()
    oauth.refresh_access_token = grant
    with pytest.raises(CredentialFileError):
        await oauth.ensure_fresh("test", force=True)
    assert not path.exists()
    assert store.files.read(store._managed_paths["test"])["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_new_login_can_recreate_a_deleted_panel_explicitly(tmp_path):
    store, _, config, _ = await panel_store(tmp_path)
    path = store.files.path(config["selected"][0], uploaded=True)
    path.unlink()
    await store.allocate_account_id("test", reserve=True)
    await store.save_login_token(new_token(), account_id="test", device_id="device", session_id="session")
    assert path.exists()
    assert (await store.load_credentials("test"))["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_config_replacement_waits_for_inflight_refresh(tmp_path):
    store, _, config, save = await panel_store(tmp_path)
    ref = config["selected"][0]
    oauth = KimiOAuthClient(store)
    entered, release = asyncio.Event(), asyncio.Event()
    async def grant(token):
        entered.set()
        await release.wait()
        return new_token()
    oauth.refresh_access_token = grant
    task = asyncio.create_task(oauth.ensure_fresh("test", force=True))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        await store.synchronize([], [], [], save)
        uploaded = store.files.import_document(ref)
        uploaded["refresh_token"] = "new-login-family"
        store.files.path(ref, uploaded=True).write_text(json.dumps(uploaded), encoding="utf-8")
        await store.synchronize([ref], [], [], save)
        assert "正在刷新" in store._selection_errors[ref]
    finally:
        release.set()
        with pytest.raises(CredentialFileError):
            await task
    assert store.files.read(store._managed_paths["test"])["refresh_token"] == "refresh-new"
    await store.synchronize([ref], [], [], save)
    assert (await store.load_credentials("test"))["refresh_token"] == "new-login-family"


@pytest.mark.asyncio
async def test_delete_after_open_does_not_resurrect_visible_file(tmp_path, monkeypatch):
    import os
    if os.name == "nt":
        pytest.skip("Windows 文件句柄阻止并发 unlink，此用例验证 POSIX 已删除句柄。")
    store, _, config, _ = await panel_store(tmp_path)
    target = store.files.path(config["selected"][0], uploaded=True)
    original = Path.open
    def delete_after_open(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        if path == target and mode == "r+b":
            path.unlink()
        return stream
    monkeypatch.setattr(Path, "open", delete_after_open)
    with pytest.raises(CredentialFileError):
        await store.save_refreshed_token("test", new_token(), device_id="device")
    assert not target.exists()
    assert store.files.read(store._managed_paths["test"])["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_disabled_selection_survives_reload_without_reenabling_legacy_paths(tmp_path):
    store, owner, config, save = await panel_store(tmp_path)
    old_private = list(store._managed_paths.values())
    await store.synchronize([], [], [], save)
    reloaded = KimiPanelCredentialStore(owner, credential_root=tmp_path)
    await reloaded.synchronize([], old_private, ["test"], save)
    assert await reloaded.list_accounts() == {}
    assert config["selected"] != old_private


def test_schema_has_one_visible_account_file_control_and_keeps_legacy_data(tmp_path):
    from astrbot.api import AstrBotConfig
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    items = schema["account_settings"]["items"]
    visible = {key: value for key, value in items.items() if not value.get("invisible")}
    assert set(visible) == {"monthly_cooldown_minutes", "credential_imports"}
    assert visible["credential_imports"]["type"] == "file"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"account_settings": {"account_files": ["credentials/old.json"], "account_ids": ["old"]}}), encoding="utf-8")
    config = AstrBotConfig(str(path), schema=schema)
    assert config["account_settings"]["account_files"] == ["credentials/old.json"]
    assert config["account_settings"]["account_ids"] == ["old"]
