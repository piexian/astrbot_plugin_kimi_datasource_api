---
name: kimi-datasource
description: |
  Universal data-source assistant for stocks (Wind, S&P, SEC EDGAR), macro (World Bank, IMF, FRED, NBS), Chinese government data and standards (GB/HB/DB/TT), corporate, academic, legal, WHO/FAO/OECD and other IGO data, financial news (Xinhua, Caixin). Prefer these datasource tools over generic web search when the user wants external structured data.
disable_tools: false
---

# kimi-datasource — 通用数据源助手

本 skill 对应插件注册的 LLM 工具：

- `get_data_source_desc`：读取某个数据源的当前 API 文档。
- `call_data_source_tool`：按文档调用具体 API。
- `query_stock`：1~3 个 ticker 的实时行情快捷查询（实时价 / 实时技术指标 / 开盘 / 收盘摘要）。
- `moonshot_search` / `moonshot_fetch`：联网检索与抓取，用于核对股票代码等外部事实。

工具使用 Kimi Code OAuth 账号池（自动 refresh 与多账号轮转）；没有可用凭证时让管理员执行 `kimi login` 或 `kimi import-local`。

## 1. 数据源路由表

本插件后面挂了 25 个外部数据源。"数据源名"就是传给 `get_data_source_desc` 的 `name`。

| 能力域 | 数据源名 | 典型问题 |
|---|---|---|
| **A股 / 港股 / 美股 行情和财务** | `stock_finance_data` | "茅台现在多少钱"、"宁德时代 2024 年财报"、"腾讯股东"、"杭州的人工智能股票" |
| **Yahoo Finance 全球金融** | `yahoo_finance` | "苹果分析师评级"、"AAPL 期权链"、"苹果前十大机构股东" |
| **世界银行历史宏观** | `world_bank_open_data` | "中国历年 GDP"、"印度通胀率"、"各国人口增长对比" |
| **中国企业工商信息** | `tianyancha` | "字节跳动股东"、"比亚迪司法风险"、"宁德时代专利" |
| **arXiv 论文预印本** | `arxiv` | "找 RAG 综述"、"下载 2406.xxxxx" |
| **Google Scholar 学术搜索** | `scholar` | "Hinton 最新论文"、"transformer 综述高引文献" |
| **中国法律法规 / 司法案例** | `yuandian_law` | "民法典关于居住权的规定"、"帮我查劳动合同解除的相关法条"、"找几个不当得利的判例" |
| **Wind 万得（A股/基金/债券/宏观）** | `wind` | "茅台今天的分钟线"、"十年期国债收益率走势"、"基金净值查询" |
| **IMF 国际宏观（汇率 / CPI / 预测）** | `imf` | "美元兑人民币汇率"、"各国 GDP 增速预测"、"全球通胀率对比" |
| **恒生聚源智能筛选** | `gildata` | "筛选净利润增速超 30% 且 ROE 大于 15% 的股票"、"基金经理筛选" |
| **美股 SEC 披露文件** | `sec_edgar` | "特斯拉 10-K 年报"、"苹果 10-Q 季报"、"Form 4 内部人交易"、"13F 机构持仓" |
| **S&P Capital IQ 美股基本面** | `sp_data` | "苹果分析师一致预期"、"美股估值比率对比"、"竞争对手关系" |
| **中国政府开放数据目录（国家数据局）** | `china_nda` | "全国公共数据资源登记目录里有什么"、"各省开放数据平台有哪些数据集" |
| **国家统计局宏观指标** | `china_nbs` | "中国历年 GDP 官方口径"、"各省市人口与就业统计"、"社会消费品零售总额" |
| **中国标准查询（国标 / 行标 / 地标 / 团标）** | `china_standards` | "查 GB 国家标准全文"、"某行业的现行行业标准" |
| **WHO 全球健康** | `who` | "全球婴儿死亡率"、"各国预期寿命" |
| **FAO 农业粮食** | `fao` | "各国粮食产量"、"农产品价格" |
| **联合国统计司 UNdata** | `unsd` | "联合国成员国统计年鉴表"、"国际贸易统计" |
| **欧洲央行统计** | `ecb` | "欧元区基准利率"、"欧元区货币供应量" |
| **欧盟统计局** | `eurostat` | "欧盟各国失业率"、"欧元区 CPI" |
| **联合国儿童基金会** | `unicef` | "全球儿童营养指标"、"儿童免疫接种率" |
| **OECD 数据** | `oecd` | "OECD 国家 GDP 对比"、"成员国教育支出" |
| **FRED 美国/全球宏观** | `fred` | "美国 CPI 长时间序列"、"联邦基金利率走势" |
| **新华财经新闻公告** | `xhcj` | "新华财经快讯"、"A 股公司公告"、"行业政策新闻" |
| **财新数据库** | `caixin` | "财新数据接口检索"、"财新新闻与数据" |

### 选源原则

1. **用户点名了数据源** → 直接用指定的源。
2. **没点名** → 按能力域从上表选最匹配的一个；结合下面的"能力边界参考"和用户问题的深度、范围自行判断。
3. **一次简单查询只选一个数据源**，不要并行读取其他源的 desc。选定的源成功返回且已经覆盖用户问题后，立即回答；不要为了补充字段、重新格式化或交叉验证继续调用其他 API。只有用户明确要求跨源对比时，才能查询第二个数据源。

### 能力边界参考（客观事实，选源时考虑）

