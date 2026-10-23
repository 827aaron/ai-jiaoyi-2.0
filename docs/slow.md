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
- **币安下架公告 → 禁止新开仓**：7 天内的币安下架 / 移除公告提到我们交易的币 → `block_symbol`，作战卡里该币立刻读成“只看不做”（保镖本来就只认卡，所以不用改保镖；封币 = 不再开仓，已有持仓由持仓管家在约 40 秒后平掉并推 Telegram——要下架的币留仓更危险）。宁可多挡：现货交易对移除的公告提到同名币也会挡。进程重启后由新闻源重新补上。

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

- `[meeting]`：每类标的请哪几位分析师（`market/social/news/fundamentals`，至少一位、不重复）、多空辩论和风控讨论轮数（0–3）、flash/pro 模型名、挑币会议和“整理作战指令”两步各用 `quick`（flash，便宜）还是 `deep`（深度档模型，默认与 quick 同为 4.1）、`max_tokens`、重试次数。
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

## 大模型来源：DeepSeek 直连或 OpenRouter

`config/slow.toml` 的 `[llm] provider` 选来源，默认 `deepseek`（直连，`DEEPSEEK_API_KEY`），行为不变。改成 `openrouter` 后走 TradingAgents 自带的 OpenRouter provider（`OPENROUTER_API_KEY`，`jiaoyi setup-keys OPENROUTER_API_KEY` 录入），仍是同一批 DeepSeek 模型：

| provider | quick（flash） | deep（pro） |
|---|---|---|
| deepseek | `deepseek-flash`（V4.1 Flash） | `deepseek-flash`（同上） |
| openrouter | `deepseek/deepseek-v4.1-flash` | `deepseek/deepseek-v4.1-flash` |

Aaron 决定深度档也用 4.1、不用 v4-pro，所以两档默认同一个模型；想换回 pro 在 `[meeting] deep_model` 写 `deepseek-v4-pro`（OpenRouter 为 `deepseek/deepseek-v4-pro`）。OpenRouter 的 id 核对于 2026-10-23（`/api/v1/models`），`[meeting] quick_model / deep_model` 写了就覆盖默认。结构化输出的重试逻辑、成本统计（按模型名里有没有 `pro` 分档）不变；OpenRouter 的实际单价随上游供应商浮动，`[prices]` 仍是占位，M2 真机后按账单核对。没有对应 key 时军师团退回桩（不出卡、不开新仓）。

## 失败退避与日花费上限

- **失败退避**：开会整轮失败（例如 DeepSeek 结构化输出持续出错）后，依次等 5 → 10 → 30 分钟再试，之后保持 30 分钟，成功一次清零（`[budget] fail_backoff_minutes`）。闸门在 `BattleCardCommander.may_refresh`，主循环的所有触发原因（含定时兜底）都要过它，不会每分钟重开最多 20 个币的会。
- **日花费上限**：`[budget] daily_cost_limit_usd`（默认 10 美元，按 `day_reset_tz` 的交易日即曼谷 0 点清零）。超了当天不再开会，沿用现有作战卡（到期后不开新仓），并通过 Telegram 推一条提醒（每天一次）。
- 统计口径：只算成功记录下来的花费（总盘会 + 出了结论的标的）。整轮失败的会议没有可记的 token 用量，所以不进统计，靠上面的退避把这部分兜住。

## 每轮最多出几张作战卡

`[schedule] max_cards_per_round`（1–50，默认不写）限制每轮开会的标的数，也就是一张作战卡里最多有几个标的。不写就用 `app.toml` 的 `card_pick_max`（行为不变）；写了取两者较小值，所以不会超过 `card_pick_max`。顺序：置顶币先占名额，剩下的名额由总盘会按选币评分和行情挑，超出名额的不开会。想省钱先降这个数，开会次数和花费大致成正比。当前上限会显示在军师团的状态要点里（面板“团队”页）。

### 日花费统计的口径（补）

日花费上限的统计包含失败的花费：会议失败时按引擎带出的真实 token 用量计（`MeetingError.cost_usd`）；超时、断网等拿不到用量时，按 `[budget] failed_meeting_cost_usd`（默认 0.05 美元）估算计。总盘会（选币）一出结果就计入，哪怕之后所有标的都失败；会议给出的结论被代码判为不合格（别的标的、越界）时，它的花费也照计。当天已花的钱落在 `data/slow_budget.json`（按曼谷交易日），重启后读回，跨天自动清零；文件读写失败只影响重启恢复，不影响出卡。
## 止损 ATR 的周期（跟风控配置走）

