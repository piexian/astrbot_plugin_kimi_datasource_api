"""开发期自检：本机凭证共存 + 响应通道优先级（无需 AstrBot 运行时）。

用法: /root/work/.venv/bin/python tests/test_local_sync.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_kimi_datasource_api.datasource import extract_text
from astrbot_plugin_kimi_datasource_api.models import (
    DatasourceError,
    OAuthError,
    OAuthUnauthorizedError,
    TokenInfo,
)
from astrbot_plugin_kimi_datasource_api.oauth import KimiOAuthClient
from astrbot_plugin_kimi_datasource_api.storage import KimiCredentialStore

FAILURES: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        FAILURES.append(label)


class FakeOwner:
    def __init__(self) -> None:
        self.kv: dict[str, object] = {}

    async def put_kv_data(self, key: str, value) -> None:
        self.kv[key] = value

    async def get_kv_data(self, key: str, default=None):
        return self.kv.get(key, default)

    async def delete_kv_data(self, key: str) -> None:
        self.kv.pop(key, None)


def token(access: str, refresh: str, ttl: int = 900) -> TokenInfo:
    now = int(time.time())
    return TokenInfo(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + ttl,
        expires_in=ttl,
        token_type="Bearer",
        scope="kimi-code",
    )


def new_store(tmp: Path, *, ttl: int = 900) -> tuple[KimiCredentialStore, Path, TokenInfo]:
    cred_file = tmp / "credentials" / "kimi-code.json"
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    base = token("acc-old", "ref-old", ttl)
    cred_file.write_text(
        json.dumps(
            {
                "access_token": base.access_token,
                "refresh_token": base.refresh_token,
                "expires_at": base.expires_at,
                "expires_in": base.expires_in,
                "scope": base.scope,
                "token_type": base.token_type,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    store = KimiCredentialStore(FakeOwner())
    return store, cred_file, base


async def scenario_rotate_and_write_back(tmp: Path) -> None:
    store, cred_file, base = new_store(tmp, ttl=1)
    client = KimiOAuthClient(store)
    calls = {"n": 0}

    async def fake_post_form(path, params):
        calls["n"] += 1
        return 200, {
            "access_token": "acc-new",
            "refresh_token": "ref-new",
            "expires_in": 900,
            "scope": "kimi-code",
            "token_type": "Bearer",
        }

    client._post_form = fake_post_form  # type: ignore[assignment]
    await store.save_login_token(
        base, account_id="local", device_id="dev", session_id="s",
        local_credentials_path=str(cred_file),
    )
    got = await client.ensure_fresh("local", force=True)
    saved = json.loads(cred_file.read_text(encoding="utf-8"))
    check("刷新后返回新 access_token", got == "acc-new")
    check("refresh 只发一次请求", calls["n"] == 1)
    check("原子回写本机凭证", saved["refresh_token"] == "ref-new" and saved["access_token"] == "acc-new")
    check("回写后无临时文件残留", not list(cred_file.parent.glob("*.tmp*")))
    check("锁目录已释放", not (tmp / "oauth" / "kimi-code.lock").exists())
    creds = await store.load_credentials("local")
    check("账号保留来源路径", creds.get("local_credentials_path") == str(cred_file))


async def scenario_adopt_local_winner(tmp: Path) -> None:
    store, cred_file, base = new_store(tmp, ttl=1)
    client = KimiOAuthClient(store)

    async def boom(path, params):
        raise AssertionError("CLI 已轮换时不应再发 refresh")

    client._post_form = boom  # type: ignore[assignment]
    await store.save_login_token(
        base, account_id="local", device_id="dev", session_id="s",
        local_credentials_path=str(cred_file),
    )
    cred_file.write_text(
        json.dumps(
            {
                "access_token": "acc-cli",
                "refresh_token": "ref-cli",
                "expires_at": int(time.time()) + 900,
                "expires_in": 900,
                "scope": "kimi-code",
                "token_type": "Bearer",
            }
        ),
        encoding="utf-8",
    )
    got = await client.ensure_fresh("local")
    check("CLI 已轮换且新 token 未过期时不再 refresh", got == "acc-cli")
    creds = await store.load_credentials("local")
    check("账号池同步为 CLI 的新 refresh_token", creds.get("refresh_token") == "ref-cli")


async def scenario_invalid_grant_adopts_local(tmp: Path) -> None:
    store, cred_file, base = new_store(tmp, ttl=1)
    client = KimiOAuthClient(store)

    async def denied(path, params):
        # 请求发出的同时，同机 CLI 抢先轮换并落盘
        cred_file.write_text(
            json.dumps(
                {
                    "access_token": "acc-cli2",
                    "refresh_token": "ref-cli2",
                    "expires_at": int(time.time()) + 900,
                    "expires_in": 900,
                    "scope": "kimi-code",
                    "token_type": "Bearer",
                }
            ),
            encoding="utf-8",
        )
        return 400, {"error": "invalid_grant", "error_description": "refresh token reused"}

    client._post_form = denied  # type: ignore[assignment]
    await store.save_login_token(
        base, account_id="local", device_id="dev", session_id="s",
        local_credentials_path=str(cred_file),
    )
    got = await client.ensure_fresh("local")
    creds = await store.load_credentials("local")
    check("refresh 被拒后采纳本机新 token", got == "acc-cli2")
    check("账号未被标记 revoked", creds.get("status") == "valid")


async def scenario_no_revocation_when_file_untouched(tmp: Path) -> None:
    store, cred_file, base = new_store(tmp, ttl=1)
    client = KimiOAuthClient(store)

    async def denied(path, params):
        return 400, {"error": "invalid_grant"}

    client._post_form = denied  # type: ignore[assignment]
    await store.save_login_token(
        base, account_id="local", device_id="dev", session_id="s",
        local_credentials_path=str(cred_file),
    )
    try:
        await client.ensure_fresh("local", force=True)
        check("来源文件未轮换时仍按吊销处理", False)
    except OAuthUnauthorizedError:
        creds = await store.load_credentials("local")
        check("来源文件未轮换时仍按吊销处理", creds.get("status") == "revoked")


async def scenario_refresh_without_rotation(tmp: Path) -> None:
    store, cred_file, base = new_store(tmp, ttl=1)
    client = KimiOAuthClient(store)

    async def same(path, params):
        return 200, {"access_token": "acc-next", "expires_in": 900, "scope": "kimi-code", "token_type": "Bearer"}

    client._post_form = same  # type: ignore[assignment]
    await store.save_login_token(
        base, account_id="local", device_id="dev", session_id="s",
        local_credentials_path=str(cred_file),
    )
    got = await client.ensure_fresh("local", force=True)
    creds = await store.load_credentials("local")
    saved = json.loads(cred_file.read_text(encoding="utf-8"))
    check("响应缺少 refresh_token 时沿用旧值", got == "acc-next" and creds.get("refresh_token") == "ref-old")
    check("回写保留未轮换的 refresh_token", saved["refresh_token"] == "ref-old")


async def scenario_lock_blocks(tmp: Path) -> None:
    from astrbot_plugin_kimi_datasource_api import local_credentials

    store, cred_file, base = new_store(tmp, ttl=1)
    client = KimiOAuthClient(store)
    await store.save_login_token(
        base, account_id="local", device_id="dev", session_id="s",
        local_credentials_path=str(cred_file),
    )

    async def held(self) -> bool:
        return False

    original = local_credentials.CredentialRefreshLock.acquire
    local_credentials.CredentialRefreshLock.acquire = held  # type: ignore[method-assign]
    try:
        try:
            await client.ensure_fresh("local", force=True)
            check("拿不到刷新锁时不发 refresh", False)
        except OAuthError as exc:
            check("拿不到刷新锁时不发 refresh", "刷新锁" in str(exc))
    finally:
        local_credentials.CredentialRefreshLock.acquire = original  # type: ignore[method-assign]


def channel_scenarios() -> None:
    both = {
        "is_success": True,
        "result": {
            "assistant": [{"type": "text", "text": "A"}],
            "user": [{"type": "text", "text": "U"}],
        },
    }
    empty_user = {
        "is_success": True,
        "result": {
            "assistant": [{"type": "text", "text": "caixin 正文"}],
            "user": [{"type": "text", "text": '{"data_preview": ""}'}],
        },
    }
    only_user = {
        "is_success": True,
        "result": {"user": [{"type": "text", "text": "U"}]},
    }
    err = {
        "is_success": False,
        "error": {
            "assistant": [{"type": "text", "text": "EMPTY_DATA - x"}],
            "user": [{"type": "text", "text": "EMPTY_DATA - x"}],
        },
    }
    check("official 优先 assistant 通道", extract_text(both) == "A")
    check("caixin 空 data_preview 不再丢正文", extract_text(empty_user) == "caixin 正文")
    check("缺 assistant 时回退 user", extract_text(only_user) == "U")
    check("bare 字符串直接透传", extract_text("# fred\n文档") == "# fred\n文档")
    check("legacy_zip 保持 user 优先", extract_text(both, mode="legacy_zip") == "U")
    try:
        extract_text(err)
        check("错误通道优先 assistant", False)
    except DatasourceError as exc:
        check("错误通道优先 assistant", "EMPTY_DATA" in str(exc))
    check(
        "空白 assistant 视为缺失",
        extract_text({
            "is_success": True,
            "result": {
                "assistant": [{"type": "text", "text": "   "}],
                "user": [{"type": "text", "text": "U"}],
            },
        })
        == "U",
    )


def header_scenarios(tmp: Path) -> None:
    import os

    from astrbot_plugin_kimi_datasource_api import identity, local_credentials
    from astrbot_plugin_kimi_datasource_api.constants import KIMI_CODE_CLI_VERSION, KIMI_DATASOURCE_VERSION

    saved_env = dict(os.environ)
    try:
        for key in list(os.environ):
            if key.startswith("KIMI_MSH_") or key == "KIMI_DISABLE_OAUTH_LOCK":
                os.environ.pop(key)

        oauth_headers = identity.oauth_device_headers("dev-1", KIMI_CODE_CLI_VERSION)
        check("OAuth 请求带产品 UA", oauth_headers.get("User-Agent") == f"kimi-code-cli/{KIMI_CODE_CLI_VERSION}")
        check("OAuth 版本位跟随 CLI", oauth_headers.get("X-Msh-Version") == KIMI_CODE_CLI_VERSION)

        ds_headers = identity.datasource_headers("tok", "dev-1", KIMI_DATASOURCE_VERSION, tool_call_id="tc")
        check("datasource UA 保持插件版本位", ds_headers.get("User-Agent") == f"kimi-datasource/{KIMI_DATASOURCE_VERSION}")
        check("datasource 默认设备头齐全", all(k in ds_headers for k in ("X-Msh-Device-Name", "X-Msh-Device-Model", "X-Msh-Os-Version", "X-Msh-Device-Id")))

        os.environ["KIMI_MSH_DEVICE_NAME"] = "env-name"
        os.environ["KIMI_MSH_DEVICE_MODEL"] = "env-model"
        os.environ["KIMI_MSH_OS_VERSION"] = "env-os"
        ds_env = identity.datasource_headers("tok", "dev-1")
        check("datasource 设备头支持 env 覆盖", (ds_env["X-Msh-Device-Name"], ds_env["X-Msh-Device-Model"], ds_env["X-Msh-Os-Version"]) == ("env-name", "env-model", "env-os"))
        ms_env = identity.moonshot_headers("tok", "dev-1")
        check("search/fetch 设备头支持 env 覆盖", (ms_env["X-Msh-Device-Name"], ms_env["X-Msh-Device-Model"], ms_env["X-Msh-Os-Version"]) == ("env-name", "env-model", "env-os"))
        check("search/fetch UA 用 CLI 版本位", ms_env.get("User-Agent") == f"kimi-code-cli/{KIMI_CODE_CLI_VERSION}")
        del os.environ["KIMI_MSH_DEVICE_NAME"], os.environ["KIMI_MSH_DEVICE_MODEL"], os.environ["KIMI_MSH_OS_VERSION"]

        lock = local_credentials.CredentialRefreshLock(tmp / "credentials" / "kimi-code.json")
        default_enabled = lock.enabled
        os.environ["KIMI_DISABLE_OAUTH_LOCK"] = "1"
        disabled = local_credentials.CredentialRefreshLock(tmp / "credentials" / "kimi-code.json").enabled
        check("KIMI_DISABLE_OAUTH_LOCK=1 停用刷新锁", default_enabled == (os.name != "nt") and not disabled)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


async def main() -> None:
    channel_scenarios()
    with tempfile.TemporaryDirectory() as td:
        header_scenarios(Path(td))
    for name, fn in [
        ("rotate_and_write_back", scenario_rotate_and_write_back),
        ("adopt_local_winner", scenario_adopt_local_winner),
        ("invalid_grant_adopts_local", scenario_invalid_grant_adopts_local),
        ("no_revocation_when_file_untouched", scenario_no_revocation_when_file_untouched),
        ("refresh_without_rotation", scenario_refresh_without_rotation),
        ("lock_blocks", scenario_lock_blocks),
    ]:
        with tempfile.TemporaryDirectory() as td:
            await fn(Path(td))
    print("\n" + ("ALL PASS" if not FAILURES else f"FAILED: {FAILURES}"))
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    asyncio.run(main())
