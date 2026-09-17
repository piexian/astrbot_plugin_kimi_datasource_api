from __future__ import annotations

import os
import platform
import socket
from uuid import uuid4

from .constants import (
    KIMI_CODE_CLI_VERSION,
    KIMI_DATASOURCE_PLATFORM,
    KIMI_DATASOURCE_VERSION,
    KIMI_OAUTH_PLATFORM,
)


def ascii_header(value: object, fallback: str = "unknown") -> str:
    cleaned = "".join(ch for ch in str(value) if 0x20 <= ord(ch) <= 0x7E).strip()
    return cleaned or fallback


def device_name() -> str:
    return ascii_header(socket.gethostname())


def new_device_id() -> str:
    return str(uuid4())


def _os_kernel_release() -> str:
    # 对齐 Node os.release()：Windows 发内核版本号（10.0.xxxxx），其余平台发内核 release
    if platform.system() == "Windows":
        return platform.version() or platform.release()
    return platform.release()


def device_model() -> str:
    system = platform.system() or platform.platform() or "unknown"
    release = _os_kernel_release()
    machine = platform.machine()
    if system == "Darwin":
        return ascii_header(f"macOS {release} {machine}")
    if system == "Windows":
        return ascii_header(f"Windows {release} {machine}")
    return ascii_header(f"{system} {release} {machine}")

def device_os_version() -> str:
    return ascii_header(_os_kernel_release(), "unknown")

def oauth_device_headers(device_id: str, version: str) -> dict[str, str]:
    return {
        # 官方 2.0.0 起 OAuth 请求也带产品 UA（createKimiDefaultHeaders）
        "User-Agent": f"kimi-code-cli/{ascii_header(version)}",
        "X-Msh-Platform": KIMI_OAUTH_PLATFORM,
        "X-Msh-Version": ascii_header(version),
        "X-Msh-Device-Name": device_name(),
        "X-Msh-Device-Model": device_model(),
        "X-Msh-Os-Version": device_os_version(),
        "X-Msh-Device-Id": ascii_header(device_id),
    }


def datasource_headers(token: str, device_id: str, version: str = KIMI_DATASOURCE_VERSION, *, tool_call_id: str = "") -> dict[str, str]:
    env = os.environ
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Msh-Tool-Call-Id": tool_call_id or str(uuid4()),
        "X-Msh-Platform": ascii_header(env.get("KIMI_MSH_PLATFORM", KIMI_DATASOURCE_PLATFORM)),
        "X-Msh-Version": ascii_header(env.get("KIMI_MSH_VERSION", version)),
        "X-Msh-Device-Name": ascii_header(env.get("KIMI_MSH_DEVICE_NAME", device_name())),
        "X-Msh-Device-Model": ascii_header(env.get("KIMI_MSH_DEVICE_MODEL", device_model())),
        "X-Msh-Os-Version": ascii_header(env.get("KIMI_MSH_OS_VERSION", device_os_version())),
        "X-Msh-Device-Id": ascii_header(env.get("KIMI_MSH_DEVICE_ID", device_id)),
        "User-Agent": f"kimi-datasource/{version}",
    }


def moonshot_headers(
    token: str,
    device_id: str,
    version: str = KIMI_CODE_CLI_VERSION,
    *,
    accept: str = "",
) -> dict[str, str]:
    env = os.environ
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Msh-Tool-Call-Id": str(uuid4()),
        "X-Msh-Platform": ascii_header(env.get("KIMI_MSH_PLATFORM", KIMI_OAUTH_PLATFORM)),
        "X-Msh-Version": ascii_header(env.get("KIMI_MSH_VERSION", version)),
        "X-Msh-Device-Name": ascii_header(env.get("KIMI_MSH_DEVICE_NAME", device_name())),
        "X-Msh-Device-Model": ascii_header(env.get("KIMI_MSH_DEVICE_MODEL", device_model())),
        "X-Msh-Os-Version": ascii_header(env.get("KIMI_MSH_OS_VERSION", device_os_version())),
        "X-Msh-Device-Id": ascii_header(env.get("KIMI_MSH_DEVICE_ID", device_id)),
        # 官方 search/fetch 由 CLI 直发，UA 产品位是 kimi-code-cli
        "User-Agent": f"kimi-code-cli/{version}",
    }
    if accept:
        headers["Accept"] = accept
    return headers
