# 慢环：选币与军师团

代码在 `src/jiaoyi/slow/`。慢环只写作战卡，不碰下单（`jiaoyi.execution` 不许导入）。

## 开会频率和成本

TradingAgents 每个标的一次会议大约 16–22 次 LLM 调用，DeepSeek 思考模型还会偶尔把结构化输出
退回成自由文本（上游 issue #750 / #1390 / #82 / #1461）。所以默认值是：

| 项 | 默认 | 说明 |
|---|---|---|
| 同一个币两次会议的最小间隔 | 2 小时（`symbol_min_interval`，建议 2–4 小时） | 没有触发就沿用上一张卡里这个币的指令，不花钱 |
| 触发重开会 | 卡到期前、关键位被破、大新闻、新入池 | 关键位突破和大新闻会无视最小间隔，只给该币重开 |
| 卡片有效期 | 2 小时（1–4 小时） | |
| 并发 | 4（`concurrency`） | 对付 DeepSeek 限流 |
| 整体防抖 | 5 分钟内不连续开会 | |

## 会议输出不合格就是“这个币没有结论”

- 结构化输出解析失败（含退回成自由文本）→ 重试一次，仍失败则该币没有结论。
- 名义价值上限或杠杆上限超过 `config/risk.toml` 的硬上限 → 该币没有结论（不是悄悄夹紧后照用）。
- 分组与候选池不一致、返回了别的标的 → 该币没有结论。
- 所有币都没有结论 → 抛 `CardRefreshError`，不出新卡；旧卡到期后快环不再开新仓。
- 没有结论的币不在卡里，快环对不在卡里的币不开仓。

`stop_atr` / `take_profit_r` 在硬上限里没有对应项，这里不夹，保镖签发时必须自己再核一遍。

## 数据源：哪些已经换成币安，哪些还没有

| 数据 | 现状 |
|---|---|
| K 线、技术指标（TradingAgents 的 `get_stock_data` / `get_indicators`） | ✅ 已换成币安 U 本位永续小时线（`binance` 数据源，见 `tradingagents_engine.py`），指标在本地计算 |
| 选币用的成交额、点差、深度、资金费率、持仓量变化 | ✅ 币安公开接口（`binance_stats.py`），喂给选币和总盘会 |
| 资金费率、持仓量**喂给 TradingAgents 的分析师** | ✅ 附在 `get_stock_data` 的输出后面（最近 6 次资金费率 + 24 小时持仓量变化），拿不到只写“暂不可用”，K 线照常 |
| 新闻 / 公告 | ✅ 已换成 `slow/news.py` 的 `NewsHub`（见下节）；美股永续仍走 TradingAgents 自带的 yfinance 新闻 |
| 美股永续的财报、新闻 | 保留 TradingAgents 自带工具（SEC EDGAR / yfinance） |

后续（M2 真机验证时一起做）：把资金费率和持仓量作为工具注册进 `VENDOR_METHODS`，并评估加密新闻源。

## 新闻与公告源（`slow/news.py`）

| 源 | 角色 | 说明 |
|---|---|---|
| 币安公告 | 主源 | 公开接口（非官方，改版会坏→解析失败直接报错，不猜），中文；上新 48、下架 161、动态 49、API 51 |
| 欧易公告 | 辅源 | 官方公开接口；注意出口 IP 不同看到的站点（全球 / 美国）不同，上线时在曼谷复查 |
| CoinDesk / Cointelegraph / The Block RSS | 背景 | 行业新闻，只喂给会议，**不触发开会**；用 defusedxml 解析 |

- 可插拔：实现 `NewsSource.fetch()` 即可加源。每个源独立失败；状态变化（挂 / 恢复）才写一条决策日志，全挂时会议照开，只是没有新闻。
- 同步读缓存：`NewsHub.for_symbol / market` 只读内存，TradingAgents 的工作线程直接调用，不碰网络。主循环定时调 `poll_and_notify`（建议公告 1–5 分钟、RSS 15 分钟；目前统一一个周期，按需拆）。
- 喂给会议：TradingAgents 的 `get_news` / `get_global_news` 路由到 `crypto_news`（加密币读 NewsHub，美股永续回退 yfinance）；总盘会的提示里带市场摘要和每个候选币的最近标题。
- 触发开会：公告类新条目提到候选币 → `BattleCardCommander.notify_news`。
- **币安下架公告 → 禁止新开仓**：7 天内的币安下架 / 移除公告提到我们交易的币 → `block_symbol`，作战卡里该币立刻读成“只看不做”（保镖本来就只认卡，所以不用改保镖；已有持仓不受影响，由持仓管家和人处理）。宁可多挡：现货交易对移除的公告提到同名币也会挡。进程重启后由新闻源重新补上。