作战卡里的 `stop_atr` 是 **ATR(14) 的倍数，周期由 `risk.toml` 的 `atr_period_min` 决定**（5/15/30/60 分钟，默认 15；Wilder 平滑，与快环特征层、保镖同一算法）。整理作战指令的提示词里不写死周期：它按配置写明周期，附上该币当前这个周期的 ATR 数值和占价格的百分比（取不到时明说“暂不可用”），并告诉军师止损倍数必须落在 `fast_loop.min_stop_atr`–`max_stop_atr` 之间。会议分析师看到的指标仍是 1h K 线，那是看趋势用的，不是止损尺度。

慢环这边也把 `stop_atr` 夹进 `[min_stop_atr, max_stop_atr]`、`take_profit_r` 夹进 `[min_tp_r, max_tp_r]`（`clamp_to_risk`，都来自 `fast_loop`），提示词里同样写明这两个区间。军师给出 0.2–0.4 倍这类过近的止损、或低于下限的止盈倍数，会被抬到下限，而不是让保镖把整张卡的这个标的拒掉白开会。

## 安装与启动检查

`make install` 默认连军师团依赖（`.[dev,slow]`，TradingAgents 钉提交）一起装。配了军师团的 key（`DEEPSEEK_API_KEY` 或 `OPENROUTER_API_KEY`，看 `[llm] provider`）却没装 `tradingagents` 时，`jiaoyi run / status` 和 `jiaoyi check-config` 直接报错退出，**不再静默用桩**——否则 M2 会空跑（不出卡、不开新仓却看不出来）。没有 key 时仍用桩，M0/M1 照常。

## M2 实跑后的三处小修

- **欧易公告**：原来的 `announcements-trading-rules` 类型已下线（返回 400），换成 `new-listings / delistings / api`；某一类型失败不影响其余，只有全部失败才算该来源失败。同一来源的报错记录最多每 30 分钟一条。
- **Reddit 限流**：匿名 RSS 约每分钟一次，多标的并发开会必然 429。`[social] reddit = "guarded"`（默认）给抓取加闸：同币 30 分钟缓存、两次真实请求至少隔 65 秒（其间不等待，返回“不可用”占位）、失败后冷却 10 分钟；`"off"` 则完全不抓。占位文字写明“不可用不等于没人讨论”，Reddit 只是情绪的背景数据之一。
- **结构化输出回退**：DeepSeek 偶尔让 Research Manager / Trader 的结构化输出失败（约每轮一次），TradingAgents 会改用自由文本再调一次。这一次调用同样挂在带计费回调的 LLM 客户端上，所以费用照常计入当日预算；回退得到的是自由文本，由后面的作战卡转换步骤统一解析，解析不了则该标的本轮不出卡。

## 指标补全与作战卡即时落盘

- **技术指标**：币安数据源（小时线）现在支持 TradingAgents 技术面分析师会要的全部指标：`close_N_sma / close_N_ema / rsi / atr / macd / macds / macdh / boll / boll_ub / boll_lb / vwma / mfi`（布林 20 根 ±2σ，VWMA 20 根，MFI 14 根）。分析师若点了不认识的名字，返回“不支持，可用：…”的文字让它换一个，不再让整场会议失败。
- **每场会议结束立即出卡**：一个标的开完会就把它并进当前卡、版本号 +1、写到 `data/battle_card.json`（连同各币上次开会时间）。重启后没过期的卡直接捡回来，也不会为刚开过会的币重复开。还没轮到的币在本轮结束前不在卡上（保守：没有指令就不开新仓）。

### 开会期间的卡与持仓币（审计 I8）

- 一轮里还没轮到（或开会失败）的币，**沿用上一张卡的指令**，开完哪个换哪个；上一张卡已过期就不沿用旧指令。没有新结论的失败币在本轮结束时从卡上拿掉。
- 有持仓的币必须一直在卡上：本轮没入选或开会失败的，清空信号、方向不动（改成 NO_TRADE 会被持仓管家当平仓理由）（持仓管家只会继续盯，不会因“不再盯”平仓）。循环在每次开会前用 `set_held` 告诉军师团当前持仓。
- 重启恢复的卡：按当前 `risk.toml` / 止损档位再夹一遍；出卡时的 ATR 周期和止损倍数区间与现在不一致就不恢复，等下一轮会。

- **开会期间的心跳**：一轮会议要十几分钟，期间每开完一场、以及每 30 秒都上报一次心跳，详情写“开会中：3/8”，面板不会把慢环标红。
- **封币与持仓**：封币 = 不再开仓，已有持仓由持仓管家约 40 秒后平掉并推 Telegram（要下架的币留仓更危险）。
- **持仓币强制入选（R5b）**：有持仓的币（候选池内的）每轮强制开会，和置顶一样不占军师的挑选名额。没有新结论时沿用旧指令（清空信号）；旧卡里也没有它（切档重启丢了旧卡、卡文件没了、币已不在池里）就按持仓方向合成一条“只盯不开”，所以持仓币永远不会从卡上消失。
## 关键位由代码算，军师只能选（M2 现场反馈）

