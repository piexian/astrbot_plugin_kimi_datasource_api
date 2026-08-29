# Changelog

## v1.1.0

- 对齐官方 kimi-datasource 3.3.0：`get_data_source_desc` 数据源枚举补齐至 12 个，新增 `yuandian_law`、`wind`、`imf`、`gildata`、`sec_edgar`、`sp_data`（已在线验证后端可用）。
- 同步官方 3.3.0 工具描述：单源选择、成功即停、用户点名直达等路由规则与能力边界。
- datasource 结果末尾追加 `[kimi-datasource] request-id · tool-call-id` trace 行（官方 3.2.0 行为），便于关联后端日志。
- `official` 响应解析在 user 通道为空时回退 assistant 通道，避免误报无文本。
- `kimi import-local` 支持 env 隔离凭证 `kimi-code-env-*.json` 回退导入。
- 请求头版本号升级为 kimi-datasource 3.3.0；内置 Skill 重写为 12 源路由表与选源铁律。
- `query_stock` 保留：已在线验证其后端方法 `get_stock_realtime_price` 仍然可用。
- 对齐 kimi-code 0.39.1 搜索/抓取：`moonshot_search` 请求体瘦身（服务端已忽略 `limit` 等字段，该参数移除），结果新增站点名，`content` 全文仅在 `include_content=true` 时输出；`moonshot_fetch` 输出增加来源说明与引用提示。

## v1.0.1

- 新增 `moonshot_search` LLM Tool，复刻 Kimi Code Moonshot search 请求链路。
- 新增 `moonshot_fetch` LLM Tool，复刻 Kimi Code Moonshot fetch 请求链路，并在远端失败时回落到本地抓取。
- Moonshot 工具复用现有 Kimi OAuth 账号池、token refresh、多账号轮转和设备头。
- 补充 README 中的 Moonshot 工具说明。

## v1.0.0

- 首次发布 Kimi Datasource API 插件。
- 支持 Kimi Code OAuth device-code 登录、token 自动刷新和多账号轮转。
- 支持导入本机 Kimi Code 凭证，覆盖 Linux、macOS 和 Windows 常见路径。
- 注册 `query_stock`、`get_data_source_desc`、`call_data_source_tool` 三个 datasource LLM tools。
- 支持 datasource 响应文件落盘、账号状态查看、手动 refresh、logout 和配置同步。
