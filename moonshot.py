from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from .constants import (
    DEFAULT_MOONSHOT_FETCH_URL,
    DEFAULT_MOONSHOT_SEARCH_URL,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    KIMI_DATASOURCE_VERSION,
)
from .datasource import account_rotation, required_string
from .identity import moonshot_headers
from .models import DatasourceError, DatasourceHTTPError, OAuthUnauthorizedError, ToolInputError
from .oauth import KimiOAuthClient
from .storage import KimiCredentialStore

AUTH_STATUS_CODES = {401, 403}
MAX_LOCAL_FETCH_BYTES = 10 * 1024 * 1024
MAX_LOCAL_REDIRECTS = 5


class KimiMoonshotClient:
    def __init__(
        self,
        store: KimiCredentialStore,
        oauth: KimiOAuthClient,
        *,
        search_url: str = DEFAULT_MOONSHOT_SEARCH_URL,
        fetch_url: str = DEFAULT_MOONSHOT_FETCH_URL,
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        proxy: str = "",
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.search_url = search_url
        self.fetch_api_url = fetch_url
        self.timeout_seconds = timeout_seconds
        self.proxy = proxy.strip() or None

    async def search(self, *, query: str, limit: int = 5, include_content: bool = False) -> str:
        query = required_string(query, "query")
        limit = normalize_limit(limit)
        response = await self._post_with_rotation(
            self.search_url,
            {
                "text_query": query,
                "limit": limit,
                "enable_page_crawling": bool(include_content),
                "timeout_seconds": max(1, self.timeout_seconds),
            },
            expect_json=True,
        )
        return format_search_results(response)

    async def fetch_url(self, *, url: str) -> str:
        url = validate_http_url(url)
        try:
            response = await self._post_with_rotation(
                self.fetch_api_url,
                {"url": url},
                accept="text/markdown",
                expect_json=False,
            )
        except DatasourceHTTPError as exc:
            if exc.status in AUTH_STATUS_CODES:
                raise
            return await self._local_fetch_after_remote_error(url, exc)
        except DatasourceError as exc:
            return await self._local_fetch_after_remote_error(url, exc)

        text = str(response).strip()
        return text or "Moonshot fetch returned an empty response."

    async def _post_with_rotation(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        accept: str = "",
        expect_json: bool,
    ) -> Any:
        account_ids = await self.store.list_account_ids(include_revoked=False)
        if not account_ids:
            raise OAuthUnauthorizedError("No Kimi OAuth accounts are available. Ask an administrator to run kimi login.")

        start_id = await self.store.next_account_id(account_ids)
        errors: list[str] = []
        for account_id in account_rotation(account_ids, start_id):
            try:
                return await self._post(
                    endpoint,
                    payload,
                    account_id=account_id,
                    force_refresh=False,
                    accept=accept,
                    expect_json=expect_json,
                )
            except OAuthUnauthorizedError as exc:
                errors.append(f"{account_id}: {exc}")
                continue
            except DatasourceHTTPError as exc:
                if exc.status not in AUTH_STATUS_CODES:
                    raise
                try:
                    return await self._post(
                        endpoint,
                        payload,
                        account_id=account_id,
                        force_refresh=True,
                        accept=accept,
                        expect_json=expect_json,
                    )
                except DatasourceHTTPError as retry_exc:
                    if retry_exc.status in AUTH_STATUS_CODES:
                        await self.store.mark_revoked(account_id)
                        errors.append(f"{account_id}: moonshot authorization failed")
                        continue
                    raise
                except OAuthUnauthorizedError as retry_exc:
                    errors.append(f"{account_id}: {retry_exc}")
                    continue

        message = "; ".join(errors) if errors else "all accounts failed"
        raise OAuthUnauthorizedError(f"Kimi Moonshot authorization failed for every configured account: {message}")

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        account_id: str,
        force_refresh: bool,
        accept: str,
        expect_json: bool,
    ) -> Any:
        token = await self.oauth.ensure_fresh(account_id, force=force_refresh)
        device_id = await self.store.get_device_id()
        timeout = aiohttp.ClientTimeout(total=max(1, self.timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=moonshot_headers(token, device_id, KIMI_DATASOURCE_VERSION, accept=accept),
                    proxy=self.proxy,
                ) as response:
                    body = await response.text()
                    if not response.ok:
                        raise DatasourceHTTPError(response.status, body)
                    if not expect_json:
                        return body
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise DatasourceError("Moonshot search response was not valid JSON.") from exc
            except asyncio.TimeoutError:
                raise DatasourceError(f"Moonshot request timed out after {self.timeout_seconds} seconds.") from None
            except aiohttp.ClientError as exc:
                raise DatasourceError(f"Moonshot request failed: {exc}") from exc

    async def _local_fetch_after_remote_error(self, url: str, remote_error: Exception) -> str:
        try:
            return await self._local_fetch(url)
        except Exception as exc:
            raise DatasourceError(
                f"Moonshot fetch failed ({remote_error}); local fallback failed ({exc})"
            ) from exc

    async def _local_fetch(self, url: str) -> str:
        timeout = aiohttp.ClientTimeout(total=max(1, self.timeout_seconds))
        headers = {"User-Agent": f"kimi-code/{KIMI_DATASOURCE_VERSION}"}
        current_url = url
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _ in range(MAX_LOCAL_REDIRECTS + 1):
                await ensure_public_url(current_url)
                async with session.get(
                    current_url,
                    headers=headers,
                    proxy=self.proxy,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400 and response.headers.get("Location"):
                        current_url = urljoin(current_url, str(response.headers["Location"]))
                        continue
                    if not response.ok:
                        body = await response.text()
                        raise DatasourceHTTPError(response.status, body[:1000])
                    raw = await read_limited(response)
                    text = decode_local_response(
                        raw,
                        response.headers.get("Content-Type", ""),
                        response.charset or "utf-8",
                    )
                    return text or "Local fetch returned an empty response."
        raise DatasourceError(f"Local fetch exceeded {MAX_LOCAL_REDIRECTS} redirects.")


def normalize_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise ToolInputError("limit must be an integer from 1 to 10.") from None
    if limit < 1 or limit > 10:
        raise ToolInputError("limit must be an integer from 1 to 10.")
    return limit


def validate_http_url(value: str) -> str:
    url = required_string(value, "url")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolInputError("url must be an absolute http or https URL.")
    return url


async def ensure_public_url(url: str) -> None:
    parsed = urlparse(validate_http_url(url))
    host = parsed.hostname
    if host is None:
        raise ToolInputError("url must include a host.")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ToolInputError("local fallback does not fetch localhost URLs.")

    try:
        reject_non_global_ip(ipaddress.ip_address(host))
        return
    except ValueError:
        pass

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DatasourceError(f"Unable to resolve host {host}: {exc}") from exc

    if not infos:
        raise DatasourceError(f"Unable to resolve host {host}.")
    for item in infos:
        address = item[4][0]
        reject_non_global_ip(ipaddress.ip_address(address))


def reject_non_global_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise ToolInputError("local fallback only fetches public internet URLs.")


async def read_limited(response: aiohttp.ClientResponse) -> bytes:
    content_length = response.content_length
    if content_length is not None and content_length > MAX_LOCAL_FETCH_BYTES:
        raise DatasourceError(f"Local fetch response exceeds {MAX_LOCAL_FETCH_BYTES} bytes.")

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(65536):
        total += len(chunk)
        if total > MAX_LOCAL_FETCH_BYTES:
            raise DatasourceError(f"Local fetch response exceeds {MAX_LOCAL_FETCH_BYTES} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def decode_local_response(raw: bytes, content_type: str, charset: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type and not is_text_media_type(media_type):
        raise DatasourceError(f"Local fetch only supports text responses; received {media_type}.")

    text = raw.decode(charset or "utf-8", errors="replace")
    if "html" in media_type or looks_like_html(text):
        return html_to_text(text)
    return normalize_text(text)


def is_text_media_type(media_type: str) -> bool:
    return (
        media_type.startswith("text/")
        or media_type in {"application/json", "application/xml", "application/xhtml+xml"}
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def looks_like_html(text: str) -> bool:
    sample = text[:500].lower()
    return "<html" in sample or "<body" in sample or "<!doctype html" in sample


class HTMLTextExtractor(HTMLParser):
    block_tags = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
    skip_tags = {"head", "script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.skip_tags:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.skip_tags and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data:
            self.parts.append(data)

    def text(self) -> str:
        return normalize_text("".join(self.parts))


def html_to_text(html: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def normalize_text(text: str) -> str:
    cleaned_lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines()]
    lines: list[str] = []
    previous_blank = True
    for line in cleaned_lines:
        if not line:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    return "\n".join(lines).strip()


def format_search_results(response: Any) -> str:
    if not isinstance(response, dict):
        return str(response)
    results = response.get("search_results")
    if not isinstance(results, list):
        return json.dumps(response, ensure_ascii=False)
    if not results:
        return "No search results."

    blocks: list[str] = []
    for index, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        date = str(item.get("date") or "").strip()
        content = str(item.get("content") or "").strip()

        lines = [f"{index}. {title}"]
        if url:
            lines.append(f"URL: {url}")
        if date:
            lines.append(f"Date: {date}")
        if snippet:
            lines.append(snippet)
        if content:
            lines.append(content)
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "No search results."
