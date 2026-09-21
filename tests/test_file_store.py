"""受管文件、旧 KV 迁移和刷新竞争的离线回归。"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from pathlib import Path

import pytest

from conftest import MemoryOwner
from astrbot_plugin_kimi_datasource_api.credential_files import CredentialFileError, CredentialFiles, credential_document, fingerprint
from astrbot_plugin_kimi_datasource_api.cooldown import MONTHLY_COOLDOWNS_KEY
from astrbot_plugin_kimi_datasource_api.models import TokenInfo
from astrbot_plugin_kimi_datasource_api.oauth import KimiOAuthClient
from astrbot_plugin_kimi_datasource_api.storage import ACCOUNTS_KEY, ACCOUNT_CONFIG_SNAPSHOT_KEY, FILE_STORE_KEY, KimiCredentialStore


def new_token(suffix="new"):
    return TokenInfo(f"access-{suffix}", f"refresh-{suffix}", int(time.time()) + 900, 900, "Bearer", "kimi-code")


async def migrated(tmp_path, owner=None):
    owner = owner or MemoryOwner()
    store = KimiCredentialStore(owner, credential_root=tmp_path)
    config = {"paths": []}
    async def publish(paths):
        config["paths"] = list(paths)
    await store.initialize_files([], ["test"], publish)
    return store, owner, config, publish


@pytest.mark.asyncio
async def test_legacy_migration_preserves_credentials_status_cli_and_cooldown(tmp_path):
    owner = MemoryOwner()
    owner.data[ACCOUNTS_KEY]["test"].update(status="revoked", local_credentials_path="original-cli.json", last_refresh_at="original")
    owner.data[MONTHLY_COOLDOWNS_KEY] = {"test": {"retry_at": time.time() + 3600, "generation": "keep"}}
    expected = copy.deepcopy(owner.data[ACCOUNTS_KEY]["test"])
    store, owner, config, _ = await migrated(tmp_path, owner)
    assert ACCOUNTS_KEY not in owner.data
    assert owner.data[MONTHLY_COOLDOWNS_KEY]["test"]["generation"] == "keep"
    document = await store.load_credentials("test")
    assert all(document[key] == value for key, value in expected.items())
    assert document["device_id"] == "test-device"
    assert document["account_id"] == "test"
    assert (tmp_path / config["paths"][0]).is_file()
    assert await store.list_account_ids(include_revoked=False) == []
    if os.name != "nt":
        assert (tmp_path / config["paths"][0]).stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_explicit_legacy_removals_are_not_resurrected(tmp_path):
    owner = MemoryOwner()
    owner.data[ACCOUNT_CONFIG_SNAPSHOT_KEY] = ["test"]
    store = KimiCredentialStore(owner, credential_root=tmp_path)
    async def publish(paths):
        assert paths == []
    await store.initialize_files([], [], publish)
    assert await store.list_accounts() == {}
    assert ACCOUNTS_KEY not in owner.data


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["file", "config", "commit", "cleanup"])
async def test_migration_recovers_each_failure_without_relogin(tmp_path, monkeypatch, failure):
    owner = MemoryOwner()
    original = copy.deepcopy(owner.data[ACCOUNTS_KEY])
    store = KimiCredentialStore(owner, credential_root=tmp_path)
    config = {"paths": []}
    fail_once = True
    original_write = store.files.write
    original_put = owner.put_kv_data
    original_delete = owner.delete_kv_data
    def write(*args, **kwargs):
        nonlocal fail_once
        if failure == "file" and fail_once:
            fail_once = False
            raise CredentialFileError("injected write failure")
        return original_write(*args, **kwargs)
    async def publish(paths):
        nonlocal fail_once
        if failure == "config" and fail_once:
            fail_once = False
            raise CredentialFileError("injected config failure")
        config["paths"] = list(paths)
    async def put(key, value):
        nonlocal fail_once
        if failure == "commit" and key == FILE_STORE_KEY and value.get("phase") == "files" and fail_once:
            fail_once = False
            raise CredentialFileError("injected commit failure")
        await original_put(key, value)
    async def delete(key):
        nonlocal fail_once
        if failure == "cleanup" and key == ACCOUNTS_KEY and fail_once:
            fail_once = False
            raise CredentialFileError("injected cleanup failure")
        await original_delete(key)
    monkeypatch.setattr(store.files, "write", write)
    monkeypatch.setattr(owner, "put_kv_data", put)
    monkeypatch.setattr(owner, "delete_kv_data", delete)
    with pytest.raises(CredentialFileError, match="injected"):
        await store.initialize_files([], ["test"], publish)
    assert owner.data[ACCOUNTS_KEY] == original
    await store.initialize_files(config["paths"], ["test"], publish)
    assert (await store.load_credentials("test"))["refresh_token"] == original["test"]["refresh_token"]
    assert ACCOUNTS_KEY not in owner.data
    assert owner.data[FILE_STORE_KEY]["cleanup_pending"] is False
    # 重载不会重新导入旧 token，也不会意外停用现有路径。
    fresh = KimiCredentialStore(owner, credential_root=tmp_path)
    await fresh.initialize_files(config["paths"], [], publish)
    assert await fresh.list_account_ids() == ["test"]


@pytest.mark.asyncio
async def test_prepared_migration_adopts_changed_legacy_source_safely(tmp_path, monkeypatch):
    owner = MemoryOwner()
    store = KimiCredentialStore(owner, credential_root=tmp_path)
    original = store.files.write
    changed = False
    def write(*args, **kwargs):
        nonlocal changed
        original(*args, **kwargs)
        if not changed:
            owner.data[ACCOUNTS_KEY]["test"]["refresh_token"] = "newer-legacy-token"
            changed = True
    monkeypatch.setattr(store.files, "write", write)
    async def publish(paths):
        pass
    with pytest.raises(CredentialFileError, match="旧凭据发生变更"):
        await store.initialize_files([], ["test"], publish)
    await store.initialize_files([], ["test"], publish)
    assert (await store.load_credentials("test"))["refresh_token"] == "newer-legacy-token"


@pytest.mark.asyncio
async def test_existing_independent_file_is_not_overwritten(tmp_path):
    owner = MemoryOwner()
    store = KimiCredentialStore(owner, credential_root=tmp_path)
    relative = store.files.filename("test")
    data = {**owner.data[ACCOUNTS_KEY]["test"], "device_id": "other", "refresh_token": "independent"}
    store.files.write(relative, "test", data)
    before = (tmp_path / relative).read_bytes()
    async def publish(paths):
        pytest.fail("must not switch configuration")
    with pytest.raises(CredentialFileError, match="已有不同凭据"):
        await store.initialize_files([], ["test"], publish)
    assert (tmp_path / relative).read_bytes() == before
    assert ACCOUNTS_KEY in owner.data


@pytest.mark.asyncio
async def test_disabling_keeps_file_but_explicit_logout_deletes_only_managed_copy(tmp_path):
    store, owner, config, publish = await migrated(tmp_path)
    relative = config["paths"][0]
    cli = tmp_path / "cli.json"
    cli.write_text("untouched", encoding="utf-8")
    document = await store.load_credentials("test")
    document["local_credentials_path"] = str(cli)
    async with store._lock:
        await store._put("test", document)
    await store.initialize_files([], [], publish)
    assert await store.list_accounts() == {}
    assert store.inactive_files() == {"test": relative}
    assert (tmp_path / relative).is_file()
    assert await store.delete_account("test")
    assert not (tmp_path / relative).exists()
    assert cli.read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing", "json", "duplicate"])
async def test_broken_file_fails_closed_without_legacy_fallback(tmp_path, broken):
    store, owner, config, _ = await migrated(tmp_path)
    target = tmp_path / config["paths"][0]
    if broken == "missing":
        target.unlink()
    elif broken == "json":
        target.write_text("{broken", encoding="utf-8")
    else:
        target.write_text('{"refresh_token":"one","refresh_token":"two"}', encoding="utf-8")
    owner.data[ACCOUNTS_KEY] = MemoryOwner().data[ACCOUNTS_KEY]
    with pytest.raises(CredentialFileError):
        await store.list_account_ids()
    assert "test-access-secret" not in str(store.file_errors)


@pytest.mark.asyncio
async def test_refresh_delete_race_does_not_recreate_file(tmp_path):
    store, _, config, _ = await migrated(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    oauth = KimiOAuthClient(store)
    async def grant(refresh):
        entered.set()
        await release.wait()
        return new_token()
    oauth.refresh_access_token = grant
    refresher = asyncio.create_task(oauth.ensure_fresh("test", force=True))
    await entered.wait()
    deletion = asyncio.create_task(store.delete_account("test"))
    await asyncio.sleep(0)
    assert not deletion.done()
    release.set()
    await asyncio.gather(refresher, deletion)
    assert not (tmp_path / config["paths"][0]).exists()
    assert await store.list_accounts() == {}


@pytest.mark.asyncio
async def test_refresh_after_config_disable_updates_file_without_reactivation(tmp_path):
    store, _, config, publish = await migrated(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    oauth = KimiOAuthClient(store)
    async def grant(refresh):
        entered.set()
        await release.wait()
        return new_token()
    oauth.refresh_access_token = grant
    refresher = asyncio.create_task(oauth.ensure_fresh("test", force=True))
    await entered.wait()
    await store.initialize_files([], [], publish)
    release.set()
    await refresher
    assert await store.list_accounts() == {}
    assert store.files.read(config["paths"][0])["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_refresh_journal_recovers_failed_rename_without_using_old_token(tmp_path, monkeypatch):
    store, _, config, _ = await migrated(tmp_path)
    target = tmp_path / config["paths"][0]
    original = store.files._atomic
    failed = False
    def atomic(path, data):
        nonlocal failed
        if path == target and not failed:
            failed = True
            raise PermissionError("injected")
        original(path, data)
    monkeypatch.setattr(store.files, "_atomic", atomic)
    with pytest.raises(CredentialFileError):
        await store.save_refreshed_token("test", new_token(), device_id="test-device")
    assert list(target.parent.glob("*.pending"))
    # 模拟重载：新 store 不依赖进程内缓存，也能恢复新 refresh_token。
    recovered = KimiCredentialStore(store.owner, credential_root=tmp_path)
    async def publish(paths):
        pass
    await recovered.initialize_files(config["paths"], [], publish)
    assert (await recovered.load_credentials("test"))["refresh_token"] == "refresh-new"
    assert not list(target.parent.glob("*.pending"))


@pytest.mark.asyncio
async def test_memory_retains_refresh_if_journal_cannot_be_written(tmp_path, monkeypatch):
    store, _, _, _ = await migrated(tmp_path)
    original = store.files._atomic
    def denied(*args):
        raise PermissionError("injected")
    monkeypatch.setattr(store.files, "_atomic", denied)
    with pytest.raises(CredentialFileError):
        await store.save_refreshed_token("test", new_token(), device_id="test-device")
    monkeypatch.setattr(store.files, "_atomic", original)
    assert (await store.load_credentials("test"))["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_upload_retry_does_not_reimport_stale_token(tmp_path):
    store, _, config, publish = await migrated(tmp_path)
    relative = "files/account_settings/credential_imports/uploaded.json"
    upload = store.files.path(relative, uploaded=True)
    upload.parent.mkdir(parents=True)
    original = {**MemoryOwner().data[ACCOUNTS_KEY]["test"], "account_id": "uploaded", "device_id": "upload-device", "local_credentials_path": "/do/not/touch"}
    upload.write_text(json.dumps(original), encoding="utf-8")
    async def failed_config(paths):
        raise CredentialFileError("config failed")
    with pytest.raises(CredentialFileError):
        await store.import_file(relative, failed_config)
    await store.save_refreshed_token("uploaded", new_token("rotated"), device_id="upload-device")
    await store.import_file(relative, publish)
    document = await store.load_credentials("uploaded")
    assert document["refresh_token"] == "refresh-rotated"
    assert not document.get("local_credentials_path")
    assert store.credential_path("uploaded") in config["paths"]


@pytest.mark.asyncio
async def test_upload_cannot_overwrite_existing_account(tmp_path):
    store, _, _, publish = await migrated(tmp_path)
    relative = "files/account_settings/credential_imports/test.json"
    upload = store.files.path(relative, uploaded=True)
    upload.parent.mkdir(parents=True)
    upload.write_text(json.dumps({**MemoryOwner().data[ACCOUNTS_KEY]["test"], "device_id": "other"}), encoding="utf-8")
    with pytest.raises(CredentialFileError, match="已存在"):
        await store.import_file(relative, publish)
    assert (await store.load_credentials("test"))["device_id"] == "test-device"


@pytest.mark.parametrize("relative", ["../secret.json", "/tmp/secret.json", "C:/secret.json", "credentials/../secret.json", "credentials/a:stream.json", "credentials/a.txt", "credentials\\a.json"])
def test_paths_are_confined(tmp_path, relative):
    with pytest.raises(CredentialFileError):
        CredentialFiles(tmp_path).path(relative)


def test_symlink_is_rejected(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    try:
        (root / "credentials").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not supported")
    with pytest.raises(CredentialFileError, match="符号链接"):
        CredentialFiles(root).path("credentials/test.json")


def test_compare_and_swap_preserves_independent_edit_and_new_token_journal(tmp_path):
    repo = CredentialFiles(tmp_path)
    path = repo.filename("test")
    first = credential_document("test", {**MemoryOwner().data[ACCOUNTS_KEY]["test"], "device_id": "test-device"})
    repo.write(path, "test", first)
    expected = fingerprint(first)
    edited = {**first, "note": "independent change"}
    repo.write(path, "test", edited)
    new = {**first, "refresh_token": "rotated-secret"}
    with pytest.raises(CredentialFileError, match="未覆盖"):
        repo.write(path, "test", new, expected=expected)
    assert json.loads((tmp_path / path).read_text(encoding="utf-8"))["note"] == "independent change"
    with pytest.raises(CredentialFileError, match="冲突"):
        repo.read(path)
    assert "rotated-secret" in next((tmp_path / "credentials").glob("*.pending")).read_text(encoding="utf-8")


@pytest.mark.parametrize("field,value", [("status", {}), ("schema_version", True), ("expires_at", float("inf")), ("expires_in", True), ("device_id", "bad\r\nheader"), ("scope", [])])
def test_malformed_fields_are_controlled_errors(field, value):
    data = {**MemoryOwner().data[ACCOUNTS_KEY]["test"], "device_id": "test-device", field: value}
    with pytest.raises(CredentialFileError):
        credential_document("test", data)


@pytest.mark.asyncio
async def test_shutdown_waits_for_refresh_and_blocks_late_writes(tmp_path):
    from astrbot_plugin_kimi_datasource_api.models import OAuthError
    store, _, config, _ = await migrated(tmp_path)
    oauth = KimiOAuthClient(store)
    entered, release = asyncio.Event(), asyncio.Event()
    async def grant(refresh):
        entered.set()
        await release.wait()
        return new_token()
    oauth.refresh_access_token = grant
    refresh = asyncio.create_task(oauth.ensure_fresh("test", force=True))
    await entered.wait()
    closing = asyncio.create_task(oauth.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.gather(refresh, closing)
    assert store.files.read(config["paths"][0])["refresh_token"] == "refresh-new"
    await store.close()
    with pytest.raises(OAuthError, match="停止"):
        await oauth.ensure_fresh("test", force=True)
    with pytest.raises(CredentialFileError, match="停止"):
        await store.mark_revoked("test")
    assert store.files.read(config["paths"][0])["status"] == "valid"


@pytest.mark.parametrize("name,environment,allowed", [
    ("ordinary.json", None, True),
    ("kimi-code-env-ffffffffffffffff.json", None, False),
    ("ordinary.json", {"oauth_host": "https://auth.invalid", "base_url": "https://api.invalid/v1"}, False),
])
def test_upload_checks_environment(tmp_path, name, environment, allowed):
    repo = CredentialFiles(tmp_path)
    relative = "files/account_settings/credential_imports/" + name
    target = repo.path(relative, uploaded=True)
    target.parent.mkdir(parents=True)
    document = {**MemoryOwner().data[ACCOUNTS_KEY]["test"], "account_id": "test", "device_id": "device"}
    if environment is not None:
        document["environment"] = environment
    target.write_text(json.dumps(document), encoding="utf-8")
    if allowed:
        assert repo.import_document(relative)["account_id"] == "test"
    else:
        with pytest.raises(CredentialFileError, match="环境"):
            repo.import_document(relative)


def test_matching_custom_cli_environment_is_accepted(tmp_path):
    from astrbot_plugin_kimi_datasource_api.credential_files import env_credential_filename
    environment = {"oauth_host": "https://auth.invalid", "base_url": "https://api.invalid/v1"}
    repo = CredentialFiles(tmp_path, environment=environment)
    relative = "files/account_settings/credential_imports/" + env_credential_filename(**environment)
    target = repo.path(relative, uploaded=True)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({**MemoryOwner().data[ACCOUNTS_KEY]["test"], "device_id": "device"}), encoding="utf-8")
    assert repo.import_document(relative)["environment"] == environment


def test_updated_upload_is_not_deleted(tmp_path):
    repo = CredentialFiles(tmp_path)
    relative = "files/account_settings/credential_imports/test.json"
    path = repo.path(relative, uploaded=True)
    path.parent.mkdir(parents=True)
    document = {**MemoryOwner().data[ACCOUNTS_KEY]["test"], "device_id": "device"}
    path.write_text(json.dumps(document), encoding="utf-8")
    expected = fingerprint(repo.import_document(relative))
    document["refresh_token"] = "new-uploaded-token"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CredentialFileError, match="已更新"):
        repo.remove_import(relative, expected=expected)
    assert path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["datasource", "moonshot"])
@pytest.mark.parametrize("all_broken", [False, True])
async def test_file_failure_rotates_without_misreporting_auth(tmp_path, kind, all_broken):
    from astrbot_plugin_kimi_datasource_api.datasource import KimiDatasourceClient
    from astrbot_plugin_kimi_datasource_api.moonshot import KimiMoonshotClient
    store, _, _, _ = await migrated(tmp_path)
    await store.save_login_token(new_token("other"), account_id="other", device_id="device", session_id="test")
    async def select(ids):
        return "test"
    store.next_account_id = select
    seen = []
    async def post(*args, account_id, **kwargs):
        seen.append(account_id)
        if account_id == "test" or all_broken:
            raise CredentialFileError("file unavailable")
        return ("ok", "request-id") if kind == "datasource" else "ok"
    if kind == "datasource":
        client = KimiDatasourceClient(store, KimiOAuthClient(store))
        client._post_json = post
        async def call():
            return (await client.call_kimi_tool("get_data_source_desc", {"name": "fred"})).text
    else:
        client = KimiMoonshotClient(store, KimiOAuthClient(store))
        client._post = post
        async def call():
            return await client._post_with_rotation("https://api.invalid", {}, expect_json=False)
    if all_broken:
        with pytest.raises(CredentialFileError, match="无可用凭据文件"):
            await call()
    else:
        assert (await call()).startswith("ok")
    assert seen == ["test", "other"]
    assert (await store.load_credentials("test"))["status"] == "valid"


@pytest.mark.asyncio
async def test_cancelled_refresh_waits_for_rotated_token_persistence(tmp_path):
    store, _, _, _ = await migrated(tmp_path)
    oauth = KimiOAuthClient(store)
    entered, release = asyncio.Event(), asyncio.Event()
    async def grant(refresh):
        entered.set()
        await release.wait()
        return new_token()
    oauth.refresh_access_token = grant
    task = asyncio.create_task(oauth.ensure_fresh("test", force=True))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.load_credentials("test"))["refresh_token"] == "refresh-new"
