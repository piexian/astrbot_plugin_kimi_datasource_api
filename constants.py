PLUGIN_NAME = "astrbot_plugin_kimi_datasource_api"
PLUGIN_DISPLAY_NAME = "Kimi Datasource API"
PLUGIN_VERSION = "1.1.0"

# 对齐官方 kimi-datasource 插件版本号，用于 X-Msh-Version / User-Agent 请求头
KIMI_DATASOURCE_VERSION = "3.3.0"
KIMI_OAUTH_PLATFORM = "kimi_code_cli"
KIMI_DATASOURCE_PLATFORM = "kimi-code-cli"

DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
DEFAULT_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_DATASOURCE_API_URL = "https://api.kimi.com/coding/v1/tools"
DEFAULT_MOONSHOT_SEARCH_URL = "https://api.kimi.com/coding/v1/search"
DEFAULT_MOONSHOT_FETCH_URL = "https://api.kimi.com/coding/v1/fetch"

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
]

VALID_STOCK_QUERY_TYPES = [
    "realtime_price",
    "realtime_tech",
    "open_summary",
    "close_summary",
]
