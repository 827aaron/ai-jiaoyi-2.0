# 快环说明

快环 = 行情 → 特征 → 哨兵 → 拍板人 → 风控员 → （执行层）开仓；盯仓 = 持仓管家 + 巡逻兵。
负责人：快环工程师。设计依据：设计文档 v1 §二、§三、§四、§五、§七。

## 模块和实现

| 接口 | 实现 | 文件 |
|---|---|---|
| `MarketDataFeed` | `BinanceFuturesFeed` | `market/binance_ws.py` |
| `FeatureEngine` | `RollingFeatureEngine` | `market/features.py`（盘口员公式在 `market/book.py`） |
| `Trigger` | `RuleTrigger` | `sentinel/rules.py` |
| `DecisionModel` | `JevDecisionModel`（Jev）/ `RuleDecisionModel`（规则版） | `gate/jev.py`、`gate/rule_model.py` |
| `EntryGate` | `JevEntryGate` | `gate/entry.py`（问题措辞和快照在 `gate/questions.py`） |
| `RiskGuard` | `RiskGuardImpl` | `risk/guard.py` |
| `Keeper` | `JevKeeper` | `keeper/keeper.py` |
| `Patrol` | `RulePatrol` | `patrol/rules.py` |

每个角色用 `core/recorder.py` 的 `RoleRecorder` 写决策日志、累计今日统计（按 `day_reset_tz` 清零）、发心跳。

## 接线要点（给 `jiaoyi.app`）

已接好：组装在 `src/jiaoyi/app.py`，主循环在 `src/jiaoyi/loop.py`（`jiaoyi run` 启动）。下面是当时的约定，改接口时同步这里。

- 启动时对每个标的调 `market.rest.fetch_recent_klines` 回填 1 分钟 K 线（`backfill_minutes(atr_period_min)` 根，15 分钟 ATR 是 240 根），再接 WebSocket，否则 ATR 要干等几个小时。
- 每个行情事件：`features.update(ev)` → `patrol.check(ev, positions)`（有动作就调执行器平仓）→
  成交事件后 `trigger.check(features.snapshot(sym), card, now)`。
- 每个候选信号：`gate.evaluate` → `guard.review` → 放行才把 `verdict.intent` 交给执行器。
- 管家每 `keeper_interval_s` 秒复查一次每个持仓；`patrol.urgent` 里的持仓立刻复查，复查后 `discard`。`keeper_price_max_age_s`（默认 0 = 不查）> 0 时，这个币的特征快照时间（成交 / 盘口 / 标记价任一最后更新，和开仓检查 `signal_max_age_s` 同一个时钟）比现在旧超过它，这一轮记「行情已旧 N 秒，这轮跳过、维持现状」：不平仓、不问模型、回吐保护也不按旧价触发，反向信号留到下一轮；同一段断流只记第一条。断流不超过 2 个 `keeper_interval_s` 时连续「该平」计数不变，更久就清零（断流前的一票不和恢复后的一票凑成连续两次）。反向作战卡在 `confirm_or_tighten` 下遇到过旧行情按「没有行情」处理：不平、只要求收紧，也不拿旧价当确认起点（记 `price_stale_s`）；原规则（直接平）不看价格，照旧。冻结盈利价回访在它前面，不受它管。
- 管家每轮复查的规则顺序（`keeper.RULE_ORDER`）：作战卡（硬平 / 收紧）→ 被封的币 → 冻结盈利价 → 价格新鲜度和来源 → 回吐保护 → Jev 盈利门控 → 平仓概率。前三条不读管家的价（卡规则自己按行情核确认，冻结盈利价读执行所自己的证据），价格过旧也照常判；后面的都先过价格检查。第一条投 close 的生效，决策记录 `output.rules` 写每条的票（没走到的写 `not_reached`），`output.rule_fired` 写生效的那条，`output.price` 写两种价。
  - `keeper_model_mode`（默认 `act` = 现状）：`record` 时每次照常问 Jev、照常记，但不按平仓概率平（含反向信号急查的「过线即平」，票写 `record` 和 `would_close`），被封的币改成硬规则直接平（不再搭平仓概率的车）；Jev 的回答照样喂给盈利门控。
  - `keeper_price_source`（默认 `mainnet` = 行情快照的主网最新成交价）：`venue` 时用执行所标记价（`HbExecutorAdapter.venue_mark`，和保护状态机同一个 `execution_mark` 读法，新鲜度上限放宽到 600 秒交给管家自己判，同一个币 2 秒内复用、并发复查共用一次读）。浮盈、最高浮盈、回吐保护、盈利门控和给 Jev 的快照价都用它，`keeper_price_max_age_s` 按它的交易所时间查（0 时按 5 秒）。读不到或过旧：这一轮回吐保护、盈利门控、平仓概率都不判（票写 `hold` / `venue_unavailable`），作战卡、被封的币、冻结盈利价照常；**不退回主网价**。最高浮盈按价格来源分开记（重启后按决策记录 `output.price.used` 找回，旧记录算主网），venue 时主网价新鲜也一起记主网的最高浮盈，中途切换价格来源不会拿一种价的峰和另一种价比。执行所标记价连续 `keeper_venue_alert_reviews`（默认 3，0 = 不告警）次复查读不到或过旧，发一次告警（类别 system；同一段只告一次），决策记录 `output.price.venue_misses` / `venue_alert` 和理由里写明，仍不退回主网价。实盘、离线桩没有单独的执行所价（`note = venue_is_mainnet`，照主网判）。冻结盈利价本来就读执行所盘口，不受这项影响。
  - Jev 盈利门控（`keeper_thesis_take_on`，默认关）：浮盈为正且至少 `keeper_thesis_take_min_r` R（R = 账本开仓价到止损价；浮盈未扣手续费，和回吐保护同口径），最近一次 Jev 答 `thesis_intact = no` 的概率至少 `keeper_thesis_take_p`，那次回答（按那次复查的时刻）不超过 `keeper_thesis_take_max_age_s` 秒，三条同时成立就全平，理由开头「盈利且进场理由不成立，止盈」，平仓原因 keeper。这一轮 Jev 出错时用上一次的回答（够新才算）。
  - 平仓前检查 `keeper_close_guard`（默认关 = 现状）：开着时所有平仓都从 `_verdict` 出去，拦下的改成拿住（理由开头「不发平仓：」，`output.close_blocked`），作战卡通道对拦下的结论也不收紧（收紧本身会换代或平仓）。拦的情况：交易所保护腿已触发或正在退出（`protection_exit_in_flight` 返回 `exiting`），交给保护轮询收尾，最多等 `keeper_close_guard_exiting_max_s`（默认 120 秒）；托管过的仓正在换代（有腿在提交或撤单中），最多等 `keeper_close_guard_replacing_max_s`（默认 30 秒）；连续在途（接连换代、换代接退出）合计最多等 `keeper_close_guard_total_max_s`（默认 180 秒）。等过头照常平，`output.close_guard_note` 记下。等待时间每次复查都刷新：不在途就清零；退出只按种类起算（触发 → 退出 → 收尾算同一次），换代按保护代数起算（同一代里腿一步步走不重算，新一代重算，但合计上限不重算）。同一笔仓半个 `keeper_interval_s` 内已发过一次平仓请求也不再发。
