# 快环说明

快环 = 行情 → 特征 → 哨兵 → 拍板人 → 保镖 → （执行层）开仓；盯仓 = 持仓管家 + 巡逻兵。
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
- 保镖构造时传 `state_dir=AppConfig.data_dir`，日亏熔断状态落盘在 `data/risk_state.json`，重启后当天仍锁住。
- 保镖需要 `LotRulesProvider`（币安 exchangeInfo 的 stepSize / minQty / minNotional），由执行层提供；拿不到就拒单。
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

`FeatureSnapshot.atr`：`atr_period_min` 分钟 K 线 14 根 Wilder ATR（默认 15 分钟，由 1 分钟 K 线按 UTC 整点聚合，缺分钟的那根整根丢掉），周期写在 `FeatureSnapshot.atr_period_min`。`book`：盘口超过 10 秒没更新就为空（保镖看到空盘口会拒单）。
`BookMetrics.ofi` 和 `cvd` 都是 60 秒窗口、按 USDT 计。

## 已知限制

- 币安 U 本位 WS 按类型分路由：盘口类（depth / bookTicker）走 `wss://fstream.binance.com/public/stream`，成交 / 标记价格 / K 线 / 强平走 `/market/stream`；旧的 `/stream` 只剩盘口在推（2026-10-23 Mac mini 实测）。行情层每 40 个币各开一条 market、一条 public 连接；按币订的连接 30 秒没消息就断开重连（看门狗），盘口员卡同时报 down。
- 只订要盯的币：总盘会本轮挑中的（挑完马上回调 `Runner.set_meeting_picks`，不等作战卡）+ 有效作战卡上的 + 有持仓的，不订整个候选池。新加的币先回填 K 线；卡过期、平仓后下一轮持仓管家（`keeper_tick`）里退订。全市场强平连接一直开着（选币要用）。盘口员卡开头写「盯着 N 个币（总盘会 n 持仓 m）」。

- 币安强平流每个标的每秒最多推一笔，爆仓额是下限估计。
- 强平走全市场流 `!forceOrder@arr`（一个连接）。`feed.liquidations`（`LiquidationTracker`）记着所有币近 1 小时的强平，
  选币用 `feed.liquidations.liquidations_1h_usdt(symbol, now)` 填 `SymbolStats.liquidations_1h_usdt`。
  进程刚启动的一小时里数据不满 1 小时，偏小。
- WebSocket 没有持仓量，拥挤交易信号第一版只看资金费率；CPI / 议息日历未接入。
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
| `model_size` | `small` | `mini` / `small` / `base` | 固定策略下用的模型 |
| `adaptive_base_max_queue` | 0 | 0–20，且 < 下一项 | 自适应：排队 ≤ 它用 base |
| `adaptive_mini_min_queue` | 3 | 1–50 | 自适应：排队 ≥ 它用 mini，中间用 small |
| `backfill_bars` | 400 | 64–1500 | 启动回填多少根 5 分钟线 |

- 三个模型都钉了 Hugging Face 提交（见 `predictor/kronos.py` 的 `MODEL_SPECS`）：mini 配 Tokenizer-2k（上下文 2048），small / base 配 Tokenizer-base（上下文 512）。用到哪个才加载哪个，加载后常驻。
- 实测 small：约 3.0 秒 / 币，5 个币排队约 14.9 秒；按需模式下一般只有卡里的几个币，排得开。`Forecast.model` 记着这次用的是哪个。
- 开关仍是 `predictor_enabled`；运行中关掉会丢掉已有预测。

## ATR 周期和止损倍数（设置页可调）

止损距离 = 作战卡给的倍数 × ATR，所以 ATR 的周期必须和军师出卡时用的周期一致。原来保镖乘的是 1 分钟 ATR，军师按 1 小时 ATR 想“1–2 倍”，实际止损只有原意的几分之一，落在 1 分钟噪音里。现在全链路统一成一个可配的周期，默认 15 分钟。

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

- 止损距离 = 作战卡倍数（窄 / 中 / 宽）× √(短 ATR × 长 ATR) × 时段系数；数量 = 风险金额 ÷ 止损距离；手续费检查在乘完系数之后。保镖日志 `detail` 记 `atr_short / atr_long / session_factor`。各最长持仓档的周期和系数表见 `core/profiles.py`（研究 v3，12 个月数据；系数表每月重算）。
- 保镖会核对快照的 `atr_period_min` 和配置一致，对不上就拒单（接线错了止损会差好几倍）。
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
