# 开发约定

所有人（包括各角色的开发线程）写代码前先读完这一页和[设计文档 v1](https://claude.ai/artifact/6h25UAzwM8Ui2yv11VJWjd)。设计文档和这里冲突时，以设计文档为准，并在 PR 里指出来。

## 三条铁律

1. **AI 永远不直接下单。** 拍板人、持仓管家、巡逻兵的 Jev 部分、军师团、预测员只输出判断或概率。唯一能进执行器的是 `OrderIntent`，只有保镖（`jiaoyi.risk`）能构造它。`tests/test_boundaries.py` 会检查：AI 角色的包不得导入 `jiaoyi.execution`，`jiaoyi.risk` 以外不得构造 `OrderIntent`。平仓也一样：管家和巡逻兵只返回 `KeeperVerdict` / `PatrolAction`，由接线代码去调执行器。
2. **故障即关闭。** Jev 超时或报错 → 开仓一律不做，盯仓维持现状；没有有效作战卡 → 不开新仓；配置缺项或超范围 → 拒绝启动。不许“出错时换个公式照常交易”。
3. **硬上限 AI 改不了。** 硬上限只在 `config/risk.toml`，由人改。作战卡和拍板人只能在它的范围里选，代码里不许写死比它更松的数。

## 审查清单（代码审查员逐条核对）

- **上线门槛（执行层）**：只改配置不能进实盘。启动时 `mode = "live"` 但没有本次有效的上线确认（确认词 + 面板密码，连续输错 5 次锁 15 分钟，切换后 Telegram 通知），就拒绝启动或降级为 paper 并告警。
- **按天清零（快环）**：日亏熔断、Jev 连续错误计数都按 `day_reset_tz` 的 0 点清零（buberlo 的已知 bug：从不清零），测试要覆盖跨日。
- **答案要完整（保镖、管家）**：`JevResponse.ok=True` 也要核对答案覆盖了请求里的每一问、选项一致、概率和为 1；缺任何一项按故障处理，不得用公式补。
- **交易所客户端**：AI 角色的包不得导入 ccxt、binance、hummingbot；币安密钥只有 `jiaoyi.execution` 能读（`tests/test_boundaries.py` 检查）。`gate` 调 Jev、`slow` 调 DeepSeek 可以用 httpx / aiohttp，审查时确认它们只连这两个服务。
- `OrderIntent` / `RiskVerdict` 签发后不能 `model_copy`、不能 `model_construct`；`jiaoyi.risk` 以外只能当类型注解用。

## 目录和归属

```
src/jiaoyi/
  contracts/      共享契约（pydantic）+ examples/*.json      架构师；改动需评审
  interfaces.py   模块接口（Protocol）                        架构师；改动需评审
  app.py          组装层：选实现或桩、启动/关闭、状态汇总     架构师（模块合并后由架构师接入）
  core/           配置读取、决策日志落盘、密钥读取            架构师
  market/         行情接入、特征、盘口员公式                  快环工程师
  sentinel/       哨兵（触发、去重、冷却）                    快环工程师
  gate/           拍板人 + 决策模型客户端（Jev）              快环工程师
  risk/           保镖（唯一签发 OrderIntent）                快环工程师；必须过代码审查员
  keeper/         持仓管家                                    快环工程师
  patrol/         巡逻兵                                      快环工程师
  execution/      hummingbot-api 适配、保命止损、对账员       执行工程师
  alerting/       Telegram 告警                               执行工程师
  slow/           选币、军师团（TradingAgents）→ 作战卡       慢环工程师
  predictor/      预测员（Kronos，带开关）                    预测员工程师
tests/                                                         测试工程师 + 各模块作者
```

接口和角色的对应：

| 接口（`jiaoyi.interfaces`） | 包 | 桩 |
|---|---|---|
| `MarketDataFeed`、`FeatureEngine` | `market` | `StubMarketDataFeed`、`StubFeatureEngine` |
| `Trigger` | `sentinel` | `StubTrigger` |
| `Predictor` | `predictor` | `StubPredictor` |
| `DecisionModel`、`EntryGate` | `gate` | `StubDecisionModel`、`StubEntryGate` |
| `RiskGuard` | `risk` | `StubRiskGuard` |
| `ExecutorAdapter`、`LifeboatStop`、`Reconciler` | `execution` | `StubExecutorAdapter`、`StubLifeboatStop`、`StubReconciler` |
| `Keeper` | `keeper` | `StubKeeper` |
| `Patrol` | `patrol` | `StubPatrol` |
| `SymbolScreener`、`BattleCardProvider` | `slow` | `StubSymbolScreener`、`StubBattleCardProvider` |
| `Alerter` | `alerting` | `StubAlerter` |
| `DecisionLog` | `core/store.py` | 已实现：`JsonlDecisionLog` |
| `SecretStore` | `core/secrets.py` | 已实现：钥匙串 + 环境变量 |

## 怎么加一个模块

1. 从 `main` 拉分支，一个 PR 只做一个角色（或一个角色的一部分）。
2. 在对应包里新建文件实现接口（接进 `jiaoyi.app` 由架构师做，模块 PR 不改 app.py），例如 `src/jiaoyi/gate/jev.py` 里写 `JevDecisionModel`，满足 `interfaces.DecisionModel`。**不要改桩**，桩留给测试和别的模块联调用。
3. 输入输出只用 `jiaoyi.contracts` 里的类型。缺字段或缺类型时，**不要在自己的包里另定义一份**，开一个只改契约的小 PR（附 JSON 示例和测试），在 PR 标题前加 `[契约]`，等合并后再用。
4. 每次判断都写一条 `DecisionRecord`（`role`、`summary` 一句中文、`input`、`output`、`latency_ms`、`cost_usd`），并定时 `heartbeat` 一条 `ModuleStatus`。团队页和历史页以后直接读这些文件，格式不用再改。
5. 加测试：至少覆盖正常路径、故障路径（超时、报错、数据缺失时是否关闭），涉及钱和数量的要有边界值。
6. 本地跑 `make check` 通过再推。CI 跑同样的命令。
7. 外部开源代码：MIT / Apache 可以复制，文件头注明来源仓库、提交号和许可证；GPL / AGPL（freqtrade、NOFX、FreqUI）**只看思路，不复制代码**。

## 契约规则

- 契约在 `src/jiaoyi/contracts/`，每个类型都有 docstring，每种都有 `examples/*.json`，`tests/test_examples.py` 保证示例能解析且来回一致。
- 契约是不可变的（`frozen`），未知字段直接报错（`extra="forbid"`）。要改值用 `model_copy(update=...)`（`OrderIntent`、`RiskVerdict` 除外，它们签发后不能改）。
- 只加不删。加字段要给默认值，保证旧数据还能读。改含义或删字段要升 `DecisionRecord.SCHEMA_VERSION` 这类版本号，并在 PR 里写清迁移。
- 时间一律带时区的 UTC（`AwareDatetime`），不带时区直接报错。显示时再换曼谷时间。
- 金额单位 USDT；百分比字段名带 `_pct`，`1.0` 表示 1%；基点字段带 `_bps`。
- 标的只能是币安 U 本位永续代码（`BTCUSDT` 这种），`Symbol` 类型会拒绝其它格式。

## 代码约定

- Python 3.11+，`asyncio`。快环里不许有阻塞调用（同步 HTTP、`time.sleep`）；不得已时放线程池。
- 类型检查是严格模式（`mypy --strict`），lint 用 ruff，行宽 100。`make format` 自动修。
- 注释和日志用中文，标识符用英文。
- 配置全部从 `config/*.toml` 读（`jiaoyi.core.config`），不读散落的环境变量。新增配置项必须进 `RiskConfig` / `AppConfig`，带合理范围（`RiskConfig` 的硬上限只拦数学 / 交易所约束；推荐值写在 `recommended_caps`，按资金算，只提示），示例文件同步更新。
- 网络调用都要有超时；超时数值来自配置。
- 每个外部调用（Jev、DeepSeek、hummingbot-api、币安）都记延迟；有花费的记花费。

## 密钥

- API key 不进仓库、不进聊天、不进网页、不进日志。日志里最多显示末四位（`core.secrets.mask`）。
- 读密钥只通过 `SecretStore`：正式环境 macOS 钥匙串（service `jiaoyi`，account 是密钥名），开发时 `.env`。
- 测试里不要写任何长得像真 key 的字符串。

## 运行环境

- 目标机器：曼谷的 Mac mini，Docker Desktop 跑 hummingbot-api（`make hb-up`），Condor 本机运行，大脑程序本机运行。
- hummingbot-api 和 Condor 用上游官方仓库，钉在 `Makefile` 里的提交号。升级要单独开 PR，先在测试网验证。
- 面板和 API 端口只在本机或 Tailscale 内网，不暴露公网。

## PR 流程

- 一个 PR 一件事，标题写清角色和内容，例如 `执行：hummingbot-api 适配器（模拟盘）`。
- PR 描述写：做了什么、对应设计文档哪一节、怎么测的、有没有改契约。
- 涉及下单、风控、止损、对账的 PR 必须由代码审查员单独评审。
- Aaron 合并。
