# Kimi Datasource API (astrbot_plugin_kimi_datasource_api)

为 AstrBot LLM 提供 Kimi datasource 专业数据库工具。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.10 | |
| AstrBot | >= v4.9.2 | 指令、插件 KV 与 LLM Tool |
| aiohttp | >= 3.9 | HTTP 客户端 |

**平台支持**: 可能是全平台？

## 功能

- `kimi help` 指令 - 查看 Kimi datasource 指令帮助
- `kimi login [账号ID]` 指令 - 管理员发起 Kimi Code OAuth device-code 登录
- `kimi import-local [账号ID]` 指令 - 管理员导入本机已登录的 Kimi Code 凭证（Linux / macOS / Windows）
- `kimi status` 指令 - 查看全部账号状态、token 过期时间和脱敏 token
- `kimi refresh [账号ID]` 指令 - 管理员强制刷新指定账号，未指定时刷新全部有效账号
- `kimi logout <账号ID|--all>` 指令 - 管理员删除指定或全部 Kimi OAuth 账号
- 多 OAuth 轮转 - LLM Tool 调用时在有效账号间轮转，失效账号自动跳过
- 内置 Skill (`kimi-datasource`) - 引导模型优先使用 datasource 工具查询财经、宏观、企业和学术数据
- LLM Tool (`query_stock`) - 查询最多 3 个股票代码的实时价格、技术指标、开盘/收盘摘要
- LLM Tool (`get_data_source_desc`) - 获取 Kimi datasource 的当前 API 文档
- LLM Tool (`call_data_source_tool`) - 按 datasource 文档调用具体 API
- LLM Tool (`moonshot_search`) - 通过 Kimi Code Moonshot search 使用 Kimi OAuth 执行网页检索
- LLM Tool (`moonshot_fetch`) - 通过 Kimi Code Moonshot fetch 抓取 URL 正文，远端失败时本地兜底
- 自动 refresh - tool 调用前自动检查并刷新即将过期的 access token
- 响应文件落盘 - 将上游 `response.files` 安全保存到插件数据目录

## 安装

### 两种方式

1. 在 AstrBot 插件市场搜索 `Kimi Datasource API` 点击安装
2. 在插件界面右下角点击加号选择从链接安装，输入本仓库地址

## 配置

### OAuth 登录设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `oauth_host` | string | 否 | Kimi OAuth 服务地址（默认: https://auth.kimi.com） |
| `login_timeout_seconds` | int | 否 | 设备码登录总等待时间（默认: 900 秒） |

### 连接设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `request_timeout_seconds` | int | 否 | 单次 OAuth / datasource HTTP 请求超时（默认: 30 秒） |
| `proxy` | string | 否 | HTTP 代理地址（例如: http://127.0.0.1:7890） |

### Datasource 设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `api_url` | string | 否 | Kimi datasource API endpoint（默认: https://api.kimi.com/coding/v1/tools） |
| `response_parse_mode` | string | 否 | 响应解析模式：`official` / `legacy_zip`（默认: official） |
| `save_response_files` | bool | 否 | 保存上游 `response.files` 到插件数据目录（默认: true） |

### 账号设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `account_ids` | list | 否 | 已登录账号 ID。登录成功后自动追加；从列表删除 ID 并保存配置后，插件会删除对应 OAuth 账号。 |

## 使用

### 登录

管理员发送。命令前缀取决于 AstrBot 的 `wake_prefix` 配置，下面按常见默认前缀 `/` 举例：

```text
/kimi login
/kimi login my-account
/kimi import-local
/kimi import-local local-kimi
```

插件会返回 Kimi 授权链接、备用验证码和剩余时间。用户在浏览器完成授权后无需再发消息，插件会后台轮询并自动保存凭证。未指定账号 ID 时会自动分配 `account-N`。

如果运行 AstrBot 的同一系统用户已经登录过 Kimi Code，可以直接执行 `kimi import-local [账号ID]` 导入本地凭证。未指定账号 ID 时默认写入 `local-kimi-code`。

