"""Isolated credentials and HTTP responses for plugin regression tests."""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kimi_datasource_api.storage import KimiCredentialStore


class MemoryOwner:
    def __init__(self):
        self.data = {
            "kimi_code.device_id": "test-device",
            "kimi_code.accounts": {
                "test": {
                    "status": "valid",
                    "access_token": "test-access-secret",
                    "refresh_token": "test-refresh-secret",
                    "expires_at": int(time.time()) + 900,
                    "expires_in": 900,
                    "scope": "kimi-code",
                    "token_type": "Bearer",
                }
            },
        }

    async def get_kv_data(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    async def put_kv_data(self, key, value):
        self.data[key] = copy.deepcopy(value)

    async def delete_kv_data(self, key):
        self.data.pop(key, None)


@pytest.fixture(autouse=True)
def forbid_unmocked_http(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Unmocked HTTP is forbidden in regression tests")
    monkeypatch.setattr(aiohttp.ClientSession, "_request", reject)


@pytest.fixture
def store():
    return KimiCredentialStore(MemoryOwner())


@pytest.fixture
def http_responses(monkeypatch):
    state = SimpleNamespace(responses=[], calls=[], sessions=[])

    class Response:
        def __init__(self, status, body):
            self.status = status
            self.ok = status < 400
            self.body = body if isinstance(body, str) else json.dumps(body)
            self.headers = {"X-Request-Id": "test-request"}

        async def text(self):
            return self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            state.sessions.append(kwargs)

        def request(self, method, url, **kwargs):
            state.calls.append((method, url, kwargs))
            assert state.responses, "Unexpected HTTP request"
            item = state.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return Response(*item)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return state
