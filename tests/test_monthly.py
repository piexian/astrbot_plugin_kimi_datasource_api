"""月刷新日精度、日历边界和冷却优先级的回归。"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from astrbot_plugin_kimi_datasource_api.models import DatasourceHTTPError, QuotaCooldownError, ToolInputError
from astrbot_plugin_kimi_datasource_api.monthly import advance_monthly_reset, describe_monthly_reset, parse_monthly_reset, reset_probe_boundary, validate_monthly_reset
from astrbot_plugin_kimi_datasource_api.oauth import KimiOAuthClient
from astrbot_plugin_kimi_datasource_api.usage import KimiUsageClient
from test_file_store import migrated, new_token

NOW = datetime(2026, 9, 21, 4, tzinfo=timezone.utc)
ERROR = DatasourceHTTPError(403, json.dumps({"error": {"type": "access_terminated_error", "message": "You've reached your monthly usage limit"}}))


@pytest.mark.parametrize("text", ["22", "22日", "22号", "每月22日", "每月 22号"])
def test_day_only_never_claims_midnight_reset(text):
    rule = parse_monthly_reset(text, now=NOW)
    assert rule["day"] == 22
    assert rule["time"] is None
    assert rule["precision"] == "date"
    assert rule["next_date"] == "2026-09-22"
    description = describe_monthly_reset(rule)
    assert "具体时刻未知" in description
    assert "00:00" not in description
    assert datetime.fromtimestamp(reset_probe_boundary(rule), timezone.utc).isoformat() == "2026-09-21T16:00:00+00:00"


@pytest.mark.parametrize("text", ["2026-09-22T06:26:58Z", "2026-09-22 14:26:58 +08:00", "22 14:26:58"])
def test_explicit_time_retains_precision_and_offset(text):
    rule = parse_monthly_reset(text, now=NOW)
    assert rule["precision"] == "second"
    assert reset_probe_boundary(rule) == datetime(2026, 9, 22, 6, 26, 58, tzinfo=timezone.utc).timestamp()
    assert "人工绑定" in describe_monthly_reset(rule)


@pytest.mark.parametrize("text", ["0", "32", "22 25:00", "22 14:99", "2026-02-30", "not-a-date", "x" * 100, "2026-09-22T14:26:00+19:00"])
def test_invalid_input_is_rejected(text):
    with pytest.raises(ToolInputError):
        parse_monthly_reset(text, now=NOW)


@pytest.mark.parametrize("text", ["", "skip", "SKIP", "跳过"])
def test_optional_input_is_none(text):
    assert parse_monthly_reset(text, now=NOW) is None


def test_day_only_on_reset_day_remains_today():
    rule = parse_monthly_reset("22", now=datetime(2026, 9, 22, 2, tzinfo=timezone.utc))
    assert rule["next_date"] == "2026-09-22"
    assert rule["time"] is None


def test_calendar_months_clamp_without_losing_original_day():
    january = datetime(2028, 1, 31, 16, tzinfo=timezone.utc)
    rule = parse_monthly_reset("31", now=january)
    assert rule["next_date"] == "2028-02-29"
    assert rule["day"] == 31
    updated = advance_monthly_reset(rule, now=datetime(2028, 2, 29, 8, tzinfo=timezone.utc).timestamp())
    assert updated["next_date"] == "2028-03-31"
    assert updated["day"] == 31


def test_year_boundary():
    rule = parse_monthly_reset("1", now=datetime(2026, 12, 31, 4, tzinfo=timezone.utc))
    assert rule["next_date"] == "2027-01-01"


@pytest.mark.parametrize("rule", [[], {"day": True}, {"day": 22, "next_date": "2026-09-23"}, {"day": 22, "next_date": "2026-09-22", "time": "bad"}])
def test_invalid_stored_rule_is_rejected(rule):
    with pytest.raises(ToolInputError):
        validate_monthly_reset(rule)


@pytest.mark.asyncio
async def test_confirmed_monthly_error_waits_for_bound_date_even_with_weekly_quota(tmp_path, http_responses):
    store, _, _, _ = await migrated(tmp_path)
    clock = [NOW.timestamp()]
    store.cooldown.clock = lambda: clock[0]
    rule = parse_monthly_reset("22", now=NOW)
    await store.set_monthly_reset("test", rule)
    with pytest.raises(QuotaCooldownError):
        async with store.cooldown.request("test"):
            raise ERROR
    blocked = await store.cooldown.get("test")
    assert blocked["retry_at"] == reset_probe_boundary(rule)
    http_responses.responses.append((200, {"usages": {"limit_5h": {"used_ratio": 0}, "limit_7d": {"used_ratio": 0.2}}}))
    usage = KimiUsageClient(store, KimiOAuthClient(store))
    await usage.get_usage("test")
    assert await store.cooldown.get("test") == blocked
    with pytest.raises(QuotaCooldownError, match="具体时刻未知"):
        async with store.cooldown.request("test"):
            pytest.fail("must not make a business request before bound date")


@pytest.mark.asyncio
async def test_regular_success_on_bound_day_does_not_assume_monthly_reset(tmp_path):
    store, _, _, _ = await migrated(tmp_path)
    now = datetime(2026, 9, 22, 1, tzinfo=timezone.utc)
    store.cooldown.clock = lambda: now.timestamp()
    rule = parse_monthly_reset("22", now=now)
    await store.set_monthly_reset("test", rule)
    async with store.cooldown.request("test"):
        pass
    assert (await store.get_monthly_reset("test"))["next_date"] == "2026-09-22"
    with pytest.raises(QuotaCooldownError):
        async with store.cooldown.request("test"):
            raise ERROR
    assert (await store.cooldown.get("test"))["retry_at"] == now.timestamp() + 3600


@pytest.mark.asyncio
async def test_forgotten_probe_cannot_advance_binding(tmp_path):
    store, _, _, _ = await migrated(tmp_path)
    clock = [datetime(2026, 9, 22, 1, tzinfo=timezone.utc).timestamp()]
    store.cooldown.clock = lambda: clock[0]
    rule = parse_monthly_reset("22", now=datetime.fromtimestamp(clock[0], timezone.utc))
    await store.set_monthly_reset("test", rule)
    with pytest.raises(QuotaCooldownError):
        async with store.cooldown.request("test"):
            raise ERROR
    clock[0] += 3600
    async with store.cooldown.request("test"):
        await store.cooldown.forget(["test"])
    assert (await store.get_monthly_reset("test"))["next_date"] == "2026-09-22"


@pytest.mark.asyncio
async def test_bound_reset_is_not_cleared_by_refresh_and_retries_hourly_after_due(tmp_path):
    store, _, _, _ = await migrated(tmp_path)
    clock = [NOW.timestamp()]
    store.cooldown.clock = lambda: clock[0]
    rule = parse_monthly_reset("2026-09-22 14:26:58 +08:00", now=NOW)
    await store.set_monthly_reset("test", rule)
    with pytest.raises(QuotaCooldownError):
        async with store.cooldown.request("test"):
            raise ERROR
    blocked = await store.cooldown.get("test")
    assert blocked["retry_at"] == reset_probe_boundary(rule)
    oauth = KimiOAuthClient(store)
    async def grant(refresh):
        return new_token()
    oauth.refresh_access_token = grant
    await oauth.ensure_fresh("test", force=True, allow_revoked=True)
    assert await store.cooldown.get("test") == blocked
    clock[0] = blocked["retry_at"]
    with pytest.raises(QuotaCooldownError):
        async with store.cooldown.request("test"):
            raise ERROR
    assert (await store.cooldown.get("test"))["retry_at"] == clock[0] + 3600
    assert (await store.get_monthly_reset("test"))["next_date"] == "2026-09-22"
    clock[0] += 3600
    async with store.cooldown.request("test"):
        pass
    assert await store.cooldown.get("test") is None
    assert (await store.get_monthly_reset("test"))["next_date"] == "2026-10-22"


@pytest.mark.asyncio
async def test_explicit_refresh_persists_day_only_with_tokens_and_preserves_on_omission(tmp_path):
    store, _, config, _ = await migrated(tmp_path)
    oauth = KimiOAuthClient(store)
    async def grant(refresh):
        return new_token()
    oauth.refresh_access_token = grant
    rule = parse_monthly_reset("22", now=NOW)
    await oauth.ensure_fresh("test", force=True, allow_revoked=True, monthly_reset=rule)
    document = store.files.read(config["paths"][0])
    assert document["refresh_token"] == "refresh-new"
    assert document["monthly_reset"] == rule
    await oauth.ensure_fresh("test", force=True, allow_revoked=True)
    assert (await store.get_monthly_reset("test")) == rule


@pytest.mark.asyncio
async def test_file_binding_recovers_when_cooldown_kv_update_failed(tmp_path, monkeypatch):
    store, owner, _, _ = await migrated(tmp_path)
    store.cooldown.clock = lambda: NOW.timestamp()
    await store.cooldown.block("test", ERROR)
    original = owner.put_kv_data
    async def failed(key, value):
        if key.endswith("monthly_cooldowns"):
            raise OSError("injected KV failure")
        await original(key, value)
    monkeypatch.setattr(owner, "put_kv_data", failed)
    rule = parse_monthly_reset("22", now=NOW)
    with pytest.raises(OSError):
        await store.set_monthly_reset("test", rule)
    monkeypatch.setattr(owner, "put_kv_data", original)
    with pytest.raises(QuotaCooldownError):
        async with store.cooldown.request("test"):
            pytest.fail("must wait for recovered binding")
    assert (await store.cooldown.get("test"))["retry_at"] == reset_probe_boundary(rule)


@pytest.mark.parametrize("text", ["3000-01-01", "0001-01-01", "9998-01-01"])
def test_dates_outside_persistent_cooldown_range_are_rejected(text):
    with pytest.raises(ToolInputError):
        parse_monthly_reset(text, now=NOW)
