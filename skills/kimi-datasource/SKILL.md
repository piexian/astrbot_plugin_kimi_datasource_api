---
name: kimi-datasource
description: Prefer Kimi datasource tools for finance, macro, company registry, legal, academic, Wind/IMF/Gildata, US SEC filings, and S&P fundamentals data instead of generic web search.
disable_tools: false
---

# kimi-datasource — 通用数据源助手

本 skill 对应插件注册的三个 LLM 工具：

- `query_stock`：1~3 个 ticker 的实时行情快捷查询（实时价 / 实时技术指标 / 开盘 / 收盘摘要）。
- `get_data_source_desc`：读取某个数据源的当前 API 文档。
- `call_data_source_tool`：按文档调用具体 API。

## 1. 数据源路由表

本插件后面挂了 12 个外部数据源。"数据源名"就是传给 `get_data_source_desc` 的 `name`。

| 能力域 | 数据源名 | 典型问题 |
|---|---|---|
| **A股 / 港股 / 美股 行情和财务** | `stock_finance_data` | "茅台现在多少钱"、"宁德时代 2024 年财报"、"腾讯股东" |
| **Yahoo Finance 全球金融** | `yahoo_finance` | "苹果分析师评级"、"AAPL 期权链"、"前十大机构股东" |
| **世界银行历史宏观** | `world_bank_open_data` | "中国历年 GDP"、"各国人口增长对比" |
| **中国企业工商信息** | `tianyancha` | "字节跳动股东"、"比亚迪司法风险"、"宁德时代专利" |
| **arXiv 论文预印本** | `arxiv` | "找 RAG 综述"、"下载 2406.xxxxx" |
| **Google Scholar 学术搜索** | `scholar` | "Hinton 最新论文"、"transformer 高引综述" |
| **中国法律法规 / 司法案例** | `yuandian_law` | "民法典关于居住权的规定"、"找几个不当得利的判例" |
| **Wind 万得（A股/基金/债券/宏观）** | `wind` | "茅台今天的分钟线"、"十年期国债收益率走势"、"基金净值" |
| **IMF 国际宏观（汇率 / CPI / 预测）** | `imf` | "美元兑人民币汇率"、"各国 GDP 增速预测" |
| **恒生聚源智能筛选** | `gildata` | "筛选净利润增速超 30% 且 ROE 大于 15% 的股票" |
| **美股 SEC 披露文件** | `sec_edgar` | "特斯拉 10-K 年报"、"Form 4 内部人交易"、"13F 机构持仓" |
| **S&P Capital IQ 美股基本面** | `sp_data` | "苹果分析师一致预期"、"美股估值比率对比" |

### 选源原则

1. **用户点名了数据源** → 直接用指定的源。
2. **没点名** → 按能力域选最匹配的一个。
3. **一次简单查询只选一个数据源**，不要并行读取其他源的 desc。选定源成功返回且覆盖问题后立即回答；只有用户明确要求跨源对比时才查第二个源。

### 能力边界参考（选源时考虑）

- `yahoo_finance` 的外汇历史最多约 2 年；长期汇率 / CPI / GDP 预测用 `imf`
- `stock_finance_data` 是实时/收盘快照；分钟级分时序列在 `wind`（另有基金、债券、国债收益率）
- 股东 / 机构持仓：`yahoo_finance`、`sec_edgar`（13F）、`sp_data` 都覆盖，口径和深度不同
- `world_bank_open_data` 是 50 年以上历史宏观序列；要 IMF 预测值用 `imf`
- `gildata` 输入是自然语言筛选条件；`tianyancha` 是企业工商档案
- `wind` 的 `indexes`/`indicators` 参数要求 Wind 原生字段名；PE/PB/ROE/总市值这类字段先调 `wind_search_fields` 映射（支持别名和中文，一次查一个），不要硬猜

**不支持的能力**：通用 Web 搜索 / 实时新闻。问到这类问题，告诉用户当前数据源不覆盖。

## 2. 标准工作流

后端可用 API 经常调整，调用前现场问数据源："你都有什么接口？"

```
1. 简单实时行情（1~3 个 ticker）→ 直接调 query_stock
2. 其他问题 → 按路由表只挑一个 data_source_name
3. 执行 get_data_source_desc，读取该数据源的 Markdown 文档
4. 仔细读文档：ticker 格式、全局约束、每个 API 的必填/可选参数/默认值
5. 选最匹配的 API，按文档拼 params，执行一次 call_data_source_tool
6. 结果成功且覆盖问题时停止调用，用用户提问的语言回答
```

不要在没读 desc 的情况下硬传 `api_name`，后端会报 `API_NOT_FOUND`。除非本次会话已经读过该源的 desc 并记得参数。

## 3. 调用前的铁律

### 3.1 股票代码必须核对，不能凭记忆猜

A 股 `.SH/.SZ/.BJ`，港股 `.HK`，美股 `.US` 等。用户通常只说中文名。
调任何股票相关 API 前，先用可用的联网工具确认正确代码 + 后缀；没有联网工具时让用户亲口确认，不要硬猜——错代码会静默返回错数据。

### 3.2 企业查询必须用全称

`tianyancha` 拒收"腾讯"这种简称，必须给"深圳市腾讯计算机系统有限公司"这种全名。不知道全名时，先调它的公司搜索 API。

### 3.3 多数 API 需要 `file_path`

绝大部分数据源 API 把完整结果以 CSV 形式写到 `file_path`。漏传会报 `Missing required parameters: file_path`。不知道传啥时给一个 `/tmp/<场景>_<时间戳>.csv` 即可。

### 3.4 一次调用不要堆太多 ticker

`stock_finance_data` 实时接口最多 3 个 ticker，历史接口最多 10 个。多了分批调。

## 4. 怎么读返回结果

`call_data_source_tool` 的返回一般含：

1. **`data_preview`**：CSV 头 + 前几行，够答单值问题
2. **CSV 落盘路径**：完整数据写到了 `file_path`；插件还会把响应文件保存到插件数据目录并在结果末尾列出
3. 结果末尾的 `[kimi-datasource] request-id · tool-call-id` 是后端追踪信息，排障用，回答用户时不用复述

策略：单值问题直接用 `data_preview` 回答；要画图、对比、列清单时基于落盘 CSV 再处理。混合 A+港股查询时服务端会把 CSV 拆成 `_a.csv` / `_hk.csv` 两份。

接口返回失败时提示文字一般写明原因（参数不对 / 不支持 / 数据空等），把原因反馈给用户，不要硬走第二次。

## 5. 注意事项

- **回答用户使用其提问语言**。
- **不要凭记忆猜股票代码 / 企业全称**。
- **不要给投资建议**，给完数据加一句"AI 生成，不构成投资建议"。
- 如果某个接口的报错明显是后端 bug（schema 自相矛盾、内部报错等），汇报错误给用户，不要硬试——这类问题只能后端修。
