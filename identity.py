from __future__ import annotations

import os
import platform
import socket
from uuid import uuid4

from .constants import (
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


def device_model() -> str:
    system = platform.system() or platform.platform() or "unknown"
    release = platform.release()
    machine = platform.machine()
    if system == "Darwin":
        return ascii_header(f"macOS {release} {machine}")
    if system == "Windows":
        return ascii_header(f"Windows {release} {machine}")
    return ascii_header(f"{system} {release} {machine}")


def device_os_version() -> str:
    return ascii_header(platform.release(), "unknown")


def oauth_device_headers(device_id: str, version: str) -> dict[str, str]:
    return {
        "X-Msh-Platform": KIMI_OAUTH_PLATFORM,
        "X-Msh-Version": ascii_header(version),
        "X-Msh-Device-Name": device_name(),
        "X-Msh-Device-Model": device_model(),
        "X-Msh-Os-Version": device_os_version(),
        "X-Msh-Device-Id": ascii_header(device_id),
    }


def datasource_headers(token: str, device_id: str, version: str = KIMI_DATASOURCE_VERSION) -> dict[str, str]:
    env = os.environ
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Msh-Tool-Call-Id": str(uuid4()),
        "X-Msh-Platform": ascii_header(env.get("KIMI_MSH_PLATFORM", KIMI_DATASOURCE_PLATFORM)),
        "X-Msh-Version": ascii_header(env.get("KIMI_MSH_VERSION", version)),
        "X-Msh-Device-Name": device_name(),
        "X-Msh-Device-Model": device_model(),
        "X-Msh-Os-Version": device_os_version(),
        "X-Msh-Device-Id": ascii_header(env.get("KIMI_MSH_DEVICE_ID", device_id)),
        "User-Agent": f"kimi-datasource/{version}",
    }


def moonshot_headers(
    token: str,
    device_id: str,
    version: str = KIMI_DATASOURCE_VERSION,
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
        "X-Msh-Device-Name": device_name(),
        "X-Msh-Device-Model": device_model(),
        "X-Msh-Os-Version": device_os_version(),
        "X-Msh-Device-Id": ascii_header(env.get("KIMI_MSH_DEVICE_ID", device_id)),
        "User-Agent": f"kimi-code/{version}",
    }
    if accept:
        headers["Accept"] = accept
    return headers
