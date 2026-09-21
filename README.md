# Kimi 专业工具 (astrbot_plugin_kimi_datasource_api)

为 AstrBot LLM 提供 Kimi datasource 专业数据库工具。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.11 | |
| AstrBot | >= v4.13.0 | 指令、插件 KV、文件上传配置与 LLM Tool |
| aiohttp | >= 3.9 | HTTP 客户端 |

**平台支持**: 可能是全平台？

## 功能

- `kimi help` 指令 - 查看 Kimi datasource 指令帮助
- `kimi login [账号ID] [月重置时间]` 指令 - 管理员登录，可绑定每月几号或完整日期时间
- `kimi import-local [账号ID]` 指令 - 管理员导入本机已登录的 Kimi Code 凭证（Linux / macOS / Windows）
- `kimi status` 指令 - 查看全部账号凭据状态、额度已用/剩余比例及重置时间
- `kimi refresh [账号ID] [月重置时间]` 指令 - 刷新并复核账号；指定账号时可更新月度绑定
- `kimi logout <账号ID|--all>` 指令 - 管理员删除指定或全部 Kimi OAuth 账号
- 多 OAuth 轮转 - LLM Tool 调用时在有效账号间轮转，失效账号自动跳过
- 内置 Skill (`kimi-datasource`) - 引导模型优先使用 datasource 工具查询财经、宏观、企业、法律、学术、国标、国际组织、财经资讯等 25 个数据源
- LLM Tool (`query_stock`) - 查询最多 3 个股票代码的实时价格、技术指标、开盘/收盘摘要
- LLM Tool (`get_data_source_desc`) - 获取 25 个数据源中指定一个的当前 API 文档
- LLM Tool (`call_data_source_tool`) - 按 datasource 文档调用具体 API
- LLM Tool (`moonshot_search`) - 通过 Kimi Code Moonshot search 使用 Kimi OAuth 执行网页检索
- LLM Tool (`moonshot_fetch`) - 通过 Kimi Code Moonshot fetch 抓取 URL 正文，远端失败时本地兜底
- 自动 refresh - 调用前刷新即将过期的 token；与本机 CLI 回读、同步关联凭据，减少重复刷新
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
| `api_url` | string | 否 | Kimi datasource API endpoint（默认: https://api.kimi.com/coding/v1/tools；global 区填 https://api.kimi.ai/coding/v1/tools）。search/fetch 和额度接口 `/usages` 由该地址派生；仅 search/fetch 可用 `KIMI_WEB_SEARCH_BASE_URL` / `KIMI_WEB_FETCH_BASE_URL` 覆盖 |
| `response_parse_mode` | string | 否 | 响应解析模式：`official`（assistant 通道优先，同官方）/ `legacy_zip`（user 通道优先，只要干净 data_preview）（默认: official） |
| `save_response_files` | bool | 否 | 保存上游 `response.files` 到插件数据目录（默认: true） |

### 账号设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `credential_imports` | file | 否 | 凭据文件列表；上传或在文件界面添加到配置，保存后启用。登录自动加入，刷新同步更新。 |
| `monthly_cooldown_minutes` | int | 否 | 无未来月度绑定或到点复核仍失败时的冷却时长，默认 60 分钟，范围 1–1440。 |

凭据文件位于插件数据目录的 `files/account_settings/credential_imports/`，保存 token、设备、环境和月度绑定。列表即启用来源，不需要另填账号或文件路径。内部恢复文件保留最新 token 与可信 CLI 关联，不能绕过文件列表启用账号。

旧 KV 和前版受管文件自动接入此列表，无需重新登录；旧字段仅隐藏保留用于兼容，迁移完成后清空。先校验文件并保存列表，再清除旧 KV，失败可重试。更新后重载插件即可；若检测到旧请求仍写 KV，会停止使用并明确报错。

文件界面删除会立即删除磁盘文件，后续调用停止使用该账号。替换已有凭据时，先删除并保存配置，再上传新文件；不要在刷新期间同名覆盖。冲突时停用受影响文件，最新 token 保留在内部 `credentials/`；需要恢复时可按上述替换流程上传该副本。

上传支持插件 JSON 或 Kimi Code CLI JSON（需带 `device_id` 或 token 中含该字段），最大 64 KiB；拒绝重复字段、路径链接和不匹配环境，忽略外部回写路径。自定义环境的 CLI 文件须保留官方 `kimi-code-env-*.json` 名称。与正在使用的本机 CLI 共存请用 `import-local`，不要并行使用上传后的独立副本。

文件原子更新并保留未完成更新的恢复记录；文件损坏或冲突会停止使用，不回退旧 token。落盘失败时先修复权限/空间再重试，不要立即重载；磁盘完全不可写时无法保证新 token 跨进程恢复。降级必须先停止调用并从最新文件反向迁移，不能恢复旧 KV 快照。


## 使用

### 登录

管理员发送。命令前缀取决于 AstrBot 的 `wake_prefix` 配置，下面按常见默认前缀 `/` 举例：

```text
/kimi login
/kimi login my-account
/kimi login my-account 22
/kimi import-local
/kimi import-local local-kimi
```

插件会返回 Kimi 授权链接、备用验证码和剩余时间。用户在浏览器完成授权后无需再发消息，插件会后台轮询并自动保存凭证。未指定账号 ID 时会自动分配 `account-N`。

未传月度参数且尚无绑定时，凭据先保存，再向发起者询问；填 `22`、`22日`、`22号` 或完整日期时间，`skip`/取消/两分钟超时均保留登录。`kimi login my-account skip` 可直接跳过询问。

