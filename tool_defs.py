from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.tool import FunctionTool


@dataclass(config={"arbitrary_types_allowed": True})
class KimiFunctionTool(FunctionTool):
    plugin: Any = None

    async def call(self, context, **kwargs):
        if self.plugin is None:
            raise RuntimeError("KimiFunctionTool plugin is not attached.")

        if self.name == "query_stock":
            return await self.plugin._tool_query_stock(**kwargs)
        if self.name == "get_data_source_desc":
            return await self.plugin._tool_get_data_source_desc(**kwargs)
        if self.name == "call_data_source_tool":
            return await self.plugin._tool_call_data_source_tool(**kwargs)
        if self.name == "moonshot_search":
            return await self.plugin._tool_moonshot_search(**kwargs)
        if self.name == "moonshot_fetch":
            return await self.plugin._tool_moonshot_fetch(**kwargs)
        raise RuntimeError(f"Unknown tool: {self.name}")
