from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

import aiohttp

from .constants import (
    DEFAULT_DATASOURCE_API_URL,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    KIMI_DATASOURCE_VERSION,
    VALID_STOCK_QUERY_TYPES,
)
from .identity import datasource_headers
from .models import DatasourceError, DatasourceHTTPError, OAuthUnauthorizedError, ToolInputError
from .oauth import KimiOAuthClient
from .storage import KimiCredentialStore


class KimiDatasourceClient:
    def __init__(
        self,
        store: KimiCredentialStore,
        oauth: KimiOAuthClient,
        *,
        api_url: str = DEFAULT_DATASOURCE_API_URL,
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        proxy: str = "",
        response_parse_mode: str = "official",
        save_response_files: bool = True,
        files_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.proxy = proxy.strip() or None
        self.response_parse_mode = response_parse_mode
        self.save_response_files = save_response_files
        self.files_dir = files_dir

    async def query_stock(
        self,
        *,
        ticker: str,
        query_type: str = "realtime_price",
        query_time: str = "",
        file_path: str = "",
    ) -> str:
        params = build_stock_params(ticker, query_type, query_time, file_path)
        result = await self.call_kimi_tool("get_stock_realtime_price", params)
        text = f"{result.text}\n\nCSV data written to: {params['file_path']}".strip()
        if result.saved_files:
            text += "\n\nLocal files saved to:\n" + "\n".join(f"- {path}" for path in result.saved_files)
        return text

    async def get_data_source_desc(self, name: str) -> str:
        result = await self.call_kimi_tool("get_data_source_desc", {"name": required_string(name, "name")})
        return result.with_saved_files()

    async def call_data_source_tool(
        self,
        *,
        data_source_name: str,
        api_name: str,
        params: dict[str, Any],
    ) -> str:
        if not isinstance(params, dict):
            raise ToolInputError("params must be an object.")
        result = await self.call_kimi_tool(
            "call_data_source_tool",
            {
                "data_source_name": required_string(data_source_name, "data_source_name"),
                "api_name": required_string(api_name, "api_name"),
                "params": params,
            },
        )
        return result.with_saved_files()

    async def call_kimi_tool(self, method: str, params: dict[str, Any]) -> "DatasourceResult":
        account_ids = await self.store.list_account_ids(include_revoked=False)
        if not account_ids:
            raise OAuthUnauthorizedError("No Kimi OAuth accounts are available. Ask an administrator to run kimi login.")

        start_id = await self.store.next_account_id(account_ids)
        errors: list[str] = []
        for account_id in account_rotation(account_ids, start_id):
            try:
                response = await self._post_json(method, params, account_id=account_id, force_refresh=False)
                break
            except OAuthUnauthorizedError as exc:
                errors.append(f"{account_id}: {exc}")
                continue
            except DatasourceHTTPError as exc:
                if exc.status not in {401, 403}:
                    raise
                try:
                    response = await self._post_json(method, params, account_id=account_id, force_refresh=True)
                    break
                except DatasourceHTTPError as retry_exc:
                    if retry_exc.status in {401, 403}:
                        await self.store.mark_revoked(account_id)
                        errors.append(f"{account_id}: datasource authorization failed")
                        continue
                    raise
                except OAuthUnauthorizedError as retry_exc:
                    errors.append(f"{account_id}: {retry_exc}")
                    continue
        else:
            message = "; ".join(errors) if errors else "all accounts failed"
            raise OAuthUnauthorizedError(f"Kimi datasource authorization failed for every configured account: {message}")

        text = extract_text(response, mode=self.response_parse_mode)
        saved_files = await self._save_response_files(response)
        return DatasourceResult(text=text, saved_files=saved_files)

    async def _post_json(self, method: str, params: dict[str, Any], *, account_id: str, force_refresh: bool) -> Any:
        token = await self.oauth.ensure_fresh(account_id, force=force_refresh)
        device_id = await self.store.get_device_id()
        timeout = aiohttp.ClientTimeout(total=max(1, self.timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(
                    self.api_url,
                    json={"method": method, "params": params},
                    headers=datasource_headers(token, device_id, KIMI_DATASOURCE_VERSION),
                    proxy=self.proxy,
                ) as response:
                    body = await response.text()
                    if not response.ok:
                        raise DatasourceHTTPError(response.status, body)
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError:
                        return body
            except asyncio.TimeoutError:
                raise DatasourceError(f"Request timed out after {self.timeout_seconds} seconds.") from None
            except aiohttp.ClientError as exc:
                raise DatasourceError(f"Kimi datasource request failed: {exc}") from exc
            except OAuthUnauthorizedError:
                raise

    async def _save_response_files(self, response: Any) -> list[str]:
        if not self.save_response_files or self.files_dir is None or not isinstance(response, dict):
            return []
        files = response.get("files")
        if not isinstance(files, list):
            return []

        self.files_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                continue
            name = safe_filename(str(item.get("name") or f"file_{index}"))
            content = item.get("content")
            if not isinstance(content, str):
                continue
            target = unique_path(self.files_dir / name)
            if item.get("encoding") == "base64":
                target.write_bytes(base64.b64decode(content))
            else:
                target.write_text(content, encoding="utf-8")
            saved.append(str(target))
        return saved


class DatasourceResult:
    def __init__(self, *, text: str, saved_files: list[str]) -> None:
        self.text = text
        self.saved_files = saved_files

    def with_saved_files(self) -> str:
        if not self.saved_files:
            return self.text
        return f"{self.text}\n\nLocal files saved to:\n" + "\n".join(f"- {path}" for path in self.saved_files)


def build_stock_params(ticker: str, query_type: str, query_time: str = "", file_path: str = "") -> dict[str, Any]:
    ticker = required_string(ticker, "ticker")
    tickers = [item.strip() for item in ticker.split(",") if item.strip()]
    if not tickers:
        raise ToolInputError("Missing required argument: ticker.")
    if len(tickers) > 3:
        raise ToolInputError("ticker accepts at most 3 values separated by commas.")

    query_type = (query_type or "realtime_price").strip()
    if query_type not in VALID_STOCK_QUERY_TYPES:
        raise ToolInputError(f"type must be one of {VALID_STOCK_QUERY_TYPES}; received: {query_type}")

    params: dict[str, Any] = {
        "ticker": ticker,
        "type": query_type,
        "file_path": required_string(file_path, "file_path") if file_path else default_stock_file_path(ticker, query_type),
    }
    if query_time and query_time.strip():
        params["time"] = query_time.strip()
    return params


def default_stock_file_path(ticker: str, query_type: str) -> str:
    safe_ticker = ticker.replace(",", "_").replace(".", "_")
    return f"/tmp/stock_{safe_ticker}_{query_type}.csv"


def extract_text(response: Any, *, mode: str = "official") -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return str(response)
    if response.get("is_success") is False:
        message = extract_user_text(response.get("error")) or json.dumps(response, ensure_ascii=False)
        raise DatasourceError(f"Tool API returned an error: {message}")

    result = response.get("result")
    if mode == "legacy_zip":
        text = extract_role_text(result, "assistant") or extract_role_text(result, "user")
    else:
        text = extract_role_text(result, "user")
    if text:
        return text
    return f"Tool API succeeded but did not return user text. Raw response: {json.dumps(response, ensure_ascii=False)}"


def extract_user_text(value: Any) -> str | None:
    return extract_role_text(value, "user")


def extract_role_text(value: Any, role: str) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = value.get(role)
    if not isinstance(parts, list):
        return None
    text = "\n\n".join(
        item["text"]
        for item in parts
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"]
    )
    return text or None


def required_string(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"Missing required argument: {field}.")
    return value.strip()


def safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return cleaned or "file"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise DatasourceError(f"Unable to choose a unique file path under {path.parent}")


def account_rotation(account_ids: list[str], start_id: str) -> list[str]:
    ids = sorted(dict.fromkeys(account_ids))
    if start_id not in ids:
        return ids
    index = ids.index(start_id)
    return ids[index:] + ids[:index]