第一张卡里 BTC 的关键位（现价 ~81600 报 86000 / 72859.5）是模型凭印象编的：不分支撑压力、没排序、离现价很远。现在：

- **候选由代码算**（`slow/levels.py`）：昨日 / 7 日高低点、近 7 天 1h 的摆动高低点、成交密集区、整数关口；只留现价 ±4×ATR（取“止损周期 ATR”和“1h ATR”里大的）以内的，相近的合并，按距现价从近到远。
- **提示词里列出候选**（支撑 / 压力两组，各最多 5 个），要求 `key_levels` 只能从候选里选。
- **落到卡上前再对回候选**：军师给的价位在 0.5×ATR 内就换成最近的候选（类型以现价上下为准，不信模型报的类型），对不上的丢掉；每边最多 3 个，先支撑后压力、各自从近到远。行情拿不到就不出关键位，不瞎编。
- **永续合约语境**：TradingAgents 的提示词是写死的股票语境（Buy / Overweight / Underweight、“减持”）。它会把 `output_language` 原样拼进每个角色的提示词，所以我们在这个字符串里加了永续合约的说明（多 / 空 / 观望，Underweight = 偏空或观望，不要“减持”），并在转换作战指令的提示词里明确 Buy/Overweight→偏多、Sell→偏空、Underweight→偏空或观望、Hold→观望。评级行本身仍是英文（TradingAgents 程序读它）。

- **落盘与心跳**：作战卡每场会后写 `<data_dir>/battle_card.json`（启动日志会打印它的绝对路径；写失败会记 ERROR 日志，不再静默）。军师团的心跳由应用每 30 秒统一上报：空闲时写“当前卡 vN，M 个可做，下次会议 HH:MM”，重启恢复后写“已恢复卡 vN…”，失败退避中写重试时间，会议中写“开会中：k/N”。

- **会议花费预记（X2）**：每场会议（含总盘会）开始前先按估算（最近几场实际花费的均值，没有就用 `failed_meeting_cost_usd`）记进当日预算并立即落盘，结束后改成实际值。被强退（重启）打断的会议不会漏记，每次重启最多多记一笔估算。


## 选币页的固定 / 拉黑 / 手动加入

存在 `data/pool_overrides.json`（`core/pool_overrides.py`），AI 改不了。面板发指令（`pin_symbol`、`unpin_symbol`、`blacklist_symbol`、`unblacklist_symbol`、`add_symbol`、`remove_symbol`，参数 `symbol`），主循环执行：改文件 → 立即重新选币。拉黑的币立即不开新仓（不等军师团下一轮出卡），也不再进池；固定和手动加入的币仍要过硬门槛，不占类别名额。手改文件也行，下一次选币（每小时）读到；文件读不出时运行中沿用上一份，启动时发 CRITICAL 告警并暂停开新仓（拉黑清单没生效，已有持仓照常看管），修好或删掉后重启自动恢复。

选币其余参数：池子大小 `app.toml` 的 `pool_size`、每轮挑几个 `card_pick_max`（面板设置页可改）；硬门槛（成交额 2000 万 U、深度 10 万 U、上线 7 天）、打分权重、每类上限 30、每小时选一次目前是代码默认（`slow/screener.py` 的 `ScreenerConfig`），点差门槛跟 `risk.toml` 的 `max_spread_bps` 走。

### 关键位范围按持仓时长统一缩放（Aaron：BTC / ETH 根本波动不了这么大）

关键位只给“持仓期内碰得到”的：范围 = `levels.span_sigma`（默认 1.5）× **该币自己的 ATR**（档位对应的周期：5 / 15 / 30 / 60 分钟）× √(持仓时长 / ATR 周期)。ATR 乘根号时间就是持仓期内价格的典型波动，所以**同一条公式适用所有币、所有档位**——波动大的币范围自动宽、BTC / ETH 自动窄，没有按币种的特例，也没有百分比表。例：ETH 现价 2390、15m ATR ≈ 6，2h 档范围 ≈ ±25（约 1%），不再出现 −9.8% 的支撑。摆动点和成交密集区也改用档位周期的 K 线（不再用 1h）。

提示词里同时告诉军师团持仓时长和该币持仓期内的一个标准差波动（TradingAgents 各角色的提示词里也带上“每笔最多持有约 N 分钟，目标价 / 止损 / 关键位按这个时长内该币的波动来给”），止损 / 止盈建议跟着收紧。持仓时长取自档位的时间止损（`fast_loop.time_limit_s`）。
