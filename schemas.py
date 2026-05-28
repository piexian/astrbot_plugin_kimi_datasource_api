from __future__ import annotations

from .constants import KNOWN_DATA_SOURCES, VALID_STOCK_QUERY_TYPES

QUERY_STOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {
            "type": "string",
            "description": "Ticker code list separated by commas, for example 600519.SH or 0700.HK.",
        },
        "type": {
            "type": "string",
            "enum": VALID_STOCK_QUERY_TYPES,
            "description": "Realtime stock query type.",
        },
        "time": {
            "type": "string",
            "description": "Optional time parameter for supported realtime endpoints.",
        },
        "file_path": {
            "type": "string",
            "description": "Optional CSV output path. When omitted, the tool chooses a temporary path.",
        },
    },
    "required": ["ticker"],
}

GET_DATA_SOURCE_DESC_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "enum": KNOWN_DATA_SOURCES,
            "description": "Data source name.",
        },
    },
    "required": ["name"],
}

CALL_DATA_SOURCE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "data_source_name": {
            "type": "string",
            "description": "Data source name returned or documented by get_data_source_desc.",
        },
        "api_name": {
            "type": "string",
            "description": "API name from the data source description.",
        },
        "params": {
            "type": "object",
            "description": "API parameters that match the data source description.",
        },
    },
    "required": ["data_source_name", "api_name", "params"],
}
