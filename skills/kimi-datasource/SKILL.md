---
name: kimi-datasource
description: Prefer Kimi datasource tools for finance, macroeconomic, company registry, arXiv, and scholarly paper data instead of generic web search.
disable_tools: false
---

## Purpose

Use this Skill when the user asks for structured financial, macroeconomic, company, academic-paper, or scholar data. Prefer the Kimi datasource LLM tools over generic web search when the answer should come from a database/API rather than a web page summary.

## Available Tools

The plugin exposes three LLM tools:

- `query_stock`: quick stock query for up to 3 tickers. Good for realtime price, realtime technical indicators, open summaries, and close summaries.
- `get_data_source_desc`: read the current API documentation for a datasource.
- `call_data_source_tool`: call one API from a datasource after reading its documentation.

Default workflow:

1. If the user asks a simple stock realtime query for 1-3 tickers, call `query_stock`.
2. For everything else, call `get_data_source_desc` with the relevant datasource name.
3. Choose the exact API and parameters from the returned documentation.
4. Call `call_data_source_tool`.
5. Summarize the result. Mention saved CSV/file paths when the tool returns them.

Do not invent API names or parameters. If an API is not obvious, call `get_data_source_desc` first.

## Datasource Routing

Use `stock_finance_data` for:

- China/HK/US stock finance data, stock screening, industry/concept/location stock discovery.
- Financial statement indicators, business segmentation, related stocks, capital structure, liquidity, efficiency, profitability, growth, and cash coverage.
- Chinese market queries involving A-share/HK/US ticker formats such as `600519.SH`, `0700.HK`, `AAPL.O`.
- Common API families include stock business segmentation, related-stock search, financial indicator category lists, and financial indicator queries.

Use `yahoo_finance` for:

- US/global stock profile data, financial statements, option chains, historical prices, news, corporate actions, holders, option expiration dates, and recommendations.
- Date ranges up to the datasource limits; use the documented `period` or `start_date`/`end_date` options.
- Common APIs include `get_stock_info`, `get_financial_statement`, `get_option_chain`, `get_historical_stock_prices`, `get_yahoo_finance_news`, `get_stock_actions`, `get_holder_info`, `get_option_expiration_dates`, and `get_recommendations`.

Use `world_bank_open_data` for:

- World Bank country indicators, GDP, population, inflation, unemployment, trade, education, health, poverty, emissions, energy, debt, and other WDI data.
- First use `world_bank_search_indicators` when the indicator code is unknown.
- Do not put country names in indicator search queries. Search indicators by concepts, then query data with ISO3 country codes such as `CHN`, `USA`, or `all`.
- Use `world_bank_open_data` after the indicator code is known.

Use `tianyancha` for:

- Chinese company registry and enterprise data: business information, shareholders, legal/judicial risk, intellectual property, annual reports, patents, bidding, and operation data.
- Use `tianyancha_company_search` when the exact company name or unified social credit code is unknown.
- Use `tianyancha_api_search` to discover the correct enterprise API, then `tianyancha_api_call` with the discovered API name and parameters.

Use `arxiv` for:

- arXiv paper search, reading paper metadata/content, listing papers, and downloading/checking paper conversion.
- Keep search queries short, usually no more than 6 keywords. Do not join terms with `OR`.
- Common APIs include `search_papers`, `read_paper`, `list_papers`, and `download_paper`.

Use `scholar` for:

- Scholarly paper search outside arXiv, author filtering, year ranges, sorting by relevance/date, and author profile lookup.
- Keep search queries concise; use author/year filters instead of long natural-language queries.
- Common APIs include `scholar_search` and `scholar_author_info`.

## When Not To Use

Use generic web search only when:

- The user asks for general news, website/page content, product pages, social media, or broad open-web evidence.
- The needed datasource is not listed above.
- The user explicitly asks to search the web rather than query structured datasource APIs.

For finance, company, macroeconomic, and academic database questions, try Kimi datasource first.
