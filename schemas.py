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
            "description": (
                "Data source name. Capabilities: stock_finance_data / yahoo_finance = general quotes and financials "
                "(yahoo_finance FX history is limited to about 2 years); world_bank_open_data = historical macro; "
                "imf = FX rates, CPI, GDP forecasts, balance of payments; tianyancha = CN company registry; "
                "arxiv / scholar = papers; yuandian_law = CN laws and cases; "
                "wind = A-share intraday minute series, funds, bonds (map PE/PB/ROE-style field names via wind_search_fields first); "
                "gildata = natural-language stock/fund screening; "
                "sec_edgar = US filings (10-K/10-Q, S-1, Form 4, 13F, 8-K); "
                "sp_data = S&P fundamentals (consensus estimates, valuation ratios, transcripts)."
            ),
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

MOONSHOT_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The query text to search for.",
        },
        "include_content": {
            "type": "boolean",
            "description": (
                "Include the full page content of every result in the output. Defaults to false; "
                "prefer reading a single relevant page with moonshot_fetch instead."
            ),
        },
    },
    "required": ["query"],
}

MOONSHOT_FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "HTTP or HTTPS URL to fetch.",
        },
    },
    "required": ["url"],
}
