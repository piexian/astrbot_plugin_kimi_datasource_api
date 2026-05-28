# Changelog

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