- 持仓平掉后调 `keeper.forget(position_id)`。
- 风控员构造时传 `state_dir=AppConfig.data_dir`，日亏熔断状态落盘在 `data/risk_state.json`，重启后当天仍锁住。
- 风控员需要 `LotRulesProvider`（币安 exchangeInfo 的 stepSize / minQty / minNotional），由执行层提供；拿不到就拒单。
- 杠杆按币安分层封顶（`[fast_loop] leverage_clamp_to_bracket`，默认关 = 原行为，热生效，面板「风控员 → 仓位太大时缩单」可改）：风控员原来的杠杆是 `max_leverage`（或作战卡约束时和卡上限取小），超过币安这个币的上限时币安拒绝设杠杆（XPLUSDT 10-26/27 六次 `-4028 Leverage 15 is not valid`），这笔开不了。打开后执行层的 `ExchangeInfoLotRules` 跟着下单精度一起拉执行所（Demo 就是 Demo）的 `/fapi/v1/leverageBracket`（签名只读 GET）：启动时一次，之后每天一次，失败 10 分钟后再试、沿用旧分层；从没拉到就按原杠杆（和关着一样）。风控员在不超过原杠杆的各档杠杆里挑能开最大仓位的那个：每个候选按 min(按风险算的名义, 该杠杆下的保证金上限, 该杠杆的分层名义上限) 打分，一样大取杠杆高的（例如分层 5k 10x / 25k 5x、按风险能开 20k：10x 只能开 5k，选 5x）；作战卡约束时不选低于卡杠杆下限的档。保证金上限按选中的杠杆算，名义价值再加一条上限「币安杠杆分层名义上限」（选中杠杆还允许的最高一档的名义上限 × 0.98：币安按标记价或空单回踩挂单价核，可能高于这里用的最新成交价，留 2% 免得 -2027）；每笔风险金额、单笔名义、总敞口和相关组敞口上限都不变，只会让名义更小。作战卡约束时降后的杠杆低于卡的杠杆下限就拒。决策记录 `detail`：`bracket_max_leverage`、`leverage_clamped`（降了才有）、没拉到分层写 `leverage_bracket`。开关在运行中打开，分层在下一次刷新下单精度（选币时）才拉。
- 缩单下限（`[fast_loop] min_size_ratio`，默认 0 = 关 = 原行为，热生效，面板「风控员 → 仓位太大时缩单」可改，也能写进 `config/app.pending.toml`）：风控员按风险金额算出仓位后，各项上限只会把它缩小，原来压到多小都照开。2026-10-28 07:15 ZECUSDT 空（btc_lead，M 档）计划风险约 231U，其他 5 笔仓占满了保证金，按「可用保证金」压成 0.067 张 ≈ 87U、风险 0.68U，照样付手续费、占一个持仓名额和一个 btc_lead 名额。打开后取整后的风险（数量 × 止损距离）低于 计划风险 × 这个比例就拒，理由「仓位被<封顶项>压到计划的 X%（风险 Y U），低于下限 Z%，不开」。计划风险 = 空仓时能开的大小：min(按风险金额算的名义, 单笔名义上限, 相关组倍数 × 本金（配置保证它不超过总敞口倍数，所以总敞口倍数不用单列）, 币安杠杆分层名义上限, 作战卡约束时的卡名义上限, 交易所单笔最大数量 × 价) ÷ 价 × 止损距离；所以只有被已有持仓占掉的可用保证金、总敞口 / 相关组余量，以及盘口深度能触发它，固定上限封顶的单比例约 1。决策记录 `detail` 写 `planned_risk_usdt` 和 `size_ratio`（开着才有）。正好等于下限放行（严格小于才拒）。多空一样算。人工放行（面板「风控员拒绝 → 放行」）把它当软规则跳过、写进 `waived`。挂单回踩没成交改市价追进时不重新算仓位（追的是批准时的数量），批准时过了下限就一直过，追单复核不用再查。被交易所单笔最大数量截断时 `binding` 写「交易所单笔最大数量」（原来会留着前面的封顶项，例如 STRK 被写成「风险金额」）。**Demo 建议保持 0**：按 10-23 到 10-28 的 220 笔放行粗算（计划风险按同日按风险金额开满的单反推本金），0.25 会多拒约 35 笔（盘口深度约 30、可用保证金 2、总敞口 2、相关组 1），而这批单合计 +72U / +0.12R，是赚钱的；0.1 约 16 笔，0.5 约 64 笔。
- 守作战卡名义下限（`[fast_loop] respect_card_notional_min`，默认关 = 原行为，热生效，同一组可改）：作战卡上的名义价值范围在 `card_caps_binding` 关着（默认）时只当军师建议记进 `detail`（`card_notional_min` / `card_notional_max`，#162 有意改成这样：按风险算的仓位常常小于卡的下限，10-23 到 10-28 的放行里约三成低于卡下限），开着时上下限都是硬约束。这一项只把下限单独拿出来：取整后的名义价值低于卡上的下限就拒，理由「按<封顶项>只能开 X U，低于作战卡名义下限 Y U」；和其他作战卡规则一样，人工放行也拦；上限、杠杆照旧只当建议。`card_caps_binding` 开着时它不起作用（卡下限本来就守，理由是「低于作战卡下限」那条）。Demo 建议先关。
- 回滚（这两项）：面板改回 0 / 关，或删掉 `app.toml` 里这两行，几秒内回到原行为、不用重启；回退代码前先删掉这两行（旧代码不认识它们，`app.toml` 会读不进）。不落新字段到账本、决策记录只多 `detail` 里的两个键，旧代码照读。
- 开仓前读账户（`handle_signal` 拍板通过、拿开仓锁后的 `executor.account()`）失败：记一条风控员拒绝「读账户失败，本信号放弃：…」（`output.detail.by = "account_read"`，带错误），不再让任务带着异常消失；`app.toml account_read_retries`（默认 0 = 不重读，最多 3，热生效）> 0 时隔 1 秒再读这么多次：只对很快就失败的（连不上、5xx）有用，hummingbot 请求超时 5 秒，一次超时基本就把 `signal_max_age_s` 用完了；每次重读前看信号剩下的有效期，不到 1 秒就直接放弃（记拒绝）；重读成功后再查暂停 / 重启和信号是否已过期，任一成立都记拒绝不开仓。主循环 `_spawn` 起的后台任务出异常一律记日志（带调用栈）；开仓 / 追单任务记风控员、管家复查记管家、巡逻兵平仓记巡逻兵、对账记对账员、面板平仓记执行的「出错」（团队页今天的出错数）。
- 开仓拍板和持仓管家默认共用同一个 Jev 实例，所以共用一份连续出错计数和冷却（`jev_max_consecutive_errors` 次连错暂停 `jev_error_cooldown_s` 秒）：管家每轮问每笔持仓，它连错会让开仓拍板也进冷却。`app.toml jev_shared_cooldown = false`（改后重启）时管家拿 `JevDecisionModel.with_own_errors()`：同一条通道、同一份配置和超时，计数和冷却各算各的；这时拍板人的灯只反映开仓拍板，管家自己的 Jev 连错 / 冷却并进管家的灯（变黄 / 变红，标题写「管家的 Jev 连续出错 N 次」或「冷却中（还剩 N 秒）」）。默认 `true` = 原行为。
- 拍板模型用 `gate.jev.decision_model_from_config(...)` 选：有所选通道的 key（`JEV_API_KEY` 或 `OPENROUTER_API_KEY`，见下文“Jev 通道”）且 `[fast_loop] jev_model` 锁了具体版本 → Jev；
  否则只有 paper 模式且 `rule_gate_without_jev = true` 才用规则版（testnet / live 构造规则版直接报错），默认返回 None（用桩，不开仓）。
  这是启动时的一次性选择，Jev 运行中出错只返回 `ok=False`，不会切到规则版。

## `FeatureSnapshot.values` 的键

“现在”是该标的最后一个事件的时间。没有足够数据的键不出现（下游用 `.get()` 给保守默认值）。

| 键 | 含义 | 单位 |
|---|---|---|
| `ret_1m`、`ret_5m` | 最新价相对 1 / 5 分钟前的收益 | 小数，0.01 = 1% |
| `vol_1m_usdt` | 最近 60 秒成交额 | USDT |
| `vol_ratio_1m` | `vol_1m_usdt` ÷ 最近 20 根 1 分钟 K 线平均成交额（至少 5 根才有） | 倍 |
| `trades_1m` | 最近 60 秒成交笔数（归集后） | 笔 |
| `taker_buy_ratio_1m` | 最近 60 秒主动买成交额占比 | 0–1 |
| `cvd_1m_usdt`、`cvd_30s_usdt` | 主动买减主动卖，60 / 30 秒 | USDT |
| `realized_vol_5m` | 5 分钟内每 10 秒对数收益的标准差（不年化） | 小数 |
| `liq_long_usdt_1m`、`liq_short_usdt_1m` | 最近 60 秒多头 / 空头被强平额 | USDT |
| `liq_long_usdt_5m`、`liq_short_usdt_5m` | 最近 5 分钟多头 / 空头被强平额 | USDT |
| `liq_ratio_1m` | 1 分钟较大一边的强平额 ÷ 30 分钟每分钟均值 | 倍 |
| `funding_rate` | 当期资金费率 | 小数，0.0001 = 0.01% |
| `funding_in_min` | 离下次资金费结算还有几分钟 | 分钟 |
| `mark_premium_bps` | 标记价相对指数价 | bp |
| `vol_ratio_10s` | 最近 10 秒成交额 × 6 ÷ 最近 20 根 1 分钟 K 线平均成交额（放量提前发现；走这条要 ≥ `burst_10s_ratio`（默认 5），同时 `vol_ratio_1m` ≥ `burst_min_1m_ratio`（默认 2）） | 倍 |
| `taker_buy_ratio_10s` | 最近 10 秒主动买成交额占比（10 秒内有成交才有） | 0–1 |
| `ret_15m`、`ret_60m`、`ret_240m` | 最新价相对 15 / 60 / 240 分钟前那根 1 分钟线收盘价（那根缺了差 5 分钟以上就没有） | 小数 |
| `ret_oiw` | 同上，窗口 = `oi_window_min`（持仓量变化信号用） | 小数 |
| `vol_1m_24h` | 过去 24 小时 1 分钟对数收益的标准差（至少 60 个才有） | 小数 |
| `btc5z` | BTC 5 分钟涨跌 ÷（BTC 的 `vol_1m_24h` × √5），每个币都带 | 倍 |
| `oi15`、`oi60`、`oi4h`、`oi_w_pct` | 持仓量 15 / 60 / 240 / `oi_window_min` 分钟变化（`market.derivs`，REST 每 60 秒） | % |
| `top_ls`、`crowd_ls` | 大户持仓多空比、全市场人数多空比（最新，REST 每 5 分钟） | 比值 |
| `top_w_pct`、`crowd_w_pct` | 上面两个在 `top_crowd_window_min` 内的对数变化 | % |
| `div4h` | 大户比 4 小时对数变化 − 散户比 4 小时对数变化（正 = 大户比散户更看多） | % |

`FeatureSnapshot.atr`：`atr_period_min` 分钟 K 线 14 根 Wilder ATR（默认 15 分钟，由 1 分钟 K 线按 UTC 整点聚合，缺分钟的那根整根丢掉），周期写在 `FeatureSnapshot.atr_period_min`。`book`：盘口超过 10 秒没更新就为空（风控员看到空盘口会拒单）。
`BookMetrics.ofi` 和 `cvd` 都是 60 秒窗口、按 USDT 计。