`kimi import-local` 会按顺序检查这些位置：

- `KIMI_CODE_HOME` 或 `KIMI_HOME` 指向的目录
- Linux: `~/.kimi-code`
- macOS: `~/Library/Application Support/kimi-code`、`~/Library/Application Support/Kimi Code`
- Windows: `%APPDATA%\kimi-code`、`%APPDATA%\Kimi Code`、`%LOCALAPPDATA%\kimi-code`、`%LOCALAPPDATA%\Kimi Code`、`%USERPROFILE%\.kimi-code`

每个目录下都期待存在 `credentials/kimi-code.json`，如果同目录下存在 `device_id` 也会一并导入。

登录期间可发送：

```text
cancel
状态
```

重复执行 `kimi login` 会返回当前待授权链接；需要重新发起时使用：

```text
/kimi login --restart
```

### 状态、刷新和退出

```text
/kimi help
/kimi status
/kimi import-local local-kimi
/kimi refresh
/kimi refresh my-account
/kimi logout my-account
/kimi logout --all
```

状态输出只显示脱敏 token。登录成功后，配置文件的 `account_settings.account_ids` 会展示已登录账号 ID；在配置列表里删除某个 ID 并保存后，插件会在初始化、指令执行或工具调用前同步删除对应 KV 凭证。

### 多账号轮转

多个账号登录后，LLM Tool 调用会按账号 ID 轮转选择有效账号。某个账号 refresh 失败或 datasource 返回 401/403 时会标记为 `revoked` 并跳过，继续尝试下一个有效账号。

### 内置 Skill

插件随包提供只读 Skill：`kimi-datasource`。用于提示模型在下列场景优先调用本插件的 datasource tools，而不是直接泛化联网搜索：

- 股票、财报、估值指标、公司分部、股价历史、期权和持仓
- World Bank 宏观经济与社会指标
- 天眼查企业工商、股东、司法风险、知识产权和经营数据
- arXiv / Scholar 论文检索和作者信息

### LLM Tool

插件会注册三个 datasource 工具和两个 Kimi 网页工具；是否允许模型调用工具由 AstrBot 内部工具控制负责。
Moonshot 网页工具复用同一套 OAuth 账号池和设备头。

| 工具名 | 用途 |
|--------|------|
| `query_stock` | 查询实时股票数据，最多 3 个 ticker |
| `get_data_source_desc` | 调用具体 datasource API 前获取当前 API 文档 |
| `call_data_source_tool` | 按文档调用具体 datasource API |
| `moonshot_search` | 调用 Kimi Code Moonshot search，支持结果数量和页面内容抓取开关 |
| `moonshot_fetch` | 调用 Kimi Code Moonshot fetch 抓取 URL 正文，远端失败时回落到本地抓取 |

典型流程是先调用 `get_data_source_desc` 获取数据源文档，再调用 `call_data_source_tool`。
网页检索和 URL 正文展开直接调用 `moonshot_search` / `moonshot_fetch`。


## 项目结构

```text
astrbot_plugin_kimi_datasource_api/
├── main.py              # 插件入口
├── oauth.py             # 登录流程
├── datasource.py        # datasource API
├── moonshot.py          # Kimi search / fetch
├── storage.py           # 插件 KV 凭证结构、设备 ID 与脱敏
├── identity.py          # 设备身份头
├── schemas.py           # 工具 schema
├── sessions.py          # 待登录会话状态
├── tool_defs.py         # llm工具
├── skills/
│   └── kimi-datasource/
│       └── SKILL.md     # 指导模型优先使用 datasource
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项
├── CHANGELOG.md         # 版本日志
└── README.md
```

## 相关链接

- [AstrBot](https://docs.astrbot.app/)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [Kimi Code](https://github.com/MoonshotAI/kimi-code)

## 许可

AGPL-3.0 License
