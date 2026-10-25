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
- 管家每 `keeper_interval_s` 秒复查一次每个持仓；`patrol.urgent` 里的持仓立刻复查，复查后 `discard`。
- 持仓平掉后调 `keeper.forget(position_id)`。
- 风控员构造时传 `state_dir=AppConfig.data_dir`，日亏熔断状态落盘在 `data/risk_state.json`，重启后当天仍锁住。
- 风控员需要 `LotRulesProvider`（币安 exchangeInfo 的 stepSize / minQty / minNotional），由执行层提供；拿不到就拒单。
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

- 三个模型都钉了 Hugging Face 提交（见 `predictor/kronos.py` 的 `MODEL_SPECS`）：mini 配 Tokenizer-2k（上下文 2048），small / base 配 Tokenizer-base（上下文 512）。用到哪个才加载哪个，加载后常驻。
- 实测 small：约 3.0 秒 / 币，5 个币排队约 14.9 秒；按需模式下一般只有卡里的几个币，排得开。`Forecast.model` 记着这次用的是哪个。
- 开关仍是 `predictor_enabled`；运行中关掉会丢掉已有预测。

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
| `stop_level_snap` | false | true / false | 止损挪到作战卡关键位外侧。默认关：价格碰到关键位后 93% 会穿过 0.1 ATR，挪过去只是让止损更宽 |
| `stop_level_buffer_atr` | 0.1 | 0–1 | 止损放在关键位外侧多少个 ATR（只在 `stop_level_snap` 开时用） |

- 止损距离 = 作战卡倍数（窄 / 中 / 宽）× √(短 ATR × 长 ATR) × 时段系数；数量 = 风险金额 ÷ 止损距离；手续费检查在乘完系数之后。风控员日志 `detail` 记 `atr_short / atr_long / session_factor`。各最长持仓档的周期和系数表见 `core/profiles.py`（研究 v3，12 个月数据；系数表每月重算）。
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

- 题目（模板 q8，`gate.questions.ONE_TAKE_PROMPT`）用旧的「Considering everything, should we take this trade in
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
执行层不自己追，交回主循环：主循环另起任务，拿开仓锁（和开新仓同一把），按此刻的暂停 / 重启 / 拉黑 / 作战卡 / 账户再过一遍风控员（`RiskGuardImpl.recheck_chase`），过了才在锁里调 `open_chase`，没过就在风控员名下记「不追了：…」。
持仓时长从成交时刻算。

## 保本和移动止损（持仓管家的开关：移动止损默认开，保本默认关）

`trailing_on` / `breakeven_on` 打开后，风控员在 OrderIntent 里带上 `trailing_activation_pct` / `trailing_stop_pct`，执行器交给
hummingbot 的 `trailing_stop`：盈利过启动线后止损线定在「当时盈利 − 回撤距离」，之后只往上挪不往下放，由 hummingbot 实时盯着平仓
（平仓原因「保本 / 移动止损」）。数字都按止损距离（R）给：

- 只开移动：启动线 = max(`trail_start_r`, `trail_distance_r` + 平仓手续费)，回撤距离 = `trail_distance_r`（档位 30m–2h 0.7、4h 起 1.0；`trail_start_r` 档位都是 0.7）；
- 只开保本：启动线 = `breakeven_trigger_r`，回撤距离 = 启动线 − 平仓手续费（启动时止损正好在保本价）；
- 都开：启动线 = `breakeven_trigger_r`，回撤距离 = min(`trail_distance_r`, 启动线 − 平仓手续费)。

只对之后新开的仓生效；保命止损（币安上的兜底单）不跟着挪，仍是程序挂了时的最后一道。拍板人管家的逻辑不变。