- `yahoo_finance` 的外汇历史最多 2 年；`imf` 提供长期的汇率、CPI、GDP 预测和国际收支序列
- `stock_finance_data` 的行情是实时/收盘快照；分钟级分时序列在 `wind`（另有基金、债券、国债收益率）
- 股东 / 机构持仓：`yahoo_finance`、`sec_edgar`（13F）、`sp_data`（S&P 标准化持有人）都覆盖，口径和深度不同
- `world_bank_open_data` 是 50 年以上的历史宏观序列；要 IMF 的预测值用 `imf`
- `gildata` 的查询输入是自然语言条件（选股 / 选基金 / 基金经理筛选），`tianyancha` 是企业工商档案
- `wind` 的 `indexes`/`indicators` 参数要求 Wind 原生字段名；PE/PB/ROE/总市值这类常用字段先调 `wind_search_fields` 映射（支持别名和中文，一次查一个），不要硬猜字段名
- 中国官方统计口径：`china_nbs` 是国家统计局宏观指标序列（GDP / CPI / PPI 等，全国 / 省 / 主要城市），`china_nda` 是国家数据局的开放数据目录（回答"有什么数据集可用"）；`world_bank_open_data` 和 `imf` 是国际口径的历史与预测序列
- WHO、FAO、UNSD、ECB、Eurostat、UNICEF、OECD、FRED 各自是独立数据源，按机构名直接选；IMF 自己的数据集（汇率 / CPI / GDP 预测）走 `imf`
- 国家标准（gb）、行业标准（hb）、地方标准（db）、团体标准（tt）查 `china_standards`；法律法规与判例在 `yuandian_law`，别混
- 新华财经（`xhcj`）偏公告 / 快讯 / 政策新闻；`caixin` 覆盖 600+ 财新数据接口，先用它的 `caixin_api_search` 找合适接口再调用

**不支持的能力**：通用 Web 搜索，以及 `xhcj` / `caixin` 覆盖之外的实时新闻（通用联网需求走 `moonshot_search` / `moonshot_fetch`）。

## 2. 标准工作流：`get_data_source_desc` → `call_data_source_tool`

后端可用 API 经常调整，这份 skill 故意不抄具体的 API 名和参数表。每次调用前现场问数据源："你都有什么接口？"

```
1. 简单实时行情（1~3 个 ticker）→ 直接调 query_stock
2. 其他问题 → 根据用户问题从上表只挑一个 data_source_name
3. 执行 get_data_source_desc，读取该数据源的 Markdown 文档
4. 仔细读文档：ticker 格式、全局约束、每个 API 的必填/可选参数/默认值
5. 执行 call_data_source_tool 取数；需要先发现接口 / 字段 / 实体的源（caixin_api_search、wind_search_fields、天眼查公司搜索），发现类调用不受“一次”限制，继续调到真正的取数 API。结果成功且已经覆盖问题时停止调用
6. 读返回结果，用用户提问时使用的语言回答
```

不要在没读 desc 的情况下硬传 `api_name`，后端会报 `API_NOT_FOUND`。除非本次会话已经读过该源的 desc 并记得参数。

## 3. 调用前的铁律

### 3.1 股票代码必须核对，不能凭记忆猜

A 股 `.SH/.SZ/.BJ`，港股 `.HK`，美股 `.US` 等。用户通常只说中文名。
调任何股票相关 API 前，先用 `moonshot_search` 确认正确代码 + 后缀；没有联网工具时让用户亲口确认，不要硬猜——错代码会静默返回错数据。

### 3.2 企业查询必须用全称

`tianyancha` 拒收"腾讯"这种简称，必须给"深圳市腾讯计算机系统有限公司"这种全名。不知道全名时，先调它的公司搜索 API。

### 3.3 多数 API 需要输出文件路径

绝大部分数据源 API 把完整结果以 CSV 形式写盘，参数名是 `file_path` 或 `filepath`（按该源 desc 里写的来）。漏传会报 `Missing required parameters`。不知道传啥时给一个 `/tmp/<场景>_<时间戳>.csv` 即可。

### 3.4 一次调用不要堆太多 ticker

`stock_finance_data` 实时接口最多 3 个 ticker，历史接口最多 10 个。多了分批调。

## 4. 怎么读返回结果

`call_data_source_tool` 的返回一般含：

1. **`data_preview`**：CSV 头 + 前几行，够答单值问题
2. **CSV 落盘路径**：完整数据写到了输出路径；插件还会把响应文件保存到插件数据目录并在结果末尾列出
3. 结果末尾的 `[kimi-datasource] request-id · tool-call-id` 是后端追踪信息，排障用，回答用户时不用复述

策略：单值问题直接用 `data_preview` 回答；要画图、对比、列清单时基于落盘 CSV 再处理。混合 A+港股查询时服务端会把 CSV 拆成 `_a.csv` / `_hk.csv` 两份。

接口返回失败时提示文字一般写明原因（`PARAMETER_ERROR` / `API_NOT_FOUND` / `EMPTY_DATA` 等），把原因反馈给用户，不要硬走第二次。

## 5. 注意事项

- **回答用户使用其提问语言**。
- **不要凭记忆猜股票代码 / 企业全称**。
- **不要给投资建议**，给完数据加一句"AI 生成，不构成投资建议"。
- 数据源本身只读，不提供任何写入或交易功能。
- 如果某个接口的报错明显是后端 bug（schema 自相矛盾、内部报错等），汇报错误给用户，不要硬试——这类问题只能后端修。
