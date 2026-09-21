# Changelog

## v1.3.1

- 修复工具接口 403 被误判为登录失效：额度或权限不足不再刷新、吊销账号；普通权限拒绝继续轮换其他账号，并保留脱敏错误说明。
- `kimi status` 显示官方额度的已用/剩余比例及重置时间；未返回的额度标为未知，单账号查询失败不影响其他账号。
- 月度额度耗尽后按账号持久化冷却，默认 60 分钟；到期按需单次复核，`status` 显示冷却原因与复核时间，其他额度余量及 token 刷新不解除冷却。
- `kimi refresh <账号ID>` 支持复核误标账号，服务端刷新成功后恢复；批量刷新仍跳过 `revoked`。
- 统一为原生“凭据文件”列表，上传保存即启用，登录自动加入、刷新同步更新；旧 KV 与前版文件自动接入，旧配置隐藏并清空。
- 登录与指定账号刷新可绑定月重置时间，支持只填几号；时刻未知不冒充零点，绑定边界后按需复核，失败回退本地冷却。
- 内部凭据原子落盘与恢复记录保护刷新结果，界面删除或覆写冲突不会自动回退旧 token，保留 CLI 共存；最低 AstrBot 版本为 4.13.0。

## v1.3.0

- 对齐 kimi-code CLI 2.0.0：CLI 版本位请求头升级为 2.0.0；OAuth device/token 请求补齐 `User-Agent: kimi-code-cli/<ver>`（官方 2.0.0 起 `createKimiDefaultHeaders` 行为）。
- datasource / search / fetch 设备指纹头补齐 `KIMI_MSH_DEVICE_NAME` / `KIMI_MSH_DEVICE_MODEL` / `KIMI_MSH_OS_VERSION` 环境覆盖（官方 mjs 同款）；Windows 下 X-Msh-Os-Version / Device-Model 改发内核版本号（对齐 Node `os.release()`）。
- 刷新锁支持官方 `KIMI_DISABLE_OAUTH_LOCK=1` 停用开关。
- 更正：官方 2.0.0 起 refresh 响应缺 refresh_token 会直接报错；本插件“沿用旧值”自此为刻意的防御性偏离，防止服务端省略时把账号刷死。

## v1.2.0

- 对齐官方 kimi-datasource 3.4.0：数据源枚举 12 → 25，新增 `china_nda`、`china_nbs`、`china_standards`、`who`、`fao`、`unsd`、`ecb`、`eurostat`、`unicef`、`oecd`、`fred`、`xhcj`、`caixin`（已在线验证 25 源全部可达，`china_nbs`/`fred`/`caixin` 实测返回真实数据）。
- 同步官方 3.4.0 工具与 schema 描述：`stop once a result covers the user's question`、`data_source_name` 字段说明、逐源能力边界；内置 Skill 改为 25 源路由表，发现类调用（`caixin_api_search`、`wind_search_fields`、天眼查公司搜索）不再受“一次调用”限制。
- 修复响应通道优先级：`official` 改为 assistant 优先、user 兜底（与官方 `extractChannelText` 一致）。此前 user 优先会让 `caixin` 这类只回空 `data_preview` 的源丢掉正文；旧行为保留为 `legacy_zip`。
- 请求头版本位升级为 kimi-datasource 3.4.0；Moonshot search/fetch 改用 CLI 版本位 `kimi-code-cli/0.42.0`，并对齐官方由 base URL 派生 `/search`、`/fetch`，支持 `KIMI_WEB_SEARCH_BASE_URL` / `KIMI_WEB_FETCH_BASE_URL` 覆盖。
- refresh 响应未轮换 `refresh_token` 时沿用旧值（对齐官方 token 事务层），不再整次刷新失败。
- 本机凭证共存：`kimi import-local` 记录来源文件，刷新前抢官方同款 `oauth/<name>.lock` 跨进程锁并回读，若 CLI 已轮换则直接采纳；刷新成功后原子回写来源文件，避免与同机 kimi-code CLI 互相吊销。
- 凭证导入诊断支持 kimi-code region profiles：识别 `mainland-cn`（.com）与 `global`（`auth.kimi.ai` / `api.kimi.ai`）登录，环境失配时报出实际 region。

## v1.1.0

- 对齐官方 kimi-datasource 3.3.0：`get_data_source_desc` 数据源枚举补齐至 12 个，新增 `yuandian_law`、`wind`、`imf`、`gildata`、`sec_edgar`、`sp_data`（已在线验证后端可用）。
- 同步官方 3.3.0 工具描述：单源选择、成功即停、用户点名直达等路由规则与能力边界。
- datasource 结果末尾追加 `[kimi-datasource] request-id · tool-call-id` trace 行（官方 3.2.0 行为），便于关联后端日志。
- `official` 响应解析在 user 通道为空时回退 assistant 通道，避免误报无文本。
- `kimi import-local` 支持 env 隔离凭证 `kimi-code-env-*.json` 回退导入。
- 请求头版本号升级为 kimi-datasource 3.3.0；内置 Skill 重写为 12 源路由表与选源铁律。
- `query_stock` 保留：已在线验证其后端方法 `get_stock_realtime_price` 仍然可用。
- 对齐 kimi-code 0.39.1 搜索/抓取：`moonshot_search` 请求体瘦身（服务端已忽略 `limit` 等字段，该参数移除），结果新增站点名，`content` 全文仅在 `include_content=true` 时输出；`moonshot_fetch` 输出增加来源说明与引用提示。
- 修复：`import-local` 在 env 凭证与当前 `api_url`/`oauth_host` 环境失配时不再盲目导入唯一变体（避免错环境 token 被吊销），报错列出期望凭证文件名。

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
