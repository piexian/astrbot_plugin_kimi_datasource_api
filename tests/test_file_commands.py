"""文件配置、登录输入和指定账号刷新命令的回归。"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import MemoryOwner
from astrbot_plugin_kimi_datasource_api import main as plugin_module
from astrbot_plugin_kimi_datasource_api.credential_files import CredentialFileError
from astrbot_plugin_kimi_datasource_api.models import DeviceAuthorization, DevicePollResult, OAuthUnauthorizedError
from astrbot_plugin_kimi_datasource_api.storage import ACCOUNTS_KEY
from astrbot_plugin_kimi_datasource_api.panel_credentials import KimiPanelCredentialStore
from astrbot.core.utils.session_waiter import SessionWaiter, USER_SESSIONS
from test_file_store import new_token


class Config(dict):
    def __init__(self):
        super().__init__(account_settings={"account_ids": ["test"], "account_files": [], "credential_imports": []})
        self.fail = False
        self.saved = None

    def save_config(self):
        if self.fail:
            raise OSError("injected config save failure")
        self.saved = copy.deepcopy(dict(self))


async def make_plugin(tmp_path, monkeypatch, *, initialize=True):
    monkeypatch.setattr(plugin_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    context = SimpleNamespace(add_llm_tools=lambda *args: None, send_message=AsyncMock())
    config = Config()
    plugin = plugin_module.KimiDatasourcePlugin(context, config)
    owner = MemoryOwner()
    plugin.store = KimiPanelCredentialStore(owner, credential_root=plugin._plugin_data_dir())
    plugin.oauth = plugin._build_oauth_client()
    plugin.datasource = plugin._build_datasource_client()
    plugin.moonshot = plugin._build_moonshot_client()
    plugin.usage = plugin._build_usage_client()
    if initialize:
        await plugin.initialize()
    return plugin, owner, config


def event(text, sender="admin"):
    return SimpleNamespace(
        get_message_str=lambda: text, get_sender_id=lambda: sender,
        unified_msg_origin="test:private:one", plain_result=lambda text: text, send=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_plugin_initialization_migrates_old_credentials_and_clears_legacy_ids(tmp_path, monkeypatch):
    plugin, owner, config = await make_plugin(tmp_path, monkeypatch)
    assert ACCOUNTS_KEY not in owner.data
    assert config["account_settings"]["account_ids"] == []
    paths = config["account_settings"]["credential_imports"]
    assert len(paths) == 1
    assert paths[0].startswith("files/account_settings/credential_imports/")
    assert plugin.store.files.path(paths[0], uploaded=True).is_file()
    assert (await plugin.store.load_credentials("test"))["access_token"] == "test-access-secret"
    await plugin.terminate()


@pytest.mark.asyncio
async def test_config_failure_keeps_original_data_and_can_retry(tmp_path, monkeypatch):
    plugin, owner, config = await make_plugin(tmp_path, monkeypatch, initialize=False)
    original = copy.deepcopy(owner.data[ACCOUNTS_KEY])
    config.fail = True
    with pytest.raises(CredentialFileError, match="配置保存失败"):
        await plugin.initialize()
    assert owner.data[ACCOUNTS_KEY] == original
    assert config["account_settings"]["account_ids"] == ["test"]
    assert config["account_settings"]["account_files"] == []
    config.fail = False
    await plugin.initialize()
    assert await plugin.store.list_account_ids() == ["test"]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_native_files_remain_selected_and_bad_input_is_isolated(tmp_path, monkeypatch):
    plugin, owner, config = await make_plugin(tmp_path, monkeypatch)
    good = "files/account_settings/credential_imports/imported.json"
    bad = "files/account_settings/credential_imports/bad.json"
    document = {**MemoryOwner().data[ACCOUNTS_KEY]["test"], "device_id": "import-device"}
    good_file = plugin.store.files.path(good, uploaded=True)
    good_file.parent.mkdir(parents=True, exist_ok=True)
    good_file.write_text(json.dumps(document), encoding="utf-8")
    bad_file = plugin.store.files.path(bad, uploaded=True)
    bad_file.write_text("[]", encoding="utf-8")
    config["account_settings"]["credential_imports"].extend([bad, good])
    await plugin._sync_config_accounts()
    assert bad in config["account_settings"]["credential_imports"]
    assert good in config["account_settings"]["credential_imports"]
    assert good_file.exists() and bad_file.exists()
    assert await plugin.store.list_account_ids() == ["test", "imported"]
    assert bad in plugin.store.file_errors
    assert ACCOUNTS_KEY not in owner.data
    await plugin.terminate()


@pytest.mark.asyncio
async def test_config_removal_disables_but_logout_deletes_file(tmp_path, monkeypatch):
    plugin, _, config = await make_plugin(tmp_path, monkeypatch)
    relative = config["account_settings"]["credential_imports"][0]
    path = plugin.store.files.path(relative, uploaded=True)
    config["account_settings"]["credential_imports"] = []
    await plugin._sync_config_accounts()
    assert path.exists()
    assert await plugin.store.list_accounts() == {}
    status = await plugin._credential_status_text()
    assert relative in status and "无启用账号" in status
    result = [text async for text in plugin.kimi_logout(event("/kimi logout test"))]
    assert "已删除" in result[0]
    assert not path.exists()
    await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["22", "22号", "22 14:26", ""])
async def test_refresh_command_accepts_optional_monthly_input(tmp_path, monkeypatch, argument):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    plugin.oauth.refresh_access_token = AsyncMock(return_value=new_token())
    output = [text async for text in plugin.kimi_refresh(event(f"/kimi refresh test {argument}"))]
    assert "刷新成功" in output[0]
    rule = await plugin.store.get_monthly_reset("test")
    if argument:
        assert rule["day"] == 22
        assert rule["time"] == ("14:26:00" if ":" in argument else None)
    else:
        assert rule is None
    await plugin.terminate()


@pytest.mark.asyncio
async def test_bad_monthly_input_does_not_consume_refresh_token(tmp_path, monkeypatch):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    plugin.oauth.refresh_access_token = AsyncMock()
    output = [text async for text in plugin.kimi_refresh(event("/kimi refresh test 32"))]
    assert "格式无效" in output[0]
    plugin.oauth.refresh_access_token.assert_not_awaited()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_rejected_refresh_does_not_write_requested_binding(tmp_path, monkeypatch):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    plugin.oauth.refresh_access_token = AsyncMock(side_effect=OAuthUnauthorizedError("invalid grant"))
    output = [text async for text in plugin.kimi_refresh(event("/kimi refresh test 22"))]
    assert "失败" in output[0]
    assert await plugin.store.get_monthly_reset("test") is None
    assert (await plugin.store.load_credentials("test"))["status"] == "revoked"
    await plugin.terminate()


async def start_login(plugin, command):
    plugin.oauth.request_device_authorization = AsyncMock(return_value=DeviceAuthorization("CODE", "fake-device-code", "https://auth.invalid", "https://auth.invalid/complete", 900, 5))
    plugin.oauth.poll_device_token = AsyncMock(return_value=DevicePollResult("success", token=new_token("login")))
    output = [text async for text in plugin.kimi_login(event(command))]
    assert output
    pending = plugin.pending_logins.get("test:private:one")
    await asyncio.wait_for(pending.poll_task, 3)
    await asyncio.sleep(0)
    return pending


@pytest.mark.asyncio
async def test_login_argument_saves_rule_without_waiting_for_input(tmp_path, monkeypatch):
    plugin, _, config = await make_plugin(tmp_path, monkeypatch)
    pending = await start_login(plugin, "/kimi login new-account 22")
    assert pending.credentials_saved and pending.state == "saved"
    assert plugin.pending_logins.get(pending.session_id) is None
    assert (await plugin.store.get_monthly_reset("new-account"))["time"] is None
    assert plugin.store.credential_path("new-account") in config["account_settings"]["credential_imports"]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_login_prompt_only_accepts_initiator_and_keeps_credentials_on_skip(tmp_path, monkeypatch):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    pending = await start_login(plugin, "/kimi login new-account")
    assert pending.state == "awaiting_reset" and pending.credentials_saved
    assert await plugin.store.load_token("new-account") is not None
    key, waiter = next((key, value) for key, value in USER_SESSIONS.items() if value.session_controller is pending.session_controller)
    assert waiter.session_filter.filter(event("22", "intruder")) != key
    await SessionWaiter.trigger(key, event("22", "intruder"))
    assert await plugin.store.get_monthly_reset("new-account") is None
    await SessionWaiter.trigger(key, event("skip"))
    assert plugin.pending_logins.get(pending.session_id) is None
    assert await plugin.store.load_token("new-account") is not None
    await plugin.terminate()
    if pending.waiter_task:
        await asyncio.gather(pending.waiter_task, return_exceptions=True)
    assert key not in USER_SESSIONS


@pytest.mark.asyncio
async def test_login_prompt_accepts_day_only(tmp_path, monkeypatch):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    pending = await start_login(plugin, "/kimi login new-account")
    key = next(key for key, waiter in USER_SESSIONS.items() if waiter.session_controller is pending.session_controller)
    await SessionWaiter.trigger(key, event("22号"))
    rule = await plugin.store.get_monthly_reset("new-account")
    assert rule["day"] == 22 and rule["time"] is None
    assert pending.state == "saved"
    await plugin.terminate()
    await asyncio.gather(pending.waiter_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_login_prompt_timeout_preserves_credentials(tmp_path, monkeypatch):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    pending = await start_login(plugin, "/kimi login new-account")
    pending.session_controller.stop(TimeoutError("test timeout"))
    if pending.session_controller.current_event:
        pending.session_controller.current_event.set()
    await asyncio.wait_for(pending.waiter_task, 3)
    assert plugin.pending_logins.get(pending.session_id) is None
    assert await plugin.store.load_token("new-account") is not None
    await plugin.terminate()


@pytest.mark.asyncio
async def test_status_lists_file_and_day_precision_without_raw_tokens(tmp_path, monkeypatch):
    plugin, _, config = await make_plugin(tmp_path, monkeypatch)
    from astrbot_plugin_kimi_datasource_api.monthly import parse_monthly_reset
    await plugin.store.set_monthly_reset("test", parse_monthly_reset("22"))
    plugin.usage.get_usage = AsyncMock(return_value={})
    output = await plugin._credential_status_text()
    assert config["account_settings"]["credential_imports"][0] in output
    assert "具体时刻未知" in output
    assert "test-access-secret" not in output and "test-refresh-secret" not in output
    await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["test", "--all"])
async def test_logout_does_not_reimport_queued_same_account(tmp_path, monkeypatch, target):
    plugin, _, config = await make_plugin(tmp_path, monkeypatch)
    relative = "files/account_settings/credential_imports/test.json"
    path = plugin.store.files.path(relative, uploaded=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**MemoryOwner().data[ACCOUNTS_KEY]["test"], "device_id": "device"}), encoding="utf-8")
    config["account_settings"]["credential_imports"] = [relative]
    output = [text async for text in plugin.kimi_logout(event(f"/kimi logout {target}"))]
    assert "已删除" in output[0]
    await plugin._sync_config_accounts()
    assert await plugin.store.list_accounts() == {}
    assert config["account_settings"]["credential_imports"] == []
    await plugin.terminate()


@pytest.mark.asyncio
async def test_logout_cancels_pending_login_from_another_session(tmp_path, monkeypatch):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    plugin.oauth.request_device_authorization = AsyncMock(return_value=DeviceAuthorization("CODE", "fake-code", "https://auth.invalid", "https://auth.invalid/complete", 900, 5))
    release = asyncio.Event()
    async def polling(code):
        await release.wait()
        return DevicePollResult("success", token=new_token())
    plugin.oauth.poll_device_token = polling
    [text async for text in plugin.kimi_login(event("/kimi login pending-account"))]
    pending = plugin.pending_logins.get("test:private:one")
    logout = event("/kimi logout pending-account")
    logout.unified_msg_origin = "test:private:other"
    [text async for text in plugin.kimi_logout(logout)]
    release.set()
    assert plugin.pending_logins.get(pending.session_id) is None
    assert await plugin.store.load_credentials("pending-account") is None
    assert "pending-account" not in plugin.store._reserved
    await plugin.terminate()


@pytest.mark.asyncio
async def test_logout_disables_duplicate_file_aliases(tmp_path, monkeypatch):
    plugin, _, config = await make_plugin(tmp_path, monkeypatch)
    original = config["account_settings"]["credential_imports"][0]
    alias = "files/account_settings/credential_imports/alias.json"
    plugin.store.files.path(alias, uploaded=True).write_bytes(plugin.store.files.path(original, uploaded=True).read_bytes())
    config["account_settings"]["credential_imports"].append(alias)
    [text async for text in plugin.kimi_logout(event("/kimi logout test"))]
    await plugin._sync_config_accounts()
    assert await plugin.store.list_accounts() == {}
    assert config["account_settings"]["credential_imports"] == []
    await plugin.terminate()


@pytest.mark.asyncio
async def test_cancelled_device_request_releases_account_reservation(tmp_path, monkeypatch):
    plugin, _, _ = await make_plugin(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def device():
        entered.set()
        await release.wait()
    plugin.oauth.request_device_authorization = device
    async def command():
        return [text async for text in plugin.kimi_login(event("/kimi login pending-account"))]
    task = asyncio.create_task(command())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "pending-account" not in plugin.store._reserved
    await plugin.terminate()


def test_help_matches_file_configuration_and_monthly_arguments():
    plugin = plugin_module.KimiDatasourcePlugin.__new__(plugin_module.KimiDatasourcePlugin)
    help_text = plugin._help_text()
    assert "kimi login [账号ID] [月重置时间]" in help_text
    assert "kimi refresh [账号ID] [月重置时间]" in help_text
    assert "凭据文件列表就是启用列表" in help_text
    assert "account_files" not in help_text
    assert "account_ids" not in help_text