## 已知限制

- 币安 U 本位 WS 按类型分路由：盘口类（depth / bookTicker）走 `wss://fstream.binance.com/public/stream`，成交 / 标记价格 / K 线 / 强平走 `/market/stream`；旧的 `/stream` 只剩盘口在推（2026-10-23 Mac mini 实测）。行情层每 40 个币各开一条 market、一条 public 连接；按币订的连接 30 秒没消息就断开重连（看门狗），盘口员卡同时报 down。
- 只订要盯的币：总盘会本轮挑中的（挑完马上回调 `Runner.set_meeting_picks`，不等作战卡）+ 有效作战卡上的 + 有持仓的，不订整个候选池。新加的币先回填 K 线；卡过期、平仓后下一轮持仓管家（`keeper_tick`）里退订。全市场强平连接一直开着（选币要用）。盘口员卡开头写「盯着 N 个币（总盘会 n 持仓 m）」。

- 全市场连接同时订 `!contractInfo`：盯着 / 候选池 / 持仓的币排了 7 天内下架、或状态变成交割 / 已下线（`FINAL_STATUSES`：PRE_DELIVERING / DELIVERING / DELIVERED / CLOSE）→ `block_symbol`（和下架公告同一条路，原因写「币安合约信息：…」，已有持仓由管家平掉，要手动解封）。其余非 TRADING（结算中、待上线等）只当临时停：记进 `App.halted`，风控员只挡新开仓、已有持仓不动，状态回到 TRADING 自动放开，只记日志不报警。风控员另按 exchangeInfo 的 `deliveryDate` / `onboardDate` 拒「7 天内下架」「上线不到 `new_listing_block_h` 小时」，按 `symbolAdlRisk`（30 分钟一查）拒自动减仓风险「高」（`adl_high_block`），查不到都不挡。
- 币安强平流每个标的每秒最多推一笔，爆仓额是下限估计。
- 强平走全市场流 `!forceOrder@arr`（一个连接）。`feed.liquidations`（`LiquidationTracker`）记着所有币近 1 小时的强平，
  选币用 `feed.liquidations.liquidations_1h_usdt(symbol, now)` 填 `SymbolStats.liquidations_1h_usdt`。
  进程刚启动的一小时里数据不满 1 小时，偏小。
- WebSocket 没有持仓量：持仓量和多空比由 `market.derivs.DerivsFeed` 轮询 REST（只查总盘会 / 作战卡上的币；查失败跳过不刷屏），拥挤交易信号仍只看资金费率；CPI / 议息日历未接入。
- BTCUSDT 一直订（不在卡上也订）：「BTC 先动」信号和每个币快照里的 `btc5z` 要它。启动回填至少 1441 根 1 分钟线（24 小时波动）。
- 这台云端开发机访问币安返回 451（地区限制），实时连通性需要在 Mac mini 上验证。

## 预测员（Kronos）实测

- Mac mini M4（2026-10-23）：真推理可用，**约 3.1 秒 / 币**（4 条路径 × 预测 12 根 5 分钟 K 线），CPU。
- 早先 MPS 每次报 `scaled_dot_product_attention for MPS does not support dropout`，原因是没切 `eval()`（见下条）。
- 2026-10-23 核对官方用法后修了两处：加载后没切 `eval()`（推理时 dropout 开着，注意力变成因果；MPS 报错的真正原因），时间戳改用开盘时间（落在 5 分钟格点）。修完要在 Mac mini 上重测各尺寸耗时，并再试一次 `device = "mps"`；预测区间分布会变，之前的观察不能直接沿用。
- 修好后 Mac mini M4 重测 small：MPS 0.8 秒/币（5 币 4.1 秒，峰值 543 MB），CPU 1.4 秒/币（5 币 6.9 秒，500 MB）。`device = "auto"` 现在优先 MPS（CUDA > MPS > CPU），MPS 推理出错会退回 CPU 并一直用 CPU。
- 推理在后台任务里跑，几秒的耗时不进拍板延迟；开关仍是 `app.toml` 的 `predictor_enabled`（示例为 `true`，关掉时快环照常跑）。

## 预测员怎么跑（设置页可调）

实时行情只推 1 分钟 K 线，预测员自己按 UTC 整 5 分钟拼成 5 分钟线（缺分钟的整根丢掉、序列从头攒）；启动和订阅集新加币时用 REST 拉 `backfill_bars` 根 5 分钟线补历史（只补不算），所以下一根 5 分钟线收盘就能出预测，不用干等 5 个多小时。推理一直在后台单线程跑，`on_kline` / `request` 都立即返回。

回填完不用等收盘：要算的币（全部模式下每个币；按需模式下作战卡、持仓和总盘会挑中的币，包括之后新加进来的）历史一够就马上在后台算一次。面板上「等待」时会写清在等什么：回填没成功、按需模式还没有要算的币、或正在算第一轮。回填和实时行情都走币安主网公开行情（`fapi.binance.com` / `fstream.binance.com`），和 Demo 下单域名无关；每个币 1 分钟线 + 5 分钟线各一次请求（权重各 2），5 个并发。

| 键（`[predictor]`） | 默认 | 可选 | 作用 |
|---|---|---|---|
| `mode` | `on_demand` | `on_demand` / `all_symbols` | 按需：只在 5 分钟收盘时算作战卡里的币、有持仓的币和总盘会刚挑中的币（排队时先算）；哨兵出信号而该币没有新鲜预测时，后台补算一次（这次信号不等它）。全部：候选池每个币每根都算 |
| `size_policy` | `fixed` | `fixed` / `adaptive` | 固定用 `model_size`；自适应按开始推理时还在排队的个数选模型 |
| `model_size` | `base` | `mini` / `small` / `base` | 固定策略下用的模型（设置页「预测模型」：base 最大，更准但每币约 3.5 秒；small 标准约 1 秒）；加载失败自动改用 small |
| `adaptive_base_max_queue` | 0 | 0–20，且 < 下一项 | 自适应：排队 ≤ 它用 base |
| `adaptive_mini_min_queue` | 3 | 1–50 | 自适应：排队 ≥ 它用 mini，中间用 small |
| `backfill_bars` | 400 | 64–1500 | 启动回填多少根 5 分钟线 |
| `keeper_snapshot` | `direction` | `direction` / `rank` / `off` | 管家复查快照里怎么带预测员（改后重启）。`direction`（原行为）：`kronos.agree` = 平均路径终点在持仓方向一侧给 1、否则 0，排得出名次再带 `kronos.rank`；`rank`：只带 `kronos.rank`（top / mid / bottom，和拍板加分同一个全池排名），排不出名次就不写 `kronos`；`off`：不带。持仓第一题里描述预测员的那句跟着换（rank：「top 利多不利空，bottom 反之」，off：不提），rank / off 各有模板版本 `q9-krank` / `q9-koff`（direction 仍是 `q9`，逐字节不变），管家决策记录 `input.jev.kronos_snapshot` 记用的哪种 |

- 三个模型都钉了 Hugging Face 提交（见 `predictor/kronos.py` 的 `MODEL_SPECS`）：mini 配 Tokenizer-2k（上下文 2048），small / base 配 Tokenizer-base（上下文 512）。用到哪个才加载哪个，加载后常驻。
- 实测 small：约 3.0 秒 / 币，5 个币排队约 14.9 秒；按需模式下一般只有卡里的几个币，排得开。`Forecast.model` 记着这次用的是哪个。
- 开关仍是 `predictor_enabled`；运行中关掉会丢掉已有预测。
- `p_up` 是二值的：平均路径终点高于现价 = 1、低于 = 0、持平 = 0.5，不是「几成路径向上」。10-27 / 28 的 1927 条预测里 77% 是 0；主网 15 分钟线回测 4 小时方向准确率 49%，喊「跌」的只对 43%。所以拍板加分只看全池名次；管家快照默认的 `kronos.agree` 由它算，做多的仓多半拿到 0、做空的多半拿到 1，多空不对称，`keeper_snapshot = "rank"` 改成只给名次。面板预测员卡片写「平均路径终点在现价下方 / 上方」。

## ATR 周期和止损倍数（设置页可调）

止损距离 = 作战卡给的倍数 × ATR，所以 ATR 的周期必须和军师出卡时用的周期一致。原来风控员乘的是 1 分钟 ATR，军师按 1 小时 ATR 想“1–2 倍”，实际止损只有原意的几分之一，落在 1 分钟噪音里。现在全链路统一成一个可配的周期，默认 15 分钟。