同一系统用户已登录 Kimi Code 时，可用 `kimi import-local [账号ID]` 导入，默认 ID 为 `local-kimi-code`。插件保留来源路径，回读 CLI 最新 token 并原子回写；目录锁沿用官方行为，Windows 或 `KIMI_DISABLE_OAUTH_LOCK=1` 时跳过，不能保证跨进程刷新绝无竞争。

`kimi import-local` 会按顺序检查这些位置：

- `KIMI_CODE_HOME` 或 `KIMI_HOME` 指向的目录
- Linux: `~/.kimi-code`
- macOS: `~/Library/Application Support/kimi-code`、`~/Library/Application Support/Kimi Code`
- Windows: `%APPDATA%\kimi-code`、`%APPDATA%\Kimi Code`、`%LOCALAPPDATA%\kimi-code`、`%LOCALAPPDATA%\Kimi Code`、`%USERPROFILE%\.kimi-code`

每个目录下优先读取 `credentials/kimi-code.json`；不存在时回退到 env 隔离凭证 `credentials/kimi-code-env-*.json`（优先匹配当前 `oauth_host` + `api_url` 对应的环境）。同目录下存在 `device_id` 也会一并导入。

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
/kimi refresh my-account 22
/kimi refresh my-account 2026-09-22 14:26:58 +08:00
/kimi logout my-account
/kimi logout --all
```

状态仅显示脱敏 token 和凭据文件路径。账号 ID 用于命令；账号启用统一在“凭据文件”中管理，`kimi logout` 删除绑定文件及内部恢复记录，保留 CLI 来源文件。

`kimi status` 查询官方 `/usages`，展示 5 小时、7 天、月度总额度和月度代码额度的已用/剩余比例及重置时间（实例本地时区）。缺失字段标为“未知”，不代表仍有额度；单账号查询失败不影响其他账号。查询前沿用自动刷新机制，`revoked` 账号不自动恢复。

旧版误标为 `revoked` 的账号可由管理员执行 `kimi refresh <账号ID>` 复核；只有刷新成功才恢复，失败则保留原状态。不带账号 ID 的批量刷新仍跳过 `revoked`。

指定账号刷新时可附带月重置输入，服务端刷新成功后与新 token 一起落盘；省略则保留已有绑定。只填几号按北京时间记录“每月该日，具体时刻未知”；完整时间可附时区，后续月份仅标为预计，月末不足该日时取当月最后一天。


### 多账号轮转

多个账号登录后，LLM Tool 按账号 ID 轮转。只有 OAuth 刷新明确被拒绝，或工具接口刷新重试后仍返回 401，才会标记为 `revoked` 并尝试下一个账号。

工具接口 403（额度或权限限制）不刷新或吊销账号，继续尝试其他账号；全部不可用时保留明确原因。月度额度耗尽需等待官方计费周期重置或购买额外额度，重复登录无法恢复。

月度额度耗尽会为账号持久化冷却：有未来绑定就等到该边界，否则默认 60 分钟。只知道日期时，从该日开始允许按需复核，绝不宣称零点恢复；失败后仍按 60 分钟复核，不直接顺延到下个月。数据源、搜索和远程抓取共同跳过冷却账号，到期仅放行一次业务请求，成功才解除，不后台轮询。

`kimi status` 同时展示冷却和官方用量。5 小时/周额度有余量、查询用量成功、刷新或重新登录同一账号都不会清除冷却；删除账号会清理其冷却记录。官方未提供月度重置时间时明确标为未提供，不用周额度重置时间或订阅到期、续费时间代替。

月度输入属于人工绑定，与官方额度分开标注；不从年费续费日期推算，不保存网页 Token/Cookie。当前网页 `GetSubscriptionStats` 不接受 Code OAuth，需参考官方页面后填写。

### 内置 Skill

插件随包提供只读 Skill：`kimi-datasource`。用于提示模型在下列场景优先调用本插件的 datasource tools，而不是直接泛化联网搜索：

- 股票、财报、估值指标、公司分部、股价历史、期权和持仓
- World Bank / IMF 宏观经济、汇率、CPI、GDP 预测与国际收支
- 天眼查企业工商、股东、司法风险、知识产权和经营数据
- 元典中国法律法规与司法案例
- arXiv / Scholar 论文检索和作者信息
- Wind A股分钟线、基金、债券；恒生聚源自然语言选股
- 美股 SEC 披露文件（10-K/10-Q、Form 4、13F）与 S&P Capital IQ 基本面

### LLM Tool

插件会注册三个 datasource 工具和两个 Kimi 网页工具；是否允许模型调用工具由 AstrBot 内部工具控制负责。
Moonshot 网页工具复用同一套 OAuth 账号池和设备头。

| 工具名 | 用途 |
|--------|------|
| `query_stock` | 查询实时股票数据，最多 3 个 ticker |
| `get_data_source_desc` | 获取 25 个数据源中指定一个的当前 API 文档 |
| `call_data_source_tool` | 按文档调用具体 datasource API |
| `moonshot_search` | 调用 Kimi Code Moonshot search；返回标题/链接/站点/日期/摘要，`include_content` 可附带全文 |
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
├── usage.py             # 官方额度查询与状态展示
├── cooldown.py          # 月度额度冷却与单次复核
├── monthly.py           # 月度绑定解析与日历边界
├── credential_files.py  # 凭据校验、原子更新与恢复
├── storage.py           # 凭据文件、KV 迁移与账号管理
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
