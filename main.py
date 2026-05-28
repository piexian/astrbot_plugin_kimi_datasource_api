from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.session_waiter import FILTERS, DefaultSessionFilter, SessionController, SessionWaiter

from .constants import (
    DEFAULT_CLIENT_ID,
    DEFAULT_DATASOURCE_API_URL,
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
    DEFAULT_OAUTH_HOST,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    KIMI_DATASOURCE_VERSION,
    PLUGIN_NAME,
)
from .datasource import KimiDatasourceClient
from .models import KimiPluginError, OAuthUnauthorizedError, token_from_credentials
from .oauth import KimiOAuthClient
from .schemas import CALL_DATA_SOURCE_TOOL_SCHEMA, GET_DATA_SOURCE_DESC_SCHEMA, QUERY_STOCK_SCHEMA
from .sessions import PendingLogin, PendingLoginRegistry
from .storage import KimiCredentialStore, mask_token, normalize_account_id, normalize_account_id_list
from .tool_defs import KimiFunctionTool

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:
    get_astrbot_plugin_data_path = None

KimiFunctionTool.__module__ = __name__


CONFIG_PATHS = {
    "oauth_host": ("oauth_settings", "oauth_host"),
    "request_timeout_seconds": ("connection_settings", "request_timeout_seconds"),
    "proxy": ("connection_settings", "proxy"),
    "login_timeout_seconds": ("oauth_settings", "login_timeout_seconds"),
    "api_url": ("datasource_settings", "api_url"),
    "response_parse_mode": ("datasource_settings", "response_parse_mode"),
    "save_response_files": ("datasource_settings", "save_response_files"),
    "account_ids": ("account_settings", "account_ids"),
}

CONFIG_DEFAULTS = {
    "oauth_host": DEFAULT_OAUTH_HOST,
    "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
    "proxy": "",
    "login_timeout_seconds": DEFAULT_LOGIN_TIMEOUT_SECONDS,
    "api_url": DEFAULT_DATASOURCE_API_URL,
    "response_parse_mode": "official",
    "save_response_files": True,
    "account_ids": [],
}


def local_kimi_code_roots(env: Mapping[str, str] | None = None) -> list[Path]:
    env = env or os.environ
    roots: list[Path] = []
    seen: set[str] = set()

    def add(path: str | Path | None) -> None:
        if not path:
            return
        candidate = Path(path).expanduser()
        key = str(candidate)
        if key not in seen:
            roots.append(candidate)
            seen.add(key)

    add(env.get("KIMI_CODE_HOME"))
    add(env.get("KIMI_HOME"))

    homes: list[Path] = []
    for key in ("HOME", "USERPROFILE"):
        value = env.get(key)
        if value:
            homes.append(Path(value))
    try:
        homes.append(Path.home())
    except RuntimeError:
        pass

    seen_homes: set[str] = set()
    for home in homes:
        home_key = str(home)
        if home_key in seen_homes:
            continue
        seen_homes.add(home_key)
        add(home / ".kimi-code")
        add(home / "Library" / "Application Support" / "kimi-code")
        add(home / "Library" / "Application Support" / "Kimi Code")
        add(home / "AppData" / "Roaming" / "kimi-code")
        add(home / "AppData" / "Roaming" / "Kimi Code")
        add(home / "AppData" / "Local" / "kimi-code")
        add(home / "AppData" / "Local" / "Kimi Code")

    for key in ("APPDATA", "LOCALAPPDATA"):
        base = env.get(key)
        if base:
            add(Path(base) / "kimi-code")
            add(Path(base) / "Kimi Code")

    xdg_config_home = env.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        add(Path(xdg_config_home) / "kimi-code")
    return roots


class KimiDatasourcePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.store = KimiCredentialStore(self)
        self.pending_logins = PendingLoginRegistry()
        self.oauth = self._build_oauth_client()
        self.datasource = self._build_datasource_client()

    async def initialize(self) -> None:
        await self._sync_config_accounts()
        self.context.add_llm_tools(
            KimiFunctionTool(
                name="query_stock",
                description="Query realtime stock price, realtime technical indicators, open summaries, or close summaries for up to 3 tickers.",
                parameters=QUERY_STOCK_SCHEMA,
                plugin=self,
            ),
            KimiFunctionTool(
                name="get_data_source_desc",
                description="Get the current API documentation for one Kimi data source before calling a specific API.",
                parameters=GET_DATA_SOURCE_DESC_SCHEMA,
                plugin=self,
            ),
            KimiFunctionTool(
                name="call_data_source_tool",
                description="Call one API from a Kimi data source after reading get_data_source_desc.",
                parameters=CALL_DATA_SOURCE_TOOL_SCHEMA,
                plugin=self,
            ),
        )
        logger.info(f"[{PLUGIN_NAME}] Kimi datasource LLM tools registered")

    def _cfg(self, key: str, default: Any = None) -> Any:
        path = CONFIG_PATHS.get(key)
        fallback = CONFIG_DEFAULTS.get(key, default)
        if path:
            section = self.config.get(path[0], {})
            if isinstance(section, dict) and path[1] in section:
                return section[path[1]]
        return self.config.get(key, fallback)

    def _build_oauth_client(self) -> KimiOAuthClient:
        return KimiOAuthClient(
            self.store,
            oauth_host=str(self._cfg("oauth_host", DEFAULT_OAUTH_HOST) or DEFAULT_OAUTH_HOST),
            client_id=DEFAULT_CLIENT_ID,
            version=KIMI_DATASOURCE_VERSION,
            timeout_seconds=int(self._cfg("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)),
            proxy=str(self._cfg("proxy", "") or ""),
        )

    def _build_datasource_client(self) -> KimiDatasourceClient:
        return KimiDatasourceClient(
            self.store,
            self.oauth,
            api_url=str(self._cfg("api_url", DEFAULT_DATASOURCE_API_URL) or DEFAULT_DATASOURCE_API_URL),
            timeout_seconds=int(self._cfg("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)),
            proxy=str(self._cfg("proxy", "") or ""),
            response_parse_mode=str(self._cfg("response_parse_mode", "official") or "official"),
            save_response_files=bool(self._cfg("save_response_files", True)),
            files_dir=self._plugin_data_dir() / "files",
        )

    def _plugin_data_dir(self) -> Path:
        if get_astrbot_plugin_data_path is not None:
            root = Path(get_astrbot_plugin_data_path())
        else:
            root = Path(__file__).resolve().parent / "data" / "plugin_data"
        path = root / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    @filter.command_group("kimi")
    def kimi(self):
        """Kimi datasource 指令组。"""
        pass

    @kimi.command("help")
    async def kimi_help(self, event: AstrMessageEvent):
        """显示 Kimi datasource 帮助。"""
        yield event.plain_result(self._help_text())

    @kimi.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @kimi.command("login")
    async def kimi_login(self, event: AstrMessageEvent):
        """发起 Kimi Code OAuth 登录。"""
        await self._sync_config_accounts()
        session_id = event.unified_msg_origin
        restart = "--restart" in event.get_message_str()
        requested_id = normalize_account_id(self._command_args(event, "login").replace("--restart", "").strip())
        existing = self.pending_logins.get(session_id)
        if existing and not restart:
            yield event.plain_result(self._pending_text(existing))
            return
        if existing:
            await self._cancel_pending(existing, notify=False)

        try:
            final_account_id = await self.store.allocate_account_id(requested_id)
            auth = await self.oauth.request_device_authorization()
        except KimiPluginError as exc:
            yield event.plain_result(f"Kimi 登录发起失败: {exc}")
            return

        now = time.time()
        deadline = now + int(self._cfg("login_timeout_seconds", DEFAULT_LOGIN_TIMEOUT_SECONDS))
        pending = PendingLogin(
            session_id=session_id,
            account_id=final_account_id,
            device_code=auth.device_code,
            verification_uri_complete=auth.verification_uri_complete,
            user_code=auth.user_code,
            started_at=now,
            deadline_at=deadline,
            interval=max(1, auth.interval),
            state="polling",
        )
        self.pending_logins.set(pending)
        pending.poll_task = asyncio.create_task(self._poll_login(pending))
        pending.waiter_task = asyncio.create_task(self._wait_login_messages(pending))
        yield event.plain_result(self._auth_text(pending, is_refresh=False))

    @kimi.command("status")
    async def kimi_status(self, event: AstrMessageEvent):
        """查看 Kimi datasource 登录状态。"""
        await self._sync_config_accounts()
        pending = self.pending_logins.get(event.unified_msg_origin)
        if pending:
            yield event.plain_result(self._pending_text(pending))
            return
        yield event.plain_result(await self._credential_status_text())

    @kimi.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @kimi.command("import-local")
    async def kimi_import_local(self, event: AstrMessageEvent):
        """导入本机已登录的 Kimi Code OAuth 凭证。"""
        await self._sync_config_accounts()
        requested_id = normalize_account_id(self._command_args(event, "import-local"))
        try:
            account_id = await self._import_local_kimi_code(event.unified_msg_origin, requested_id)
        except KimiPluginError as exc:
            yield event.plain_result(f"导入本地 Kimi Code 凭证失败: {exc}")
            return
        yield event.plain_result(f"已导入本地 Kimi Code 凭证，账号 ID: {account_id}")

    @kimi.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @kimi.command("refresh")
    async def kimi_refresh(self, event: AstrMessageEvent):
        """强制刷新一个或全部 Kimi OAuth token。"""
        await self._sync_config_accounts()
        requested_id = normalize_account_id(self._command_args(event, "refresh"))
        try:
            if requested_id:
                token = await self.oauth.ensure_fresh(requested_id, force=True)
                yield event.plain_result(f"Kimi 账号 {requested_id} token 刷新成功: {mask_token(token)}")
                return

            account_ids = await self.store.list_account_ids(include_revoked=False)
            if not account_ids:
                yield event.plain_result("没有可刷新的 Kimi OAuth 账号。请先执行 kimi login。")
                return
            lines = ["Kimi token 刷新结果:"]
            for item in account_ids:
                try:
                    token = await self.oauth.ensure_fresh(item, force=True)
                    lines.append(f"- {item}: 成功 {mask_token(token)}")
                except KimiPluginError as exc:
                    lines.append(f"- {item}: 失败 {exc}")
            yield event.plain_result("\n".join(lines))
        except KimiPluginError as exc:
            yield event.plain_result(f"Kimi token 刷新失败: {exc}")

    @kimi.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @kimi.command("logout")
    async def kimi_logout(self, event: AstrMessageEvent):
        """删除一个或全部 Kimi OAuth 账号。"""
        await self._sync_config_accounts()
        pending = self.pending_logins.get(event.unified_msg_origin)
        if pending:
            await self._cancel_pending(pending, notify=False)

        raw = self._command_args(event, "logout").strip()
        if raw in {"--all", "all", "*"}:
            await self.store.delete_credentials()
            await self._write_config_account_ids([])
            yield event.plain_result("已删除全部 Kimi OAuth 账号。")
            return

        requested_id = normalize_account_id(raw)
        if not requested_id:
            yield event.plain_result("请指定要删除的账号 ID，或使用 kimi logout --all 删除全部账号。")
            return
        removed = await self.store.delete_account(requested_id)
        ids = await self.store.list_account_ids(include_revoked=True)
        await self._write_config_account_ids(ids)
        if removed:
            yield event.plain_result(f"已删除 Kimi OAuth 账号: {requested_id}")
        else:
            yield event.plain_result(f"未找到 Kimi OAuth 账号: {requested_id}")

    @kimi.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @kimi.command("sync")
    async def kimi_sync(self, event: AstrMessageEvent):
        """按配置账号 ID 列表同步删除账号。"""
        removed = await self._sync_config_accounts()
        if removed:
            yield event.plain_result("已按配置删除账号: " + ", ".join(removed))
        else:
            yield event.plain_result("Kimi 账号配置已同步，没有需要删除的账号。")

    async def _tool_query_stock(
        self,
        ticker: str,
        type: str = "realtime_price",
        time: str = "",
        file_path: str = "",
    ) -> str:
        return await self._run_tool(
            lambda: self.datasource.query_stock(
                ticker=ticker,
                query_type=type,
                query_time=time,
                file_path=file_path,
            )
        )

    async def _tool_get_data_source_desc(self, name: str) -> str:
        return await self._run_tool(lambda: self.datasource.get_data_source_desc(name))

    async def _tool_call_data_source_tool(
        self,
        data_source_name: str,
        api_name: str,
        params: dict[str, Any],
    ) -> str:
        return await self._run_tool(
            lambda: self.datasource.call_data_source_tool(
                data_source_name=data_source_name,
                api_name=api_name,
                params=params,
            )
        )

    async def _run_tool(self, factory) -> str:
        try:
            await self._sync_config_accounts()
            return await factory()
        except OAuthUnauthorizedError as exc:
            return f"Kimi datasource 需要重新登录: {exc} 请让管理员执行 kimi login。"
        except KimiPluginError as exc:
            return f"Kimi datasource 调用失败: {exc}"
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] unexpected tool error: {exc}", exc_info=True)
            return f"Kimi datasource 调用失败: {exc}"

    async def _import_local_kimi_code(self, session_id: str, requested_id: str = "") -> str:
        credentials_path = None
        device_id_path = None
        searched: list[str] = []
        for root in local_kimi_code_roots():
            searched.append(str(root))
            candidate = root / "credentials" / "kimi-code.json"
            if candidate.exists():
                credentials_path = candidate
                device_id_path = root / "device_id"
                break

        if credentials_path is None:
            raise KimiPluginError("未找到本机 Kimi Code 凭证。已检查: " + ", ".join(searched))

        try:
            data = json.loads(credentials_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KimiPluginError(f"{credentials_path} 不是有效 JSON") from exc
        except OSError as exc:
            raise KimiPluginError(f"无法读取 {credentials_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise KimiPluginError(f"{credentials_path} 内容格式不正确")
        token = token_from_credentials(data)
        if token is None:
            raise KimiPluginError(f"{credentials_path} 缺少可用 access_token 或 refresh_token")

        if device_id_path.exists():
            try:
                await self.store.save_device_id(device_id_path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise KimiPluginError(f"无法读取 {device_id_path}: {exc}") from exc

        account_id = await self.store.allocate_account_id(requested_id or "local-kimi-code")
        device_id = await self.store.get_device_id()
        saved_id = await self.store.save_login_token(
            token,
            account_id=account_id,
            device_id=device_id,
            session_id=session_id,
        )
        await self._append_config_account_id(saved_id)
        return saved_id

    async def _poll_login(self, pending: PendingLogin) -> None:
        try:
            while self.pending_logins.is_current(pending) and pending.remaining_seconds > 0:
                result = await self.oauth.poll_device_token(pending.device_code)
                if result.kind == "success" and result.token:
                    device_id = await self.store.get_device_id()
                    account_id = await self.store.save_login_token(
                        result.token,
                        account_id=pending.account_id,
                        device_id=device_id,
                        session_id=pending.session_id,
                    )
                    pending.account_id = account_id
                    await self._append_config_account_id(account_id)
                    await self._finish_pending(pending)
                    await self._send_session(
                        pending.session_id,
                        f"Kimi 登录成功，账号 ID: {account_id}\ndatasource 工具已可使用。",
                    )
                    return
                if result.kind == "denied":
                    await self._fail_pending(pending, f"Kimi 登录被拒绝: {result.description}")
                    return
                if result.kind == "expired":
                    await self._renew_device_code(pending)
                    continue
                if result.error_code == "slow_down":
                    pending.interval += 5
                await asyncio.sleep(min(pending.interval, max(1, pending.remaining_seconds)))
            if self.pending_logins.is_current(pending):
                await self._fail_pending(pending, "Kimi 登录已超时，请重新执行 kimi login。")
        except asyncio.CancelledError:
            raise
        except KimiPluginError as exc:
            if self.pending_logins.is_current(pending):
                await self._fail_pending(pending, f"Kimi 登录失败: {exc}")

    async def _renew_device_code(self, pending: PendingLogin) -> None:
        auth = await self.oauth.request_device_authorization()
        pending.device_code = auth.device_code
        pending.verification_uri_complete = auth.verification_uri_complete
        pending.user_code = auth.user_code
        pending.interval = max(1, auth.interval)
        await self._send_session(pending.session_id, self._auth_text(pending, is_refresh=True))

    async def _wait_login_messages(self, pending: PendingLogin) -> None:
        session_filter = DefaultSessionFilter()
        FILTERS.append(session_filter)
        waiter = SessionWaiter(session_filter, pending.session_id, False)
        pending.session_controller = waiter.session_controller

        async def handler(controller: SessionController, event: AstrMessageEvent) -> None:
            text = event.get_message_str().strip().lower()
            if text in {"取消", "cancel", "stop", "/cancel"}:
                await self._cancel_pending(pending, notify=True)
                return
            if self._is_command_text(text, "login") and "--restart" in text:
                await self._renew_device_code(pending)
                if self.pending_logins.is_current(pending):
                    controller.keep(max(1, pending.remaining_seconds), reset_timeout=True)
                return
            if text in {"状态", "status"} or self._is_command_text(text, "status"):
                await event.send(MessageChain().message(self._pending_text(pending)))
            else:
                await event.send(MessageChain().message("正在等待你在浏览器中完成 Kimi 授权。输入 cancel 可取消登录。"))
            if self.pending_logins.is_current(pending):
                controller.keep(max(1, pending.remaining_seconds), reset_timeout=True)

        try:
            await waiter.register_wait(handler, timeout=max(1, pending.remaining_seconds))
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] login session waiter ended: {exc}")

    async def _cancel_pending(self, pending: PendingLogin, *, notify: bool) -> None:
        removed = self.pending_logins.pop(pending.session_id)
        if not removed:
            return
        pending.state = "cancelled"
        if pending.poll_task and not pending.poll_task.done():
            pending.poll_task.cancel()
        if pending.session_controller:
            pending.session_controller.stop()
        if notify:
            await self._send_session(pending.session_id, "已取消 Kimi 登录。")

    async def _finish_pending(self, pending: PendingLogin) -> None:
        self.pending_logins.pop(pending.session_id)
        pending.state = "saved"
        if pending.session_controller:
            pending.session_controller.stop()
        if pending.waiter_task and not pending.waiter_task.done():
            pending.waiter_task.cancel()

    async def _fail_pending(self, pending: PendingLogin, message: str) -> None:
        self.pending_logins.pop(pending.session_id)
        pending.state = "failed"
        if pending.session_controller:
            pending.session_controller.stop()
        if pending.waiter_task and not pending.waiter_task.done():
            pending.waiter_task.cancel()
        await self._send_session(pending.session_id, message)

    async def _send_session(self, session_id: str, text: str) -> None:
        try:
            await self.context.send_message(session_id, MessageChain().message(text))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] failed to send active message: {exc}")

    async def _sync_config_accounts(self) -> list[str]:
        accounts = await self.store.list_accounts()
        actual_ids = sorted(accounts)
        config_ids = normalize_account_id_list(self._cfg("account_ids", []))
        snapshot = await self.store.load_config_snapshot()

        if snapshot is None and not config_ids and actual_ids:
            await self._write_config_account_ids(actual_ids)
            return []

        removed: list[str] = []
        if snapshot is not None:
            removed = [account_id for account_id in snapshot if account_id not in config_ids]
            if removed:
                removed = await self.store.delete_accounts(removed)
                accounts = await self.store.list_accounts()
                actual_ids = sorted(accounts)

        if snapshot is None and not config_ids:
            config_ids = actual_ids
        elif config_ids:
            config_ids = [account_id for account_id in config_ids if account_id in accounts]
            missing = [account_id for account_id in actual_ids if account_id not in config_ids]
            config_ids.extend(missing)

        await self._write_config_account_ids(config_ids)
        return removed

    async def _append_config_account_id(self, account_id: str) -> None:
        ids = normalize_account_id_list(self._cfg("account_ids", []))
        if account_id not in ids:
            ids.append(account_id)
        await self._write_config_account_ids(ids)

    async def _write_config_account_ids(self, account_ids: list[str]) -> None:
        ids = normalize_account_id_list(account_ids)
        section = self.config.setdefault("account_settings", {})
        if isinstance(section, dict):
            section["account_ids"] = ids
        self.config["account_settings"] = section if isinstance(section, dict) else {"account_ids": ids}
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()
        await self.store.save_config_snapshot(ids)

    def _auth_text(self, pending: PendingLogin, *, is_refresh: bool) -> str:
        title = "Kimi 授权链接已更新" if is_refresh else "请完成 Kimi Code OAuth 登录"
        return (
            f"{title}\n\n"
            f"账号 ID: {pending.account_id}\n"
            f"打开链接: {pending.verification_uri_complete}\n"
            f"备用验证码: {pending.user_code}\n"
            f"剩余时间: {pending.remaining_seconds} 秒\n\n"
            "授权完成后无需发送消息，插件会自动检测。输入 cancel 可取消。"
        )

    def _pending_text(self, pending: PendingLogin) -> str:
        return (
            "Kimi 登录正在等待授权。\n"
            f"账号 ID: {pending.account_id}\n"
            f"链接: {pending.verification_uri_complete}\n"
            f"验证码: {pending.user_code}\n"
            f"剩余时间: {pending.remaining_seconds} 秒"
        )

    async def _credential_status_text(self) -> str:
        accounts = await self.store.list_accounts()
        if not accounts:
            return "Kimi datasource 未登录。请管理员执行 kimi login [账号ID]。"

        lines = ["Kimi datasource 账号:"]
        for account_id in sorted(accounts):
            credentials = accounts[account_id]
            status = str(credentials.get("status") or "unknown")
            expires_at = credentials.get("expires_at")
            remaining = int(expires_at - time.time()) if isinstance(expires_at, int | float) else 0
            expires_text = "未知"
            if isinstance(expires_at, int | float) and expires_at > 0:
                expires_text = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(
                f"- {account_id}: {status}, expires={expires_text}, remaining={max(0, remaining)}s, "
                f"access={mask_token(str(credentials.get('access_token') or ''))}, "
                f"refresh={mask_token(str(credentials.get('refresh_token') or ''))}"
            )
        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "Kimi datasource 指令:\n"
            "在前面加你的 AstrBot 唤醒前缀，例如默认配置通常是 /。\n"
            "kimi help - 显示帮助\n"
            "kimi login [账号ID] - 管理员发起 OAuth 登录；不填则自动生成 account-N\n"
            "kimi import-local [账号ID] - 管理员导入本机 Kimi Code 已登录凭证\n"
            "kimi status - 查看所有账号状态\n"
            "kimi refresh [账号ID] - 管理员刷新指定账号；不填则刷新全部有效账号\n"
            "kimi logout <账号ID|--all> - 管理员删除账号\n"
            "kimi sync - 按配置文件 account_ids 列表同步删除账号\n\n"
            "配置文件 account_settings.account_ids 会展示已登录账号 ID；从列表删除某个 ID 后，插件会同步删除对应账号。"
        )

    def _command_args(self, event: AstrMessageEvent, sub_command: str) -> str:
        text = re.sub(r"\s+", " ", event.get_message_str().strip())
        parts = text.split(" ", 2)
        if len(parts) < 2:
            return ""
        root = re.sub(r"^[^A-Za-z0-9_]+", "", parts[0])
        if root != "kimi" or parts[1] != sub_command:
            return ""
        if len(parts) == 2:
            return ""
        return parts[2].strip()

    def _is_command_text(self, text: str, sub_command: str) -> bool:
        parts = re.sub(r"\s+", " ", text.strip()).split(" ", 2)
        if len(parts) < 2:
            return False
        root = re.sub(r"^[^A-Za-z0-9_]+", "", parts[0])
        return root == "kimi" and parts[1] == sub_command

    async def terminate(self) -> None:
        await self.pending_logins.cancel_all()
