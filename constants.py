PLUGIN_NAME = "astrbot_plugin_kimi_datasource_api"
PLUGIN_DISPLAY_NAME = "Kimi Datasource API"
PLUGIN_VERSION = "1.3.0"

# 对齐官方 kimi-datasource 插件版本号，用于 X-Msh-Version / User-Agent 请求头
KIMI_DATASOURCE_VERSION = "3.4.0"
# moonshot search/fetch 与 OAuth 设备头由 kimi-code CLI 直发，参考版本用于头里的版本位
KIMI_CODE_CLI_VERSION = "2.0.0"
KIMI_OAUTH_PLATFORM = "kimi_code_cli"
KIMI_DATASOURCE_PLATFORM = "kimi-code-cli"

DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
DEFAULT_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_DATASOURCE_API_URL = "https://api.kimi.com/coding/v1/tools"
DEFAULT_MOONSHOT_SEARCH_URL = "https://api.kimi.com/coding/v1/search"
DEFAULT_MOONSHOT_FETCH_URL = "https://api.kimi.com/coding/v1/fetch"

# 对齐 kimi-code region profiles：默认 mainland-cn，global 走 .ai 部署
KIMI_REGION_PROFILES = {
    "mainland-cn": {
        "oauth_host": "https://auth.kimi.com",
        "base_url": "https://api.kimi.com/coding/v1",
    },
    "global": {
        "oauth_host": "https://auth.kimi.ai",
        "base_url": "https://api.kimi.ai/coding/v1",
    },
}

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_LOGIN_TIMEOUT_SECONDS = 15 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 5

KNOWN_DATA_SOURCES = [
    "stock_finance_data",
    "yahoo_finance",
    "world_bank_open_data",
    "tianyancha",
    "arxiv",
    "scholar",
    "yuandian_law",
    "wind",
    "imf",
    "gildata",
    "sec_edgar",
    "sp_data",
    "china_nda",
    "china_nbs",
    "china_standards",
    "who",
    "fao",
    "unsd",
    "ecb",
    "eurostat",
    "unicef",
    "oecd",
    "fred",
    "xhcj",
    "caixin",
]

VALID_STOCK_QUERY_TYPES = [
    "realtime_price",
    "realtime_tech",
    "open_summary",
    "close_summary",
]
