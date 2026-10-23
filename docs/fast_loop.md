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

- 启动时对每个标的调 `market.rest.fetch_recent_klines` 回填 1 分钟 K 线，再接 WebSocket，否则 ATR 要等 15 分钟。
- 每个行情事件：`features.update(ev)` → `patrol.check(ev, positions)`（有动作就调执行器平仓）→
  成交事件后 `trigger.check(features.snapshot(sym), card, now)`。
- 每个候选信号：`gate.evaluate` → `guard.review` → 放行才把 `verdict.intent` 交给执行器。
- 管家每 `keeper_interval_s` 秒复查一次每个持仓；`patrol.urgent` 里的持仓立刻复查，复查后 `discard`。
- 持仓平掉后调 `keeper.forget(position_id)`。
- 保镖构造时传 `state_dir=AppConfig.data_dir`，日亏熔断状态落盘在 `data/risk_state.json`，重启后当天仍锁住。
- 保镖需要 `LotRulesProvider`（币安 exchangeInfo 的 stepSize / minQty / minNotional），由执行层提供；拿不到就拒单。
- 拍板模型用 `gate.jev.decision_model_from_config(...)` 选：有 `JEV_API_KEY` 且 `[fast_loop] jev_model` 锁了具体版本 → Jev；
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
| `liq_ratio_1m` | 1 分钟较大一边的强平额 ÷ 30 分钟每分钟均值 | 倍 |
| `funding_rate` | 当期资金费率 | 小数，0.0001 = 0.01% |
| `mark_premium_bps` | 标记价相对指数价 | bp |

`FeatureSnapshot.atr`：1 分钟 K 线 14 根 Wilder ATR。`book`：盘口超过 10 秒没更新就为空（保镖看到空盘口会拒单）。
`BookMetrics.ofi` 和 `cvd` 都是 60 秒窗口、按 USDT 计。

## 已知限制

- 币安 forceOrder 流每个标的每秒最多推一笔强平，爆仓额是下限估计。
- WebSocket 没有持仓量，拥挤交易信号第一版只看资金费率；CPI / 议息日历未接入。
- 这台云端开发机访问币安返回 451（地区限制），实时连通性需要在 Mac mini 上验证。