接线（主循环，架构师）：创建 `NewsHub(default_sources(), log)`，传给 `TradingAgentsEngine(news=hub)`，并定时 `await hub.poll_and_notify(pool_symbols, commander.notify_news, commander.block_symbol)`。

## 误封后怎么解封

下架公告匹配是宽松的（宁可多挡，例如现货交易对移除公告提到同名币）。目前 `BattleCardCommander.unblock_symbol(symbol)` 只有代码入口；进程重启后会由新闻源按 7 天窗口重新补封。要给人一个入口，需要架构师在 `app.py` / 面板的“设置”页暴露 `blocked_symbols` 和 `unblock_symbol`（同时显示被封的币和原因）。在那之前，误封的处理办法是等公告超出 7 天，或在代码里调用 `unblock_symbol`。

## 强平数据

慢环**不另开强平连接**。快环行情层连一路全市场强平流（`!forceOrder@arr`）并滚动统计 1 小时名义额，慢环只依赖窄接口 `slow.liquidations.LiquidationSource.liquidations_1h_usdt(symbol, now)`，没有新契约。

- 选币：`BinanceStatsSource(liquidations=...)` 把它填进 `SymbolStats.liquidations_1h_usdt`，参与打分（占 10% 权重，约 1000 万 USDT 封顶）。
- 会议：`TradingAgentsEngine(liquidations=...)` 把近 1 小时强平写进 `get_stock_data` 的衍生品段和总盘会的候选表。
- 接线（架构师）：把快环的统计对象同时传给 `BinanceStatsSource` 和 `TradingAgentsEngine`；没传则强平为 0 / 不显示，不影响其它功能。
- 多空拆分（多头被强平 vs 空头被强平）等快环 PR 合并后再加，需要对齐它的 `stats()` 返回结构。

## 共享市场简报（`slow/brief.py`）

每轮开会前由 `BattleCardCommander` 调一次 `MarketBriefService.refresh()`，所有标的的会议共用，不再每个会议各查一遍大盘。内容：BTC / ETH 趋势（日线、4 小时线）、恐慌贪婪指数（alternative.me，免费无 key）。每部分独立失败，全失败则沿用上一份，简报挂了会议照开（记一条错误日志）。

- 进入会议：总盘会提示、单标的结论整理提示、都带这份简报。
- **美元指数（DXY）暂未接**：没有稳定的免费无 key 来源（试过的站点会拦截脚本请求）。要加就实现一个 `BriefPart` 加进 `default_parts()`。
- 接线（架构师）：创建一个 `MarketBriefService()`，同时传给 `BattleCardCommander(brief=...)` 和 `TradingAgentsEngine(brief=...)`，退出时 `aclose()`。

## 成本与节奏配置（`slow/settings.py`，可选 `config/slow.toml`）

开会花多少钱、多频繁，都能在 `config/slow.toml` 里调（示例见 `config/slow.example.toml`）：

- `[meeting]`：每类标的请哪几位分析师（`market/social/news/fundamentals`，至少一位、不重复）、多空辩论和风控讨论轮数（0–3）、flash/pro 模型名、挑币会议和“整理作战指令”两步各用 `quick`（flash，便宜）还是 `deep`（pro）、`max_tokens`、重试次数。
- `[schedule]`：并发（1–8）、作战卡有效期（1–4 小时）、同一个币两次会议最小间隔（0.5–12 小时）。
- `[prices]`：每百万 token 美元价，只用于花费统计（占位值，M2 真机按官网核对）。

没有这个文件就用默认值，**与不配置时行为完全一致**。写错字段或超范围直接拒绝启动（`ConfigError`）。这里**不放任何风控数值**，硬上限仍只在 `risk.toml`。

接线（架构师）：`cfg = load_slow_config()`，`TradingAgentsEngine(settings=cfg.engine_settings(), ...)`，`BattleCardCommander(..., **cfg.commander_kwargs())`。

## 作战卡复盘（`slow/review.py`）

出卡时 `BattleCardCommander(journal=CardJournal(data_dir))` 把**这次真开了会**的每个标的追加到 `data/card_review/cards.jsonl`（沿用旧卡的不重复记）。卡过期后：

```
python -m jiaoyi.slow.review --days 7        # 按方向 / 类别 / 出卡原因汇总
python -m jiaoyi.slow.review --json          # 逐条明细
```

用公开 5 分钟 K 线，在卡的有效期内算收盘涨跌、最大有利 / 不利波动；long/short 才算命中（方向收益 > 0），both / no_trade 只看波动。**只读报告，不会自动改任何权重、阈值或提示词。**样本少（几十条以内）时别当结论。接线（架构师）：创建 commander 时传 `journal=CardJournal(data_dir)`。