| 键（`[fast_loop]`） | 默认 | 范围 | 作用 |
|---|---|---|---|
| `atr_period_min` | 15 | 5 / 15 / 30 / 60 | ATR 用几分钟 K 线（14 根）。改了要让军师按新周期出卡 |
| `atr_long_period_min` | 档位给（0 = 不用） | 0 / 15 / 30 / 60 / 240，且长于 `atr_period_min` | 长周期 ATR。写了止损 ATR = √(短周期 ATR × 长周期 ATR)；长周期还没攒够时先只用短周期。回填按长的那个拉 |
| `session_factor` | 档位给 | true / false | 止损 ATR 再乘曼谷时段系数：按入场那一刻的曼谷小时，周一到周五查 `session_factor_weekday`、周六周日查 `session_factor_weekend`（各 24 个数，0.3–3）。只在开仓时乘一次 |
| `atr_floor_7d` | 0 | 0–1 | 短周期 ATR 先取 max(当前, 7 天中位数 × 这个数)；0 = 关（默认：开平方之后下限只修一半，周末还偏宽）。7 天历史在回填时按 ATR 周期拉 REST K 线（一次最多 1500 根），之后实时攒；不够 1 天不设下限 |
| `min_stop_atr` | 0.5 | 0.1–5，且 < `max_stop_atr` | 作战卡止损倍数下限，低于它这个标的不做 |
| `max_stop_atr` | 4.0 | 0.5–10 | 作战卡止损倍数上限，超过这个标的不做 |
| `min_tp_r` / `max_tp_r` | 1.0 / 6.0 | 0.5–5 / 1–20 | 作战卡止盈倍数（相对止损距离）的范围 |
| `tp_r_cap` | 0（不封顶） | 0 或 0.5–20 | 下单用的止盈倍数封顶，见下面「止盈封顶」 |
| `stop_level_snap` | false | true / false | 止损挪到作战卡关键位外侧。默认关：价格碰到关键位后 93% 会穿过 0.1 ATR，挪过去只是让止损更宽 |
| `stop_level_buffer_atr` | 0.1 | 0–1 | 止损放在关键位外侧多少个 ATR（只在 `stop_level_snap` 开时用） |

- 止损距离 = 作战卡倍数（窄 / 中 / 宽）× √(短 ATR × 长 ATR) × 时段系数；数量 = 风险金额 ÷ 止损距离；手续费检查在乘完系数之后。风控员日志 `detail` 记 `atr_short / atr_long / session_factor`。各最长持仓档的周期和系数表见 `core/profiles.py`（研究 v3，12 个月数据；系数表每月重算）。
- 止盈封顶 `tp_r_cap`：风控员下单用的止盈倍数 = min(作战卡止盈下限（先按 `card_range_clamp` 夹进 `min_tp_r`–`max_tp_r`）, `tp_r_cap`)。它在取交集**之后**才截，所以从不因此拒单——不要拿 `max_tp_r = 1.0` 代替：卡的下限超过 `max_tp_r` 时，夹完没有交集或夹到的仍是卡给的数，前者整个标的被拒。截完如果比手续费下限（`min_tp_fee_multiple` × 2 × `taker_fee_bps`）还近，只截到手续费下限；卡本身就够不到手续费下限的照旧拒。止盈距离 `take_profit_pct` 按截后的倍数算，托管止盈单价格、限价止盈、穿过止盈价没成交的判断、回测都从它来，口径一致；止损、仓位、保命止损、移动止损都只看止损距离，不受影响。开了（> 0）风控员 `detail` 记 `tp_r_raw`（截前）和 `tp_r_capped`（实际下单），被手续费下限托住时加 `tp_r_cap_note`；移动止损 / 保本启动线加上开仓手续费（hummingbot 按扣过开仓手续费的净盈亏判启动，价格要走「启动线 + `taker_fee_bps`」）不近于截后的止盈时记 `trailing_vs_tp`（`trail_distance_r` 1.0 时启动线 = 1.0R + 手续费，止盈 1.0R 前永远不启动，所以推荐配 `trail_distance_r` 0.3）。`tp_r_cap` 比 `min_tp_r` 小时以 `tp_r_cap` 为准：`min_tp_r` 只管作战卡的止盈范围（卡下限低于它就抬上来或拒），截是之后的事，所以下单止盈可以低于 `min_tp_r`，只受手续费下限托底。`jev-replay` 的止盈也按封顶算；面板作战卡的止盈刻度按封顶画（不含手续费下限）。默认 0 = 原行为；Demo 推荐 1.0（2026-10-28 主网 1 分钟回放 194 笔：止盈多在 1.2–1.5R、很少到，单封 1.0R 约 +122 U/天，配回吐 30% 和移动 0.3R 约 +203 U/天）。面板「风控员 → 止盈放多远」可改，也能写进 `config/app.pending.toml` 的 `[fast_loop]`；热生效，只对之后新开的仓，已开仓的托管止盈单不动。回滚：改回 0（或删掉这一行）几秒内恢复；回退到没有这个键的旧代码前先删掉这一行（旧代码不认识的键会拒绝启动），账本没有新字段。
- 风控员会核对快照的 `atr_period_min` 和配置一致，对不上就拒单（接线错了止损会差好几倍）。
- 拍板人快照里的 `atr_bps` 跟着变成新口径，所以问题模板升到 `q2`（提示里写明 `atr_bps` 是 `stop_atr` 的单位）。
- 巡逻兵的 1% / 2%（1 分钟逆向波动）是价格百分比，和 ATR 无关，不用改。15 分钟止损变宽后，2% 的冲击线仍然明显宽于正常止损（BTC 15 分钟 ATR 一般 0.1%–0.3%）。

## Jev 通道：直连 TypeSafe 或经 OpenRouter

`[fast_loop] jev_provider` 选通道，默认 `typesafe`（直连）。

| | 直连 `typesafe` | 经 OpenRouter `openrouter` |
|---|---|---|
| key（`jiaoyi setup-keys`） | `JEV_API_KEY` | `OPENROUTER_API_KEY` |
| `jev_model` 写法 | `jev-1.14` | `typesafe/jev-1.14` |
| 地址 | `https://api.typesafe.ai/v1/systemone` | `https://openrouter.ai/api/v1/systemone` |
| 花费 | 按 `jev_usd_per_mtok` × 输入 token 估 | 用响应里的 `usage.cost` |
| 请求号 | 响应头 `x-typesafe-request-id` | 响应正文 `id`（`gen-dec-…`） |

