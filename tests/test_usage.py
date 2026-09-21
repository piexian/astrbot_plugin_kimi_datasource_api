"""Quota queries and status output tolerate missing fields and partial failures."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from astrbot_plugin_kimi_datasource_api.main import KimiDatasourcePlugin
from astrbot_plugin_kimi_datasource_api.models import DatasourceError, DatasourceHTTPError, OAuthUnauthorizedError
from astrbot_plugin_kimi_datasource_api.oauth import KimiOAuthClient
from astrbot_plugin_kimi_datasource_api.usage import KimiUsageClient, format_reset_time, format_usage, used_ratio
from astrbot_plugin_kimi_datasource_api.constants import PLUGIN_VERSION

def test_release_metadata_and_changelog_match():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert f"version: v{PLUGIN_VERSION}" in (root / "metadata.yaml").read_text(encoding="utf-8")
    headings = [line for line in (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines() if line.startswith("## ")]
    assert headings[0] == f"## v{PLUGIN_VERSION}"


USAGES = {
    "usages": {
        "limit_5h": {"used_ratio": 0, "reset_time": "2026-09-21T03:26:57Z"},
        "limit_7d": {"used_ratio": 0.889023, "reset_time": "2026-09-23T06:26:57Z"},
    }
}


def plugin_for(store):
    plugin = object.__new__(KimiDatasourcePlugin)
    plugin.store = store
    plugin.config = {"account_settings": {"account_ids": ["test"]}}
    plugin.oauth = KimiOAuthClient(store)
    plugin.usage = KimiUsageClient(store, plugin.oauth)
    return plugin


@pytest.mark.asyncio
async def test_usage_request_contract(store, http_responses):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    client = KimiUsageClient(store, oauth, base_url="https://api.kimi.ai/coding/v1/", proxy="http://proxy.invalid", timeout_seconds=60)
    http_responses.responses.append((200, USAGES))
    assert await client.get_usage("test") == USAGES
    method, url, kwargs = http_responses.calls[0]
    assert method == "GET"
    assert url == "https://api.kimi.ai/coding/v1/usages"
    assert kwargs["headers"]["Authorization"] == "Bearer test-access-secret"
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["proxy"] == "http://proxy.invalid"
    assert kwargs["allow_redirects"] is False
    assert http_responses.sessions[0]["timeout"].total == 10
    oauth.ensure_fresh.assert_awaited_once_with("test", force=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 403, 404, 429, 500])
async def test_usage_failure_does_not_revoke_or_refresh(store, http_responses, status):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    http_responses.responses.append((status, {"error": {"message": "service rejected test-access-secret"}}))
    with pytest.raises(DatasourceHTTPError) as caught:
        await KimiUsageClient(store, oauth).get_usage("test")
    assert "test-access-secret" not in str(caught.value)
    assert oauth.ensure_fresh.await_count == 1
    assert (await store.load_credentials("test"))["status"] == "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_status", [200, 401, 403])
async def test_usage_401_retries_without_marking_account_revoked(store, http_responses, retry_status):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(side_effect=["old-access", "new-access"]))
    http_responses.responses.extend([(401, "unauthorized"), (retry_status, USAGES)])
    client = KimiUsageClient(store, oauth)
    if retry_status == 200:
        assert await client.get_usage("test") == USAGES
    else:
        with pytest.raises(DatasourceHTTPError):
            await client.get_usage("test")
    assert [c.kwargs["force"] for c in oauth.ensure_fresh.await_args_list] == [False, True]
    assert (await store.load_credentials("test"))["status"] == "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["not json", "null", "[]", '"string"'])
async def test_usage_rejects_malformed_responses(store, http_responses, body):
    http_responses.responses.append((200, body))
    with pytest.raises(DatasourceError, match="无效"):
        await KimiUsageClient(store, KimiOAuthClient(store)).get_usage("test")
    assert (await store.load_credentials("test"))["status"] == "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.TimeoutError(), aiohttp.ClientConnectionError("secret proxy credentials")])
async def test_usage_network_errors_are_safe(store, http_responses, error):
    http_responses.responses.append(error)
    with pytest.raises(DatasourceError) as caught:
        await KimiUsageClient(store, KimiOAuthClient(store)).get_usage("test")
    assert "secret proxy credentials" not in str(caught.value)
    assert (await store.load_credentials("test"))["status"] == "valid"


def test_usage_format_uses_actual_windows_only():
    output = "\n".join(format_usage(USAGES))
    assert "5 小时额度：已用 0.0%，剩余 100.0%" in output
    assert "7 天额度：已用 88.9%，剩余 11.1%" in output
    assert "月度总额度：未知" in output
    assert "月度代码额度：未知" in output
    assert "不代表仍可用" in output
    assert "2026-09-23" in output


def test_all_quota_windows_and_overage():
    data = {"usages": {key: {"used_ratio": "1.25"} for key in ["limit_5h", "limit_7d", "limit_month_total", "limit_month_code"]}}
    lines = format_usage(data)
    assert len(lines) == 4
    assert all("已用 125.0%，剩余 0.0%" in line and "重置 未知" in line for line in lines)


@pytest.mark.parametrize("value", [None, True, False, {}, [], "nan", "inf", "-inf", -0.01, "bad", 10**400, 1e308])
def test_invalid_ratios_are_unknown(value):
    assert used_ratio(value) is None
    output = "\n".join(format_usage({"usages": {"limit_month_total": {"used_ratio": value}}}))
    assert "月度总额度：未知" in output


@pytest.mark.parametrize("data", [{}, {"usages": None}, {"usages": []}, {"usages": {"limit_5h": "bad"}}])
def test_missing_usage_is_not_reported_as_zero(data):
    assert "已用" not in "\n".join(format_usage(data))


@pytest.mark.parametrize("value", [None, 0, "not-date", "2026-09-21T03:26:57", "99999-01-01T00:00:00Z"])
def test_invalid_reset_is_unknown(value):
    assert format_reset_time(value) == "未知"


def test_reset_time_has_explicit_local_offset():
    timestamp = "2026-09-23T06:26:57+00:00"
    assert format_reset_time(timestamp) == datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


@pytest.mark.asyncio
async def test_status_partial_failure_and_revoked_skip(store):
    accounts = await store.list_accounts()
    accounts["failed"] = dict(accounts["test"])
    accounts["revoked"] = {**accounts["test"], "status": "revoked"}
    await store._save_accounts(accounts)
    plugin = plugin_for(store)

    async def get_usage(account_id):
        if account_id == "failed":
            raise DatasourceError("额度查询超时")
        assert account_id == "test"
        return USAGES

    plugin.usage.get_usage = AsyncMock(side_effect=get_usage)
    output = await plugin._credential_status_text()
    assert "failed: 凭据=valid" in output
    assert "额度查询失败：额度查询超时" in output
    assert "test: 凭据=valid" in output and "88.9%" in output
    assert "revoked: 凭据=revoked" in output
    assert "kimi refresh revoked" in output
    assert plugin.usage.get_usage.await_count == 2
    assert "test-access-secret" not in output and "test-refresh-secret" not in output


@pytest.mark.asyncio
async def test_status_reflects_refresh_rejection(store):
    plugin = plugin_for(store)

    async def get_usage(account_id):
        await store.mark_revoked(account_id)
        raise OAuthUnauthorizedError("refresh rejected")

    plugin.usage.get_usage = get_usage
    output = await plugin._credential_status_text()
    assert "test: 凭据=revoked" in output
    assert "额度查询失败：refresh rejected" in output


@pytest.mark.asyncio
async def test_status_queries_at_most_three_accounts_concurrently(store):
    accounts = await store.list_accounts()
    await store._save_accounts({str(i): dict(accounts["test"]) for i in range(8)})
    plugin = plugin_for(store)
    active = peak = 0

    async def get_usage(account_id):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1
        return USAGES

    plugin.usage.get_usage = get_usage
    output = await plugin._credential_status_text()
    assert peak == 3
    assert output.count("凭据=valid") == 8


@pytest.mark.asyncio
async def test_no_accounts_status_does_not_call_api(store):
    await store.delete_credentials()
    plugin = plugin_for(store)
    plugin.usage.get_usage = AsyncMock()
    assert "未登录" in await plugin._credential_status_text()
    plugin.usage.get_usage.assert_not_awaited()


def test_usage_endpoint_tracks_datasource_region_not_search_override(store, monkeypatch):
    plugin = plugin_for(store)
    plugin.config["datasource_settings"] = {"api_url": "https://api.kimi.ai/coding/v1/tools/"}
    monkeypatch.setenv("KIMI_WEB_SEARCH_BASE_URL", "https://search.invalid")
    assert plugin._build_usage_client().url == "https://api.kimi.ai/coding/v1/usages"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_refresh_command_only_recovers_explicit_account(store, explicit):
    accounts = await store.list_accounts()
    accounts["revoked"] = {**accounts["test"], "status": "revoked"}
    await store._save_accounts(accounts)
    plugin = plugin_for(store)
    plugin.config["account_settings"]["account_ids"] = ["test", "revoked"]
    plugin.oauth.ensure_fresh = AsyncMock(return_value="refreshed-access")
    command = "/kimi refresh revoked" if explicit else "/kimi refresh"
    event = SimpleNamespace(get_message_str=lambda: command, plain_result=lambda text: text)
    output = [result async for result in plugin.kimi_refresh(event)]
    if explicit:
        plugin.oauth.ensure_fresh.assert_awaited_once_with("revoked", force=True, allow_revoked=True)
        assert "额度不会因刷新而重置" in output[0]
    else:
        plugin.oauth.ensure_fresh.assert_awaited_once_with("test", force=True)
