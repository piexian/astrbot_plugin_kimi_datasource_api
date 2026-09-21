"""Monthly quota blocks survive usage reads, refreshes, reloads and concurrent probes."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_kimi_datasource_api.cooldown import KimiQuotaCooldown, MONTHLY_COOLDOWNS_KEY
from astrbot_plugin_kimi_datasource_api.datasource import KimiDatasourceClient
from astrbot_plugin_kimi_datasource_api.main import KimiDatasourcePlugin
from astrbot_plugin_kimi_datasource_api.models import DatasourceError, DatasourceHTTPError, QuotaCooldownError
from astrbot_plugin_kimi_datasource_api.moonshot import KimiMoonshotClient
from astrbot_plugin_kimi_datasource_api.oauth import KimiOAuthClient
from astrbot_plugin_kimi_datasource_api.storage import KimiCredentialStore
from astrbot_plugin_kimi_datasource_api.usage import KimiUsageClient

MONTHLY = {"error": {"type": "access_terminated_error", "message": "You've reached your monthly usage limit for this billing cycle."}}


def monthly_error():
    return DatasourceHTTPError(403, json.dumps(MONTHLY))


def clients(store):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    return oauth, KimiDatasourceClient(store, oauth), KimiMoonshotClient(store, oauth)


async def invoke(kind, datasource, moonshot):
    if kind == "datasource":
        return await datasource.get_data_source_desc("stock_finance_data")
    if kind == "search":
        return await moonshot.search(query="test")
    return await moonshot.fetch_url(url="https://example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["datasource", "search", "fetch"])
async def test_monthly_error_blocks_all_business_apis_without_refresh(initial, store, http_responses):
    oauth, datasource, moonshot = clients(store)
    moonshot._local_fetch = AsyncMock(side_effect=AssertionError("No fallback during quota cooldown"))
    http_responses.responses.append((403, MONTHLY))
    before = time.time()
    with pytest.raises(QuotaCooldownError, match="月度额度已用尽"):
        await invoke(initial, datasource, moonshot)
    record = await store.cooldown.get("test")
    assert before + 3600 <= record["retry_at"] <= time.time() + 3600
    assert (await store.load_credentials("test"))["status"] == "valid"
    for kind in ["datasource", "search", "fetch"]:
        with pytest.raises(QuotaCooldownError) as caught:
            await invoke(kind, datasource, moonshot)
        assert "下次允许复核" in str(caught.value)
        assert "非额度重置时间" in str(caught.value)
        assert "未登录" not in str(caught.value)
    assert len(http_responses.calls) == 1
    assert oauth.ensure_fresh.await_count == 1
    moonshot._local_fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["datasource", "search", "fetch"])
async def test_initial_monthly_error_rotates_to_healthy_account(kind, store, http_responses):
    accounts = await store.list_accounts()
    accounts["z-healthy"] = dict(accounts["test"])
    await store._save_accounts(accounts)
    oauth, datasource, moonshot = clients(store)
    http_responses.responses.extend([(403, MONTHLY), (200, {"result": "ok"})])
    await invoke(kind, datasource, moonshot)
    assert [call.args[0] for call in oauth.ensure_fresh.await_args_list] == ["test", "z-healthy"]
    assert await store.cooldown.get("test")
    assert await store.cooldown.get("z-healthy") is None
    assert all(a["status"] == "valid" for a in (await store.list_accounts()).values())


@pytest.mark.asyncio
async def test_cooling_account_skipped_before_oauth_or_http(store, http_responses):
    accounts = await store.list_accounts()
    accounts["z-healthy"] = dict(accounts["test"])
    await store._save_accounts(accounts)
    await store.cooldown.block("test", monthly_error())
    oauth, datasource, _ = clients(store)
    http_responses.responses.append((200, "OK"))
    assert "OK" in await datasource.get_data_source_desc("stock_finance_data")
    oauth.ensure_fresh.assert_awaited_once_with("z-healthy", force=False)
    assert len(http_responses.calls) == 1


@pytest.mark.asyncio
async def test_mixed_revocation_and_cooldown_is_not_reported_as_all_unauthorized(store, http_responses):
    accounts = await store.list_accounts()
    accounts["z-rejected"] = dict(accounts["test"])
    await store._save_accounts(accounts)
    await store.cooldown.block("test", monthly_error())
    _, datasource, _ = clients(store)
    http_responses.responses.extend([(401, "token rejected"), (401, "token rejected")])
    with pytest.raises(QuotaCooldownError) as caught:
        await datasource.get_data_source_desc("stock_finance_data")
    assert "账号 test 月度额度冷却中" in str(caught.value)
    assert "z-rejected" in str(caught.value)
    assert (await store.load_credentials("test"))["status"] == "valid"
    assert (await store.load_credentials("z-rejected"))["status"] == "revoked"


@pytest.mark.asyncio
async def test_cooldown_survives_reload_refresh_and_login(store):
    expected = await store.cooldown.block("test", monthly_error())
    reloaded = KimiCredentialStore(store.owner)
    assert await reloaded.cooldown.get("test") == expected
    oauth = KimiOAuthClient(reloaded)
    oauth._post_form = AsyncMock(return_value=(200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900}))
    await oauth.ensure_fresh("test", force=True, allow_revoked=True)
    assert await reloaded.cooldown.get("test") == expected
    token = await reloaded.load_token("test")
    await reloaded.save_login_token(token, account_id="test", device_id="test-device", session_id="test")
    assert await reloaded.cooldown.get("test") == expected


@pytest.mark.asyncio
async def test_usage_success_does_not_clear_monthly_error_and_status_prioritizes_it(store, http_responses):
    expected = await store.cooldown.block("test", monthly_error())
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    plugin = object.__new__(KimiDatasourcePlugin)
    plugin.store = store
    plugin.usage = KimiUsageClient(store, oauth)
    http_responses.responses.append((200, {"usages": {"limit_5h": {"used_ratio": 0}, "limit_7d": {"used_ratio": 0.889023}, "limit_month_total": {"used_ratio": 0}}}))
    output = await plugin._credential_status_text()
    assert "凭据=valid" in output
    assert "月度额度冷却中" in output
    assert "官方月度重置时间：未提供" in output
    assert "剩余 100.0%" in output and "剩余 11.1%" in output
    assert output.index("月度额度冷却中") < output.index("5 小时额度：")
    assert await store.cooldown.get("test") == expected


@pytest.mark.asyncio
async def test_expired_cooldown_status_does_not_itself_admit_probe(store):
    now = [time.time()]
    store.cooldown.clock = lambda: now[0]
    await store.cooldown.block("test", monthly_error())
    now[0] += 3601
    record = await store.cooldown.get("test")
    assert "待复核" in store.cooldown.describe("test", record)
    assert await store.cooldown.get("test") == record


@pytest.mark.asyncio
async def test_only_one_probe_admitted_even_when_probe_outlives_next_interval(store):
    now = [time.time()]
    store.cooldown.clock = lambda: now[0]
    await store.cooldown.block("test", monthly_error())
    now[0] += 3601
    _, datasource, moonshot = clients(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def post(*args, **kwargs):
        entered.set()
        await release.wait()
        return "OK", "request-id"

    datasource._post_json = AsyncMock(side_effect=post)
    probe = asyncio.create_task(datasource.get_data_source_desc("stock_finance_data"))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        now[0] += 3601
        for kind in ["datasource", "search", "fetch"]:
            with pytest.raises(QuotaCooldownError, match="复核正在进行"):
                await invoke(kind, datasource, moonshot)
    finally:
        release.set()
        await probe
    datasource._post_json.assert_awaited_once()
    assert await store.cooldown.get("test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["monthly", "server", "network", "tool_error"])
async def test_failed_probe_preserves_cooldown_and_never_falls_back(store, http_responses, failure):
    now = [time.time()]
    store.cooldown.clock = lambda: now[0]
    await store.cooldown.block("test", monthly_error())
    now[0] += 3601
    _, datasource, moonshot = clients(store)
    moonshot._local_fetch = AsyncMock(side_effect=AssertionError("No fallback during probe"))
    response = {"monthly": (403, MONTHLY), "server": (503, "service unavailable"), "tool_error": (200, {"is_success": False, "error": {"assistant": [{"type": "text", "text": "tool failed"}]}})}
    if failure == "network":
        datasource._post_json = AsyncMock(side_effect=DatasourceError("network failed"))
    else:
        http_responses.responses.append(response[failure])
    with pytest.raises(QuotaCooldownError):
        if failure == "server":
            await moonshot.fetch_url(url="https://example.com")
        else:
            await datasource.get_data_source_desc("stock_finance_data")
    record = await store.cooldown.get("test")
    assert record["retry_at"] == now[0] + 3600
    assert (await store.load_credentials("test"))["status"] == "valid"
    moonshot._local_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_probe_keeps_persisted_retry_time(store):
    now = [time.time()]
    store.cooldown.clock = lambda: now[0]
    await store.cooldown.block("test", monthly_error())
    now[0] += 3601
    with pytest.raises(asyncio.CancelledError):
        async with store.cooldown.request("test"):
            raise asyncio.CancelledError()
    assert not store.cooldown._inflight
    assert (await store.cooldown.get("test"))["retry_at"] == now[0] + 3600
    with pytest.raises(QuotaCooldownError):
        async with KimiQuotaCooldown(store.owner, clock=lambda: now[0]).request("test"):
            pytest.fail("A reload must not bypass the persisted probe interval")


@pytest.mark.asyncio
@pytest.mark.parametrize("was_probe", [False, True])
async def test_late_success_does_not_erase_new_monthly_rejection(store, was_probe):
    now = [time.time()]
    store.cooldown.clock = lambda: now[0]
    if was_probe:
        await store.cooldown.block("test", monthly_error())
        now[0] += 3601
    async with store.cooldown.request("test"):
        latest = await store.cooldown.block("test", monthly_error())
    assert await store.cooldown.get("test") == latest


@pytest.mark.asyncio
async def test_parallel_account_updates_do_not_lose_records(store):
    await asyncio.gather(*(store.cooldown.block(str(i), monthly_error()) for i in range(10)))
    assert len(await store.owner.get_kv_data(MONTHLY_COOLDOWNS_KEY, {})) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["single", "batch", "all"])
async def test_logout_removes_cooldown(store, method):
    await store.cooldown.block("test", monthly_error())
    if method == "single":
        await store.delete_account("test")
    elif method == "batch":
        await store.delete_accounts(["test"])
    else:
        await store.delete_credentials()
    assert await store.cooldown.get("test") is None


@pytest.mark.parametrize("status,error_type,message,expected", [(403, "access_terminated_error", "monthly usage limit", True), (403, "access_terminated_error", "subscription expired", False), (403, "permission_denied", "monthly usage limit", False), (401, "access_terminated_error", "monthly usage limit", False), (429, "rate_limit_error", "try again", False)])
def test_monthly_classification_is_narrow(status, error_type, message, expected):
    assert DatasourceHTTPError(status, json.dumps({"error": {"type": error_type, "message": message}})).is_monthly_quota is expected


@pytest.mark.parametrize("minutes,seconds", [(60, 3600), (30, 1800), (0, 60), (2000, 86400)])
def test_configured_interval_is_bounded(store, minutes, seconds):
    assert KimiCredentialStore(store.owner, cooldown_minutes=minutes).cooldown.seconds == seconds


def test_configuration_default_and_schema_agree():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["account_settings"]["items"]["monthly_cooldown_minutes"]["default"] == 60
    plugin = object.__new__(KimiDatasourcePlugin)
    plugin.config = {}
    assert plugin._cfg("monthly_cooldown_minutes") == 60
    plugin.config = {"account_settings": {"monthly_cooldown_minutes": 120}}
    assert plugin._cfg("monthly_cooldown_minutes") == 120
