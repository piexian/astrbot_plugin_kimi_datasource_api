"""Use the real astrbot package from the sibling .venv to reproduce plugin load.

Run with: ../.venv/bin/python tests/test_astrbot_load.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

import astrbot_plugin_kimi_datasource_api.main as main_mod
from astrbot_plugin_kimi_datasource_api.panel_credentials import KimiPanelCredentialStore


class FakeContext:
    def __init__(self) -> None:
        self.tools: list = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)


class FakeKV:
    def __init__(self) -> None:
        self.data: dict = {}

    async def put_kv_data(self, key, value) -> None:
        self.data[key] = value

    async def get_kv_data(self, key, default):
        return self.data.get(key, default)

    async def delete_kv_data(self, key) -> None:
        self.data.pop(key, None)


tmp_context = tempfile.TemporaryDirectory(prefix="astrbot-load-test-")
tmp = tmp_context.name
main_mod.get_astrbot_plugin_data_path = lambda: str(Path(tmp) / "plugin_data")

plugin = main_mod.KimiDatasourcePlugin(FakeContext(), {})
plugin.store = KimiPanelCredentialStore(FakeKV(), credential_root=plugin._plugin_data_dir())
plugin.oauth = plugin._build_oauth_client()
plugin.datasource = plugin._build_datasource_client()
plugin.moonshot = plugin._build_moonshot_client()
plugin.usage = plugin._build_usage_client()

asyncio.run(plugin.initialize())

assert plugin.store.file_ready
assert "kimi_code.accounts" not in plugin.store.owner.data
tools = plugin.context.tools
expected = [
    "query_stock",
    "get_data_source_desc",
    "call_data_source_tool",
    "moonshot_search",
    "moonshot_fetch",
]
assert plugin.usage.store is plugin.store
assert plugin.usage.oauth is plugin.oauth
assert [t.name for t in tools] == expected, [t.name for t in tools]
for tool in tools:
    assert isinstance(tool.description, str), tool.name
asyncio.run(plugin.terminate())
tmp_context.cleanup()
print(f"LOAD_OK tools={len(tools)}: {', '.join(t.name for t in tools)}")