- 两条通道都用同一个 `typesafe-sdk`，OpenRouter 只是换了 base URL（[说明](https://openrouter.ai/docs/guides/community/typesafe-sdk)）；问题模板、门槛、`JevResponse`、决策日志格式完全一样。
- `jev_model` 仍然必须锁具体版本，`~typesafe/jev-latest` 这类别名会被拒绝启动。OpenRouter 回的模型名带日期（例如 `typesafe/jev-1.14-20260917`），日志里记的是这个全名，不算“版本变了”。
- 只认所选通道的 key：选了 `openrouter` 却只有 `JEV_API_KEY`，不会接 Jev（反过来也一样），等于没有 Jev，按原规则不开新仓。
- 走 OpenRouter 时多一跳转发，延迟会高一点，见下面的实测。

### M1 实测（2026-10-23，Mac mini，曼谷，经 OpenRouter）

| | 结果 |
|---|---|
| 实际模型 | `typesafe/jev-1.14-20261014` |
| 3 次调用延迟 | 963 / 549 / 489 ms（第一次是冷启动：DNS + TLS 握手） |
| 单次花费 | 约 $3.6e-05（6 道开仓题，一次问完） |
| 解析 | 6 题全部解析成功 |

据此：
- 示例配置的 `jev_timeout_ms` 从 1000 放宽到 1500。首次 963ms 太贴近 1000，冷启动或网络抖一下就会白白放弃一笔开仓。已有的 `app.toml` 需要自己改。
- 启动时 `App.start` 会调一次 `JevDecisionModel.warmup()`：发一道很小的探测题把连接建好。探测不进决策日志、不动错误计数，失败只记日志、不影响启动。
- SDK 连接池空闲约 30 秒后断开，之后第一笔会重新握手（多几百毫秒），1500ms 的超时能盖住。如果实测超时率偏高，再考虑定时保活。
- 花费：按每天 200 次调用算，约 $0.007/天。
- OpenRouter 上 `typesafe/jev-1.13` 不支持 Box 题型；我们只用 Choice，不受影响。

## Jev 问题模板和复盘

- 模板（问题措辞、选项说明、快照格式）在 `gate/questions.py`，版本号 `TEMPLATE_VERSION`，内容指纹 `template_fingerprint()`。
  改了任何一个字，`tests/test_jev_templates.py` 就会失败：升版本号，在 `KNOWN_TEMPLATES` 里登记新指纹，旧版本保留。
  换模板版本和换 Jev 模型版本一样，门槛要重新校准。
- 拍板人和持仓管家的每条决策记录都带 `input.jev`（模板版本、指纹、请求号、类型、问题、完整快照）和
  `output.jev`（是否成功、错误、模型版本、延迟、花费、每个选项的概率）。被作战卡拦下、没真正调用的记录 `input.jev.called = false`。
- 复盘用 `jiaoyi.gate.review.iter_jev_calls(data_dir)`，把每次真正的调用摊平成一行，可按 `template` / `model` 分组；
  `template_dump()` 给出当前模板全文。

## 拍板人：一道「做不做」（q6，Aaron 2026-10-24）

`[fast_loop] gate_questions`：`"one"`（默认）只问一道 `take`，再加 `stop_width` / `size` 两题给风控员定止损档和仓位档
（不参与放不放行）；`"six"` 是旧的 6 道题，留着回放对照。风控员按回答里有没有 `genuine` 判断用哪种口径。

- 题目（模板 q9，沿用 q8 问法，`gate.questions.ONE_TAKE_PROMPT`）用旧的「Considering everything, should we take this trade in
  direction `side` now, after fees?」加目标说明。10-25 线上没有新单：原来那句「Take it only if all of these hold together」
  加完整作战卡，「做」中位只有 0.08（45 条 × 2 次，≥0.55 为 0）；换回这句、卡精简后中位 0.59（54/90），
  数据在 /mnt/project-files/gate-ab*.jsonl、gate-blocks*.txt、gate-confirm.jsonl。技术面的三个周期仍跟最长持仓档走
  （`gate.context.horizon_for`，2 小时档 15m / 1h / 4h）。
- 快照（上限 3200 字）在旧快照基础上：作战卡（方向、带类型的关键位、止损止盈范围、仓位上限、大盘判断 200 字；理由、
  进场区间、允许的信号、卡龄不给，A/B 里它们把「做」压低）；`tech` 技术面（1 分钟线内存聚合，4 小时及以上档补拉 4h / 1d K 线）；`flow` 5 分钟到 1 小时的主动买卖、
  大单、15 分钟爆仓；`book.imb_1m` / `imb_5m` 代替一瞬间的 `imb`；`deriv` 基差、离结算几分钟、近 3 次资金费率、大户人数多空比、
  自动减仓风险；`day` 24 小时行情。预测员**不进**快照。超长按 `card.regime` → `day` → `flow.big_15m` → `kronos` → `book` 删。
- REST 补拉（大户人数多空比、主动买卖量比、资金费率、24 小时行情、长周期 K 线）在信号进来时就开始，和排队同时进行，
  最多等 1 秒，没回来的不写；按币缓存（5 分钟 / 30 分钟 / 60 秒），查失败的 30 秒内不重试。
- 决策日志 `input.jev.extra` 存所有额外块的原始值（含快照超长删掉的）、没拿到的项 `missing`、等 REST 用了多久 `rest_ms`。
- 只剩一个门槛 `min_p_take`（面板「拍板人：做的把握至少」），各档默认 0.55；想多做一些可以放到 0.50。
- 按信号类型单独定门槛：`[fast_loop.min_p_take_by_kind]`（`dict[信号类型, 0–1]`，默认空 = 所有类型用 `min_p_take`，即原行为）。面板「拍板人 Jev → 按信号类型单独定门槛」每类一格，留空 = 跟总门槛；也能写进 `config/app.pending.toml` 随更新生效；热生效。类型名按 `SignalKind` 校验，写错或超出 0–1 拒绝。
  - 为什么有：3.2 天的漏斗里 `btc_lead` 33 个信号 Jev「做」都在 0.15–0.38，一笔没开；按主网 1 分钟线、真实止损 / 止盈 / 4 小时到时、12bp 成本模拟平均 +0.42R（不带移动止损）/ +0.17R（带），胜率 82–85%，12 个小时簇里 9 个为正；同期 Jev「做」和结果的秩相关是 −0.13。Demo 建议 `btc_lead = 0.0`。
  - 只换「做」这一道门的门槛（预测员加分照旧加在概率上），Jev 照常调用、照常记录（拍板人记录 `input.signal_kind`），风控员其余检查一条不少：熔断、暂停、持仓笔数、同币持仓、信号过期、要下架 / 刚上线 / 自动减仓、作战卡（有效、版本、允许的信号、方向）、止损档、价差和滑点、每笔风险、总敞口和每类币合计上限、交易所精度。Jev 不可用或没答止损档 / 仓位档照样拒绝。
  - 用了单独门槛时，拍板人记录的 `input` 和风控员记录的 `output.detail` 都写 `take_floor_kind`（类型）和 `take_floor`（当时的门槛）；没过时原因写「“做”概率 0.60 低于门槛 0.7（btc_lead 单独门槛）」。面板拍板人记录详情优先按记录里的 `take_floor` 标「做」过没过，旧记录才按现在的配置。
  - 同一次 BTC 大动会在多个小币上同时出 `btc_lead`，这些仓是相关的。每类币合计上限 `category_exposure_x` 管不住它（现在 risk.toml 里 alt = major = 15 = `max_total_exposure_x`，不起作用）；真正封顶的是 `max_open_positions`、总敞口 `max_total_exposure_x`，和下面的 `max_open_by_kind`。
  - 校准不被拖偏：`jev-backtest` / `gate-calibrate` / `jev-replay` 给总门槛 `min_p_take` 的推荐时，不算记录里有 `take_floor_kind` 的信号，也不算现在配了单独门槛的类型（`outcomes.global_floor_rows`，报告里写 `own_floor_excluded`）；分桶表照旧全算。`jev-replay` 判「会不会被拍板拦」时按这类的单独门槛。
- 按信号类型限制同时持仓：`[fast_loop.max_open_by_kind]`（`dict[信号类型, 0–100]`，默认空 = 不另外限，即原行为）。某类信号开的在管持仓（含开仓中、平仓中）到了这个数，同类新信号风控员就拒绝「btc_lead 信号开的仓已有 3 笔，达到这类上限 3 笔」；挂单没成交改市价追进前也查一遍。0 = 这类信号不开仓。面板「拍板人 Jev → 按信号类型限制同时持仓」每类一格，留空 = 不单独限；能写进 `config/app.pending.toml`；热生效。Demo 建议 `btc_lead = 3`。
  - 开仓指令 `OrderIntent` 和持仓记录 `PositionRecord` 新增 `signal_kind`，账户状态 `AccountState.open_by_kind` 按它数；升级前开的仓没有这一项，不计入（升级后第一批新仓开始算）。
  - 账本 `data/positions.json` 只在还在管的仓上写 `record.signal_kind`（为空、已平、开仓失败的不写），追单指令 `chase.signal_kind` 同样为空不写，所以没有在管持仓带类型时旧代码能直接读。回退代码前：先停主程序，跑 `python scripts/signal_kind_rollback.py data/positions.json` 看要删什么，再加 `--write`（先备份成 `positions.json.bak-signal-kind`），删掉在管持仓里的 `record.signal_kind` / `chase.signal_kind`。只关功能（清空 `max_open_by_kind`）不用跑。
  - 回滚：面板把这一格清空，或删掉 `[fast_loop.min_p_take_by_kind]` / `[fast_loop.max_open_by_kind]` 这一段，几秒内回到原行为；不用重启。
  `min_p_genuine` / `min_p_direction` / `max_p_toxic` 只在 `"six"` 下生效，设置页不显示，配置里写了也不报错。
- 题目里点名 `tech` 那句、`flow` / `deriv` / `day` 的说明还没在本地记录上测过，上线后用 `ab/run_ab.py` 重跑验证。

## 进场区间（Aaron 2026-10-24）

`[fast_loop] entry_zone_only`（默认关；10-24 数据显示开着会挡掉好信号，Aaron 10-25 同意关）：卡上这个币写了 `entry_zone`、触发价在区间外，主循环在问拍板人之前就拦下，
风控员名下记一条「没到区间」（`detail.by = "entry_zone"`）。往交易方向最多放宽 0.1R（做多高过上沿、做空低过下沿），
R ≈ 卡上 `stop_atr` 中值 × 止损 ATR；另一侧不放宽。卡上没写区间照旧不限。不加挂单等回踩。

## 拍板门槛与校准

旧的 6 道题问法下拍板一票否决：「真信号 / 做 / 方向 / 毒性」任何一个没过就不开。门槛 0–1 都收，档位只给推荐值；
paper 的推荐是 做 ≥ 0.55（一道题问法的门槛）、真信号 ≥ 0.35、毒性 ≤ 0.60、方向 ≥ 0.45（`profiles.PAPER_GATE`，各持仓档一样；
配置文件写了的优先、面板显示「手动固定」，点「恢复档位默认」就重新跟档位走）。

`[fast_loop] predictor_weight`（预测员意见占几成，各档推荐 0.2，0 = 不听；Aaron 10-24）：照官方 Kronos 回测用法
（Aaron 10-24「直接改成官方的」）：每个币一次采样 `[predictor] paths` 条（默认 10，温度 `temperature` 0.6、top_p 0.9），
模型自己平均成一条；分数 = 平均路径终点 ÷ 现价 − 1；同一时刻所有新鲜预测按分数排名（`rank_pct`，1 = 最看好）。
排在前 `rank_band`（默认 1/6）= 看好，后 `rank_band` = 看坏（看坏给做空用是我们的镜像，官方只做多），中间不表态；
一起预测的币少于 `rank_min_peers`（默认 6）就不排名。做多碰上看好、做空碰上看坏，风控员拿「做」和门槛比之前先加
`权重 × 0.5`（0.2 时 +0.10），其余不加也不扣，结果夹在 0–1。单个币的涨跌方向不看（论文里方向相关很弱，只看名次）。
信号没带预测、预测过期、排不出名次都不加。只动「做」这一道门，预测员不进拍板人的快照。`predictor_nudge_mode` 已停用。
风控员日志 `detail` 里写 `predictor`（「同向（看好，N 个币里排第 k）」/「不同向，不扣分（…）」/ 排不出名次 / 没有预测）、
`predictor_rank_pct`、`predictor_score_bps`、`predictor_nudge`、`take_raw`、`take_adj`；
管家快照的 `kronos` = `{agree: 平均路径是否朝持仓方向 1/0, rank: top/mid/bottom}`（模板 q7）。

预测员各档默认（官方论文表 8：回看 ≈ 往后预测的 5 倍，5m 480/96、15m 160/32、1h 80/12；攒够 64 根才开算，短档凑不够
就用 64）：30m 持仓 5m 64/6、1h 5m 64/12、2h 5m 120/24、4h 15m 80/16、8h 15m 160/32、12h 15m 240/48、24h 1h 120/24，
各档 10 条、温度 0.6。都能在面板改；配置文件写死的（例如 Mac 上 lookback 128 / paths 4）优先，点「恢复档位默认」跟档位走。
耗时按 `paths × pred_len × (lookback + pred_len)` 增长（官方没有 KV 缓存），每次推理的耗时记在预测员决策日志 `latency_ms`。
拒绝原因写成「“做”概率 0.18（预测员 +0.10）低于门槛 0.2」。面板拍板人那一行仍按拍板人原话标门槛，挪后的结论看风控员那一行。

`[fast_loop] gate_record_only`（拍板只记不拦）：不写 = 关；只在 paper 生效；testnet / live 写 true 当关、启动时日志警告「拍板只记不拦 只在模拟盘生效，已忽略」，面板总览顶部也提示；面板切到测试网 / 实盘时自动把它关掉并记进修改记录。
开着时门槛没过也放行，风控员日志 `detail.gate_would_block` 记下本会拦的原因，面板写「拍板本会拦，只记不拦放行」；
订单流有毒概率 ≥ 0.8 仍然拦，回答不全永远拦。

**校准：`jiaoyi jev-backtest [--since 2026-10-23T12:47]`**（要能连币安，在 Mac 上跑）。不用等新样本：
决策日志里每次拍板都记了完整快照和每个选项的概率，命令按快照里的价格、方向、最长持仓，拉之后的 1 分钟线算
「做了会怎样」——止损 / 止盈用风控员当时算的（没算到那步的按 1.5 × 快照 `atr_bps` 估、止盈 1.5R），
同一根线两边都碰到按先止损，都没碰到按到时收盘平，扣来回吃单费。放行的、拍板拦的、风控员拦的都算。
输出按模板版本分开（问法不同的概率不能混比）：做概率分桶、做 × 真信号 × 毒性 →
笔数 / 胜率 / 平均 R，并扫门槛给推荐（门槛内总 R 最大、至少 10 笔）。
明细缓存 `data/shadow/outcomes.jsonl`（算过的不重算），汇总 `data/debug/jev_backtest.json`。

**回放测试：`jiaoyi jev-replay --days 3 --n 400`**（要 Jev key 和币安，在 Mac 上跑）。实盘信号太少时用：
从 data.binance.vision 下载过去几天的 1 分钟线和逐笔成交（缓存 `data/history`），灌进和实盘同一个特征引擎、
同一个哨兵规则造信号（作战卡用回放卡：两边都可以、所有信号类型、关键位取前 24h / 4h 高低点、
止损 ATR / 止盈 R 用当前最长持仓档推荐），均匀抽 `--n` 个按当前模板和最长持仓真问 Jev；
止损 = Jev 选的止损档 × 和风控员同一个止损 ATR，止盈 = 止损 × 最小 R，结果算法同上。
默认选币 = 总盘会最常选的 8 个（`data/card_review/cards.jsonl`），可 `--symbols BTCUSDT,ETHUSDT`。
历史数据没有盘口（快照 `book` 写 null）、爆仓（为 0，不出「强平瀑布反弹」）、资金费率（不出「拥挤交易反向」）和预测员。
输出同样的分桶表 + 按现在门槛会放行多少 + 按信号类型，存 `data/replay/jev/<时间>/` 和 `data/debug/jev_replay.json`。

- 只因为拍板人概率没过门槛被拦的，风控员记 `detail.by = "gate"`，面板显示黄色「拍板放弃」；红色「风控员拒绝」只留给风控员自己的规则。
- 滑点（`risk.toml max_slippage_bps`）只拦不利方向（做多价格比触发时高、做空比触发时低），`detail.slippage_bps` 正数 = 不利；
  `detail.since_trigger_ms` 记触发到审完用了多久。推荐值按最长持仓档（约止损的 1/4）：30m 6 / 1h 8 / 2h 11 / 4h 16 / 8h 22 / 12h 28 / 24h 40bp。
- 拍板问题模板 q3：时间按最长持仓问（快照 `hold_min`），`atr_bps` 是止损用的 ATR（√短×长）。

## 进场挂单等回踩（默认关）

`entry_limit_pullback`（testnet / live 还要 `entry_limit_pullback_live`）打开后，风控员不再出市价单：
挂单价 = 现价 ∓ `entry_limit_offset_stop` × 止损距离（按档 30m 0.6 / 1h 0.4 / 2h·4h 0.25 / 8h 起 0.1，按最小跳动往有利方向取整），
OrderIntent 带 `limit_wait_s`（`entry_limit_wait_min` × 60）和 `limit_chase`。执行层只挂单（LIMIT_MAKER），成交前不挂保命止损；
等满没成交就撤单，记「挂单没成交已撤」（不报警）。`entry_limit_chase`（8h 起默认开）时改市价追进，
执行层不自己追，交回主循环：主循环另起任务，拿开仓锁（和开新仓同一把），按此刻的暂停 / 重启 / 拉黑 / 作战卡 / 账户再过一遍风控员（`RiskGuardImpl.recheck_chase`），过了才在锁里调 `open_chase`，没过就在风控员名下记「不追了：…」。按现价复查进场区间时，`chase_price_max_age_s`（默认 0 = 不查）> 0 且这个币的特征快照比现在旧超过它，就记「行情已旧 N 秒，这次不追」。这个检查只在打开 `entry_zone_only`、并且卡上这个币有进场区间时才做（也就是只在要按现价查区间的时候）；没有区间时追进不看价格，也就不查它有多旧。
持仓时长从成交时刻算。

## 保本和移动止损（持仓管家的开关：移动止损默认开，保本默认关）

`trailing_on` / `breakeven_on` 打开后，风控员在 OrderIntent 里带上 `trailing_activation_pct` / `trailing_stop_pct`，执行器交给
hummingbot 的 `trailing_stop`：盈利过启动线后止损线定在「当时盈利 − 回撤距离」，之后只往上挪不往下放，由 hummingbot 实时盯着平仓
（平仓原因「保本 / 移动止损」）。数字都按止损距离（R）给：

- 只开移动：启动线 = max(`trail_start_r`, `trail_distance_r` + 平仓手续费)，回撤距离 = `trail_distance_r`（档位 30m–2h 0.7、4h 起 1.0；`trail_start_r` 档位都是 0.7）；
- 只开保本：启动线 = `breakeven_trigger_r`，回撤距离 = 启动线 − 平仓手续费（启动时止损正好在保本价）；
- 都开：启动线 = `breakeven_trigger_r`，回撤距离 = min(`trail_distance_r`, 启动线 − 平仓手续费)。

只对之后新开的仓生效；保命止损（币安上的兜底单）不跟着挪，仍是程序挂了时的最后一道。拍板人管家的逻辑不变。

## 新作战卡方向反了（#314，`opposite_card_action`）

触发条件和原规则一模一样：最新作战卡有效、这个币的指令明确 long / short、`allowed_signals` 非空、在指令自己的 issued_at–expires_at 之内、方向和持仓相反。不问模型。

- `opposite_card_action = "close"`（默认）：立刻全平，和以前一字不差（同一条决策记录、同一个平仓原因，不读行情）。
- `"confirm_or_tighten"`：先看价格确认，三条任一成立就全平（同一条路、同一个原因 KEEPER，理由后面加「（价格确认：…）」）；没确认就交给执行器收紧（见下），持仓留着、模型照常复查。之后每轮管家（`keeper_interval_s`）和每次出新卡都重新判，卡还有效时一旦确认就平（`close` 自己去重：已在平 / 已有平仓申请不重复发）。名字保留 `confirm_or_tighten`，「收紧」按 `opposite_profit_mode` 分两种。

### 价格确认（`keeper.opposite_confirmation`，每条都写进决策记录 `output.card_rule.confirm`）

| 设置 | 默认 | 含义（0 / false = 这条不用） |
|---|---|---|
| `opposite_confirm_loss_r` | 0.5 | 按行情价已亏到这么多 R；R = 账本开仓价到账本止损价（开仓时的原始距离，收紧另存不改它） |
| `opposite_confirm_atr` | 1.0 | 从**这笔仓第一次看到这条反向指令**时的价格起，朝不利方向走了这么多倍 ATR（行情快照里止损用的 ATR） |
| `opposite_confirm_key_level` | true | 价格越过反向卡自己在开仓价之外最近的关键位：卡做空看开仓价下方最高的支撑 / 枢轴（跌破），卡做多看开仓价上方最低的阻力 / 枢轴（涨破）；卡里没有就不成立 |

三条都关 = 永远不确认、只收紧。**没有行情快照（比如刚重启）不平**：已收紧过就维持，没收紧过只让执行器收紧（执行器自己按标记价核对），有行情了再判确认。

「反向指令」= 币级指令身份 `币|issued_at|方向`（`opposite_instruction_key`）：整卡换版本（别的币变了）不变，这个币重新开会出了新指令才变。每笔仓每条指令第一次看到时的价格和时间（第②条起点）、这笔仓第一次碰到反向卡的时间和价格（反事实）落在 `data/keeper_opposite.json`，重启后接着用，平仓后清掉。

### 没确认时（`HbExecutorAdapter.tighten_stop`）

1. **亏着或赚得不够 → 全平**（用户决定，正常分支，只进决策记录，不发 CRITICAL、不写 last_error）：扣掉实际开仓手续费（开仓成交账）、按 `taker_fee_bps` 估的平仓手续费、再加 `opposite_tighten_buffer_bps` 的保本价，离触发价源（止损按标记价触发：带交易所时间、5 秒内的同环境标记价；CONTRACT_PRICE：同环境买一 / 卖一）不够 `max(opposite_tighten_mark_gap_bps, 保护状态机的标记价缓冲)` 就平：多要 保本价 < 价 ×（1 − gap），空要 保本价 > 价 ×（1 + gap）。按默认费率（单边约 5 bp）实际上要比标记价赚约 25 bp 以上才会收紧，其余照旧全平。这一条先于下面所有「等待 / 跳过」判断。
2. **赚得够**，按 `opposite_profit_mode`：
   - `trail`（默认）：`BracketManager.anchor_trailing` 在**第一次**碰到反向卡时的可成交价（多买一 / 空卖一，和移动止损同一来源，先过移动止损同样的盘口-标记价偏离核对）强制启动移动止损：峰值 = 这个价的净收益，回撤距离 = 开仓带的移动止损距离 × `opposite_trail_distance_mult`（落成 `trailing_distance_pct`，之后这笔仓的移动止损都按它），止损价用 `trailing_update` 同一套扣费逆算。之后照常的移动止损只收紧。已经激活的移动止损峰值 / 止损价只取更紧的。回撤距离约 1.2% 时，赚得不多的仓锚定出来的止损会在开仓价另一侧（用户选择的行为）；但永远不比正在挂的止损松（`_stop_target` 取最紧，非固定来源不换松）。锚定价、时间、指令落盘（`opposite_anchor_price` / `tightened_at` / `opposite_card_key`），**只锚一次**：换卡版本、重启都不重锚。开仓没带移动止损的仓退回 breakeven。锚定时盘口疑似插针（盘口价偏离标记价超过移动止损的上限）：`opposite_anchor_retry_s`（默认 30）秒内每轮返回 pending 再试、不告警（起始时刻落盘 `opposite_anchor_retry_since`，重启不重新计时），过了按原规则平 + CRITICAL；0 = 立刻平。
   - `breakeven`：保本价落成 `tightened_stop_price`。
3. 两种都**不改账本止损价**（开仓价回填会按 `stop_loss_pct` 重算它，R 也要用原始距离），而是作为 #333 `_stop_target` 的候选：移动止损价，或 `_extra_stop_candidates` 每轮从落盘的 `tightened_stop_price` 读出的 `("tightened", 价)`（卡变了也一直给，所以它是下限；收紧价不受移动止损最小步长限制）。和固定 / 正在挂的止损取最紧，新腿用途来自 `StopTarget.purpose`（breakeven 最紧时是 `tightened`，触发后平仓原因 `tightened_stop`；trail 是 `trailing`），换代先确认新代再撤旧代。和正在挂的止损比用 `live_stop_price`；换代后在挂的止损没变（候选不比它紧）记 `kept`，不记 `tightened`。
4. 回退 / 等待（都在第 1 条「赚得够」之后）：旧通道的仓（没有 `server_protection`）、从没完整托管（含开仓残单没终态、#325 开仓宽限期）→ 全平；保护在退出 → 跳过；曾完整托管、这一轮退回临时保护但旧止损仍确认在挂 → 落盘、返回 pending，由保护轮询换；临时保护且没有确认在挂的止损 → 跳过，交给保护状态机；上一代（移动止损换代）还在提交 / 撤销中 → 落盘、等它，不算失败。
5. 证据读不到（开仓成交账、标记价、盘口，或插针重试过期）→ 全平 + `server_protection.last_error` + CRITICAL（去重键 `opposite_tighten:<持仓号>`，不写 `trailing_last_error`）；换单没确认 → 已落盘、旧止损保留、last_error + CRITICAL，保护轮询继续换。
6. **语义变化（和 #333 一起）**：落盘之后才挂不稳的收紧价（比如还没换上价格就跌过去了），由 #333 夹到标记价旁可挂的最紧价并发紧急告警，不再走原规则平。
7. 收紧 / 锚定之后卡又转回持仓方向：不自动放松；持仓期间收紧价不清不放松。
8. 主循环按（持仓, 反向指令）在内存里记「已收紧 / 已够紧」，同一条指令之后每轮不再进执行器的锁。

### A/B（`opposite_card_ab`，0–1，只能配 `confirm_or_tighten`）

每个反向事件按 `sha256(持仓号|币|指令发布时间 issued_at|方向)` 前 8 字节固定抽签，小于这个比例走新规则，其余照旧全平；0 = 不分组（全走新规则）。同一条指令同一笔仓重启、重算、整卡换版本结果都一样；这个币重新开会出新指令才重抽。

### 决策记录

管家 `output.card_rule`：`instruction_key`、`arm`、`ab` / `ab_draw`、`branch`（`ab_legacy_close` / `confirmed_close` / `no_market_data_tighten` / `tighten`）、`price` / `atr` / `ref_price` / `ref_at`、`confirm`（每条的输入和是否成立）、`counterfactual`（新规则组：原规则会在何时何价平 = 这笔仓第一次碰到反向卡的时间和价格；原规则组：新规则会平还是收紧）。同一条指令同一支只记第一次。主循环记执行器结果（`output.opposite_tighten`：action / why / 模式 / 保本价 / 是否可挂 / 正在挂的止损 / 触发价源 / 锚定价 / 锚定止损 / 换后在挂止损），以及 `counterfactual`（估算保本价、是否可挂），结果变了才再记。面板持仓的托管保护里显示收紧价或锚定价、距离和理由（只读）。

### 回滚

改回 `opposite_card_action = "close"` 即恢复原规则（热生效）。

本 PR 新增：`FastLoopConfig.opposite_*`（10 个，旧代码的 app.toml 不写就行）；`ServerProtection` 上 7 个字段 `tightened_stop_price / tightened_reason / tightened_at / opposite_anchor_price / opposite_card_key / trailing_distance_pct / opposite_anchor_retry_since`（为空时不落盘，`registry.OPPOSITE_CARD_FIELDS`）；`LegPurpose` 的 `tightened`；`CloseReason.TIGHTENED_STOP`（`tightened_stop`）；管家的 `data/keeper_opposite.json`（旧代码不读，可删）。

没用过这条规则的账本和加字段之前一字不差，旧代码直接能读。**只要账本里有** `purpose = "tightened"` 的腿、`tightened_stop` 的平仓 / 退出原因，或上面 7 个字段任何一个有值，代码回退之前先停主程序，跑 `python scripts/opposite_card_rollback.py data/positions.json` 看要改什么，再加 `--write` 改（先备份成 `positions.json.bak-314`）：删掉这些字段、`tightened` 腿改 `fixed`（交易所上的单不动）、`tightened_stop` 改 `stop_loss`。锚定过的仓回退后移动止损按开仓指令带的距离继续走。

已知限制：收紧价按交易所价格精度取整，可能差一个最小变动价位，由缓冲盖住。

## 新作战卡把持仓的币改成中性（`card_neutral_action`）

照旧卡开的仓（例如 NEAR / QNT 照延后数据出的做空卡开的空单），新卡把这个币改成不做了，原来的管家不投票，仓一直拿着。这条规则复用上面反向卡没确认时的同一套收紧，不另写算法。

**什么算中性**（`JevKeeper._neutral_rule`，都要求最新卡有效、在这条指令自己的 issued_at–expires_at 之内、**指令发布时间晚于开仓时间**——开仓前就是这样的指令（例如照旧卡手动开的仓）不算「被换掉」）：

- 这个币的指令 `bias = no_trade`：会议本身观望（研究经理 / 组合经理 Hold、两人方向相反、转卡和评级相反）、封币、`stale_on_arrival_action = "no_trade"` 都算；
- 例外：闸门把会议方向改成的 no_trade（会议没给进场区间、评级只是偏向要等区间、类别敞口上限为 0）在卡上记 `CardSymbol.no_trade_from` = 会议本来的方向。它**和持仓同方向时不算中性**（研究和持仓一致，只是先别开新仓），反着持仓才算；没记（`None`）的按普通 no_trade 算；
- 或者 `allowed_signals` 为空且理由里有 `stale_on_arrival`（#341 到手即作废的只看不做）。方向和持仓相同、相反都算：作废的指令不算数。#341 截理由时只截原理由，`stale_on_arrival` 标记一定留着；
- 别的原因的只看不做（开会失败留着盯、指令过期留着盯、美股休市遮掉、`allowed_signals` 为空的普通卡）**不算**：这些方向没变，只是先不开新仓；
- 卡里没有这个币：只在 `card_neutral_on_absent = true` 时算（发布时间用整卡的 `created_at`）。持仓的币本来会强制留在卡上，所以默认不算；
- 方向明确反着持仓的（有信号的 long / short）归上面的反向卡规则，中性规则不管。之后卡从中性转成反向，反向卡规则接手；已锚定过的锚不重做也不放松（执行器看到 `opposite_anchor_price` 已有就返回 `kept`）。

**怎么处理**（热生效，面板「持仓管家 → 新作战卡把这个币改成中性」可改）：

| 设置 | 默认 | 含义 |
|---|---|---|
| `card_neutral_action` | `"hold"` | `hold` = 不管（现状，和加这条之前一字不差）；`tighten` = 亏着或赚得不够照旧全平，赚得够在**第一次看到中性卡时**的可成交价锚定移动止损；`close` = 直接全平（平仓原因 KEEPER） |
| `card_neutral_on_absent` | `false` | 卡里没有这个币也算中性 |
| `card_neutral_trail_distance_mult` | `1.0` | `tighten` 锚定的移动止损回撤距离 = 开仓带的移动止损距离 × 这个倍数 |

`tighten` 走 `HbExecutorAdapter.tighten_stop`，和反向卡没确认时完全同一条路，固定 `trail` 模式（开仓没带移动止损的仓退回 breakeven）：保本 / 可挂判定（`opposite_tighten_buffer_bps`、`opposite_tighten_mark_gap_bps`）、锚定（只锚一次，落盘在 `server_protection` 的 `opposite_anchor_price` / `opposite_card_key`，换卡、重启都不重锚，永远不比正在挂的止损松）、插针重试窗口（`opposite_anchor_retry_s`）、旧通道 / 从没完整托管照旧全平、证据读不到 CRITICAL、换代先确认新代，都和上面一样。指令身份 = 反向卡的 `币|issued_at|方向` 后面加 `|neutral`（卡里没有这个币：`币|absent|neutral`），按这个币的研究、不按整卡版本：别的币开会、并发会议合并卡只换版本，同一条中性指令不会再动一次；主循环按它缓存「已收紧 / 已够紧」，同一笔仓同一时间只排一次（`_card_rule_queued`），决策记录同一条指令同一支只记一次，执行器结果按（规则种类, action, why）去重。告警标题写「作战卡转中性…」（反向卡写「反向卡…」）。模型复查照常进行；`close` 立刻平的不再排模型复查。

**不做价格确认**：中性不是反向，没有「新方向」可确认；亏的本来就按可挂判定直接平，赚的只是锚住利润，不需要再等价格。

**行情没有或过旧**（没有行情快照，或比 `keeper_price_max_age_s` 旧）：一律不平。`tighten` 只让执行器收紧（`allow_close = False`：执行器本来要平的分支都改成 `{"action": "hold", "why": "no_close_<原因>"}`，下一轮再试；证据读不到照样 CRITICAL，正文写「行情过旧，这次不平」）；`close` 这一轮先拿住，等新行情；这一轮不算规则通道已处理，照常进管家复查：冻结盈利价照常判，模型复查按旧行情规则跳过。**和反向卡（#335）不同**：反向卡在旧行情下只是不做价格确认，执行器仍按交易所标记价判可挂，「亏着 / 赚得不够」照样平（`allow_close` 保持 true）；中性卡在旧行情下一律 `allow_close = False`。

**决策记录**：管家 `output.card_rule`：`kind = "neutral_card"`、`card_neutral_action`、`instruction_key`、`card_bias`（卡里没有 = null）、`issued_at` / `expires_at`、`price` / `price_stale_s`、`branch`（`neutral_close` / `neutral_tighten` / `no_market_data_tighten` / `no_market_data_hold`）、`counterfactual`（`{"legacy_action": "hold"}`）。同一条指令同一支只记第一次。主循环记执行器结果（`output.opposite_tighten`，多一个 `card_rule_kind = "neutral_card"`），结果变了才再记；亏着平是正常分支（INFO / 平仓记录），只有证据读不到才 CRITICAL。

**回滚**：改回 `card_neutral_action = "hold"` 即恢复（热生效）。回退代码时：

1. 先从 `config/app.toml` 的 `[fast_loop]` 删掉 `card_neutral_action` / `card_neutral_on_absent` / `card_neutral_trail_distance_mult` 三个键，不然旧代码按未知字段拒绝启动；
2. 账本没有新字段：锚定 / 收紧用的就是上面反向卡那 7 个字段，按上面反向卡的回滚步骤做；
3. 作战卡契约多了 `CardSymbol.no_trade_from`，为空时不落盘（和以前一字不差，`research_token` 也不变）；只要 `data/battle_card.json` 或 `data/card_review/cards.jsonl` 里有这个键（闸门改过观望的卡），回退前停主程序把这些 `no_trade_from` 键删掉，或等下一轮开会出新卡再回退；
4. 管家的 `data/keeper_opposite.json` 不记中性卡的东西。

## 模拟盘按主网价格判止损（`paper_stop_on_mainnet`，默认关）

问题：Demo 的止损（托管止损腿、保命单）按 **Demo 的标记价**触发，下单信号用的是**主网**行情。主网插针越过我们的止损时，Demo 标记价常常没跟着到，Demo 上的止损不触发、仓还拿着（「插针都过了止损线，我们的模拟还拿着」）；放到主网，这笔早被止损了。程序自己反应不够快、抓不住插针，所以记账不能靠反应速度。

设置（`[fast_loop]`，热生效，面板「执行 → 模拟盘按主网止损」可改，也能写进 `config/app.pending.toml`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `paper_stop_on_mainnet` | `"off"` | `off` = 现状；`mark` = 主网标记价（`markPrice@1s`）越过止损；`last` = 主网逐笔成交价（`aggTrade`）越过止损，插针更容易碰到 |
| `paper_stop_slippage_bps` | `2.0` | 按止损价记模拟成交时的滑点，按不利方向：多 = 止损价 ×（1 − bp），空 = 止损价 ×（1 + bp） |
| `paper_stop_book_pnl` | `true` | 面板已实现盈亏对记过账的仓用模拟成交价；`false` = 一律 Demo 实际成交 |

只对 `paper` / `testnet`；`live` 不管开关一律不做（实盘的交易所止损就是真的）。不加任何请求：用的就是主循环本来就订着的主网行情（持仓的币一直在订阅集里）。

**哪个止损**（`HbExecutorAdapter.paper_stop_levels`，多空对称，取价格先碰到的那个 = 多取最高、空取最低）：账本止损 `stop_loss_price`；交易所上在挂的每条止损侧托管腿（固定 / 移动 / 收紧 / 保命腿，`brackets.live_leg`：已确认、NEW、没触发）；已启动的移动止损价 `trailing_stop_price`（还没换上新腿也算）；反向卡收紧价 `tightened_stop_price`；旧通道仓在挂的保命单。碰到即算越过（多：价 ≤ 止损；空：价 ≥ 止损）。

**越过时**（`Runner._paper_stop_check`，在巡逻兵之前跑：主网上止损先于巡逻兵的市价平成交）：

1. 再查一次账本，这笔仍是 `open` 才算；正在平（`closing`，或本进程已在平）不记——平仓决定在越过之前，主网上那张市价单早成交了。
2. 记账（每笔仓只记第一次，写 `data/paper_stops.json`）：止损价、来源、越过的主网价和交易所时间、滑点、`paper_exit_price`、是否要求了平仓。决策记录（巡逻兵，`output.paper_stop`）+ 一条 INFO 推送「主网价格越过止损」。
3. Demo 自己的止损已在退出（`protection_exit_in_flight` = `exiting`）：只记账，不再下平仓单。否则在账本上记 `close_detail`「主网价格越过止损」，走原来的平仓入口 `Runner.close(…, stop_loss)`：同一笔正在平就不重复提交，执行器的每仓退出锁、托管直连平仓的去重和对账员的宽限都照旧。平仓记录和「平仓完成」推送写「原因：主网价格越过止损」（只在平仓原因是止损时显示，别的原因平掉的不挂这句）。
4. 记了账的仓巡逻兵不再另发平仓。平仓没成功或进程重启时，每次刷新持仓（管家每轮）对还开着的、记过账的仓再要求平一次（同一笔 30 秒内最多一次）；不会重新记账。

**怎么算钱**：`data/paper_stops.json` 只存模拟成交；Demo 实际成交照旧在 `positions.json`（`exit_price` / `realized_pnl_usdt`）。面板 `PanelFiles.ledger()` 在 `paper_stop_book_pnl` 开着时把记过账、已平、净盈亏已知的仓换成模拟成交：出场价 = `paper_exit_price`；净盈亏 = Demo 净盈亏 + 方向 × 数量 ×（模拟出场 − Demo 出场）（手续费仍是 Demo 实扣；Demo 出场价还没回填时按 方向 × 数量 ×（模拟出场 − 入场）− 手续费）。用到它的：首页今日已平盈亏 / 胜率 / 手续费、历史页（出场价下面小字写 Demo 实际出场，悬停看 Demo 实际净盈亏）、今日盈亏曲线、作战卡历史的已平盈亏。**不改**的：资金页（币安收入流水，没有流水时按账本的 Demo 实际成交）、日亏熔断和账户权益（执行层 `account()` 按 Demo 账本 / 余额）——这些是 Demo 账户里真实动的钱。

**回滚**：`positions.json` 没有新字段（`close_detail` 只在平仓进行中落盘，平完就不写，原本就是这样），旧代码直接能读。回退到没有这三个键的旧代码前，先从 `config/app.toml` 的 `[fast_loop]` 删掉 `paper_stop_on_mainnet` / `paper_stop_slippage_bps` / `paper_stop_book_pnl`（旧代码按未知字段拒绝启动）。`data/paper_stops.json` 旧代码不读，可留可删；删了面板就回到 Demo 实际成交。
