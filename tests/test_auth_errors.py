"""Authentication failures stay distinct from service quota and permission errors."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_kimi_datasource_api.datasource import KimiDatasourceClient
from astrbot_plugin_kimi_datasource_api.models import (
    DatasourceHTTPError,
    OAuthError,
    OAuthUnauthorizedError,
    QuotaCooldownError,
)
from astrbot_plugin_kimi_datasource_api.moonshot import KimiMoonshotClient
from astrbot_plugin_kimi_datasource_api.oauth import KimiOAuthClient

QUOTA_ERROR = {
    "error": {
        "type": "access_terminated_error",
        "message": "You've reached your monthly usage limit for this billing cycle.",
    }
}


def make_client(kind, store, oauth):
    if kind == "datasource":
        return KimiDatasourceClient(store, oauth)
    return KimiMoonshotClient(store, oauth)


async def call_client(kind, client):
    if kind == "datasource":
        return await client.get_data_source_desc("stock_finance_data")
    if kind == "fetch":
        return await client.fetch_url(url="https://example.com")
    return await client.search(query="example")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["datasource", "search", "fetch"])
@pytest.mark.parametrize("body", [QUOTA_ERROR, {"error": {"type": "permission_denied", "message": "not allowed"}}])
async def test_403_never_refreshes_or_revokes(kind, body, store, http_responses):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    client = make_client(kind, store, oauth)
    client._local_fetch = AsyncMock(side_effect=AssertionError("403 must not trigger fallback"))
    http_responses.responses.append((403, body))
    monthly = body["error"]["type"] == "access_terminated_error"
    with pytest.raises(QuotaCooldownError if monthly else DatasourceHTTPError) as caught:
        await call_client(kind, client)
    if not monthly:
        assert caught.value.status == 403
        assert caught.value.error_type == body["error"]["type"]
    assert bool(await store.cooldown.get("test")) == monthly
    assert body["error"]["message"] in str(caught.value)
    oauth.ensure_fresh.assert_awaited_once_with("test", force=False)
    assert (await store.load_credentials("test"))["status"] == "valid"
    assert await store.list_account_ids(include_revoked=False) == ["test"]
    client._local_fetch.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["datasource", "search", "fetch"])
@pytest.mark.parametrize("outcome", ["success", "permission", "monthly", "auth"])
async def test_permission_403_rotates_without_refresh_or_revocation(kind, outcome, store, http_responses):
    accounts = await store.list_accounts()
    accounts["test2"] = {**accounts["test"], "device_id": "device-2"}
    await store._save_accounts(accounts)
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    client = make_client(kind, store, oauth)
    client._local_fetch = AsyncMock(side_effect=AssertionError("403 must not trigger fallback"))
    permission = {"error": {"type": "permission_denied", "message": "not allowed"}}
    http_responses.responses.append((403, permission))
    if outcome == "success":
        http_responses.responses.append((200, {}))
        await call_client(kind, client)
    elif outcome == "monthly":
        http_responses.responses.append((403, QUOTA_ERROR))
        with pytest.raises(QuotaCooldownError):
            await call_client(kind, client)
    else:
        http_responses.responses.extend([(403, permission)] if outcome == "permission" else [(401, {}), (401, {})])
        with pytest.raises(DatasourceHTTPError) as caught:
            await call_client(kind, client)
        assert caught.value.status == 403
        assert caught.value.error_type == "permission_denied"
        assert "not allowed" in str(caught.value)
    calls = oauth.ensure_fresh.await_args_list
    expected = [("test", False), ("test2", False)]
    if outcome == "auth":
        expected.append(("test2", True))
    assert [(call.args[0], call.kwargs["force"]) for call in calls] == expected
    assert (await store.load_credentials("test"))["status"] == "valid"
    assert (await store.load_credentials("test2"))["status"] == ("revoked" if outcome == "auth" else "valid")
    assert await store.cooldown.get("test") is None
    assert bool(await store.cooldown.get("test2")) == (outcome == "monthly")
    client._local_fetch.assert_not_awaited()



@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["datasource", "search"])
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_other_http_errors_do_not_revoke(kind, status, store, http_responses):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    http_responses.responses.append((status, {"error": {"message": "service busy"}}))
    with pytest.raises(DatasourceHTTPError, match="service busy"):
        await call_client(kind, make_client(kind, store, oauth))
    assert oauth.ensure_fresh.await_count == 1
    assert (await store.load_credentials("test"))["status"] == "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["datasource", "search", "fetch"])
@pytest.mark.parametrize("retry_status", [200, 401, 403])
async def test_401_retries_once_and_only_repeated_401_revokes(kind, retry_status, store, http_responses):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(side_effect=["old-access", "new-access"]))
    body = {"error": {"type": "invalid_authentication_error", "message": "token rejected"}}
    retry_body = QUOTA_ERROR if retry_status == 403 else body if retry_status == 401 else {}
    http_responses.responses.extend([(401, body), (retry_status, retry_body)])
    client = make_client(kind, store, oauth)
    if retry_status == 401:
        with pytest.raises(OAuthUnauthorizedError, match="token rejected"):
            await call_client(kind, client)
    elif retry_status == 403:
        with pytest.raises(QuotaCooldownError, match="月度额度已用尽"):
            await call_client(kind, client)
    else:
        await call_client(kind, client)
    assert [c.kwargs["force"] for c in oauth.ensure_fresh.await_args_list] == [False, True]
    assert (await store.load_credentials("test"))["status"] == ("revoked" if retry_status == 401 else "valid")


@pytest.mark.asyncio
async def test_fetch_keeps_existing_server_error_fallback(store, http_responses):
    oauth = SimpleNamespace(ensure_fresh=AsyncMock(return_value="test-access-secret"))
    client = KimiMoonshotClient(store, oauth)
    client._local_fetch = AsyncMock(return_value="fallback page")
    http_responses.responses.append((503, "temporarily unavailable"))
    assert "fallback page" in await client.fetch_url(url="https://example.com")
    client._local_fetch.assert_awaited_once_with("https://example.com")
    assert (await store.load_credentials("test"))["status"] == "valid"


def test_error_details_are_bounded_and_redacted():
    body = json.dumps({"error": {"type": "denied", "message": 'Bearer opaque-token access_token="abc" refresh_token=def eyJtoken.signature opaque-secret ' + "x" * 1000}})
    error = DatasourceHTTPError(403, body, secrets=("opaque-secret",))
    for text in (str(error), error.body, error.detail):
        for secret in ("opaque-token", "abc", "def", "eyJtoken.signature", "opaque-secret"):
            assert secret not in text
        assert len(text) < 700
    assert error.error_type == "denied"


@pytest.mark.parametrize("body,detail", [("not JSON", "not JSON"), ("", "empty response"), ('{"message":"gateway failed"}', "gateway failed"), ('{"error":"invalid_grant"}', "invalid_grant")])
def test_error_body_shapes(body, detail):
    assert detail in str(DatasourceHTTPError(403, body))


@pytest.mark.asyncio
async def test_revoked_account_needs_explicit_recovery(store):
    await store.mark_revoked("test")
    oauth = KimiOAuthClient(store)
    oauth.refresh_access_token = AsyncMock()
    with pytest.raises(OAuthUnauthorizedError):
        await oauth.ensure_fresh("test", force=True)
    oauth.refresh_access_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id,force", [(None, True), ("test", False)])
async def test_recovery_requires_account_id_and_force(store, account_id, force):
    with pytest.raises(OAuthError, match="显式指定"):
        await KimiOAuthClient(store).ensure_fresh(account_id, force=force, allow_revoked=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "invalid_grant", "network"])
async def test_explicit_recovery_commits_only_after_success(store, outcome):
    await store.mark_revoked("test")
    oauth = KimiOAuthClient(store)

    async def post(path, params):
        assert (await store.load_credentials("test"))["status"] == "revoked"
        assert params["refresh_token"] == "test-refresh-secret"
        if outcome == "network":
            raise OAuthError("network unavailable")
        if outcome == "invalid_grant":
            return 400, {"error": "invalid_grant"}
        return 200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900}

    oauth._post_form = post
    oauth.max_refresh_retries = 1
    if outcome == "success":
        assert await oauth.ensure_fresh("test", force=True, allow_revoked=True) == "new-access"
    else:
        with pytest.raises(OAuthError):
            await oauth.ensure_fresh("test", force=True, allow_revoked=True)
    credentials = await store.load_credentials("test")
    assert credentials["status"] == ("valid" if outcome == "success" else "revoked")
    assert credentials["refresh_token"] == ("new-refresh" if outcome == "success" else "test-refresh-secret")


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_recovery_of_imported_account_verifies_local_winner(store, tmp_path, success):
    local = tmp_path / "credentials" / "kimi-code.json"
    local.parent.mkdir()
    payload = {"access_token": "cli-access", "refresh_token": "cli-refresh", "expires_at": int(time.time()) + 900, "expires_in": 900}
    local.write_text(json.dumps(payload), encoding="utf-8")
    accounts = await store.list_accounts()
    accounts["test"].update(status="revoked", local_credentials_path=str(local))
    await store._save_accounts(accounts)
    oauth = KimiOAuthClient(store)

    async def post(path, params):
        assert (await store.load_credentials("test"))["status"] == "revoked"
        assert params["refresh_token"] == "cli-refresh"
        if not success:
            return 400, {"error": "invalid_grant"}
        return 200, {"access_token": "verified-access", "refresh_token": "verified-refresh", "expires_in": 900}

    oauth._post_form = AsyncMock(side_effect=post)
    if success:
        assert await oauth.ensure_fresh("test", force=True, allow_revoked=True) == "verified-access"
        assert json.loads(local.read_text())["refresh_token"] == "verified-refresh"
    else:
        with pytest.raises(OAuthUnauthorizedError):
            await oauth.ensure_fresh("test", force=True, allow_revoked=True)
        assert json.loads(local.read_text()) == payload
        assert (await store.load_credentials("test"))["status"] == "revoked"
    assert oauth._post_form.await_count == 1
    assert not (tmp_path / "oauth" / "kimi-code.lock").exists()
