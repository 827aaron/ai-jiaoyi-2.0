# M0 执行层：在 Mac mini 上一步步跑通

目标：在币安合约**测试网**上跑通一笔完整的 开仓 → 止损/止盈 → 平仓，并确认**保命止损同时挂出、平仓后撤销**。全程不碰真钱。

> **关于“模拟盘”**：hummingbot 官方只有现货的纯模拟连接器，永续合约没有。所以 `mode = "paper"` 和 `"testnet"` 都连**币安 Demo Trading**（hummingbot 里仍叫 `binance_perpetual_testnet`，见第 3 步的补丁），区别只在模式标记和面板横幅。Demo 的钱是假的。币安已停用旧的 testnet.binancefuture.com；要回退旧环境见第 3 步末尾。`live` 才连主网，而且要过上线门槛（见最后一节）。

## 0. 先说清楚哪些已验证、哪些要你在 Mac mini 上验证

| 部分 | 状态 |
|---|---|
| 适配器、保命止损、对账员的逻辑 | 已用内存假件的单测覆盖（`make check`），含故障路径 |
| 对 hummingbot-api 的 REST 调用 | 按钉住的上游提交读源码写成，请求形状由单测检查；**没有对真实 hummingbot 跑过** |
| 对币安的 ccxt 调用（挂 / 查 / 撤条件单） | 参数由单测检查；**没有对真实币安（含测试网）跑过** |
| 一笔完整交易 | **需要你在 Mac mini 上跑第 5 步的冒烟脚本**；有问题把输出贴给协调者 |

## 1. 前置

- macOS，已装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（并已启动）、`uv`、`tmux`、`git`、`make`
- 一个 Telegram bot（找 @BotFather 建）和你自己的数字 ID（找 @userinfobot）
- **币安 Demo Trading** 账号和 API key：<https://demo.binance.com>（登录后在 API 管理里建 Demo 的 key；主账号的真实 key 不能用在这里）。Demo 账户自带假 USDT。Demo 接口域名：REST `https://demo-fapi.binance.com`，WS `wss://demo-fstream.binance.com`（币安官方文档）。
- 币安 **Demo** 账户要用**单向持仓模式**（One-way Mode），不能是对冲模式，否则保命止损的只减仓单会被拒

## 2. 拉代码并装依赖

```bash
git clone https://github.com/827aaron/ai-jiaoyi-2.0 && cd ai-jiaoyi-2.0
make install            # 建 .venv
make init-config        # 生成 config/risk.toml、config/app.toml；app.toml 里 mode 保持 "paper"
make check-config
make hb-fetch           # 拉上游 hummingbot-api 和 Condor 到 vendor/，钉在 Makefile 里的提交
```

`config/risk.toml` 的硬上限**只给推荐，不设限制**：你填的就是上限。程序只拦数学上不成立的值（≤0、百分比超过 100、保命止损倍数不大于 1），其余你填什么都收；杠杆能开多少由币安按合约决定（下单前回读校验）。推荐值按账户资金自动算（面板设置页每项旁边显示「推荐 X（按 NU 算）」；终端拿不到资金，按 1000U 算），比推荐冒险时 `check-config` / `status` / `run` 和面板会黄色提示，但不拒绝启动。具体币种能开多少倍杠杆以币安为准。

| 项 | 推荐公式 |
|---|---|
| 每笔风险 `risk_per_trade_pct` | 资金 < 2 万U 1%；2–10 万 0.75%；≥ 10 万 0.5% |
| 日亏熔断 `daily_loss_halt_pct` | 每笔风险 × 3 |
| 最大杠杆 `max_leverage` | 资金 < 1000U 10 倍（不然凑不够最小下单额），否则 5 倍 |
| 总敞口 `max_total_exposure_x` | 3 倍 |
| 同时持仓 `max_open_positions` | 资金 ÷ 200U，取 2–5 |
| 单笔名义上限 `max_notional_per_trade_x` | 权益 × 2 倍（金额随资金变；`max_notional_per_trade_usdt` 可选再加绝对硬顶，不写就不限） |
| 保命止损倍数 / 止盈手续费倍数 | 1.5 / 2，和资金无关 |

## 3. 起 hummingbot-api（Docker）

```bash
make hb-setup           # 上游的交互式 setup：它会问 API 用户名 / 密码 / 配置密码，生成 vendor/hummingbot-api/.env
make hb-up              # 先给 hummingbot-api 镜像打 Demo 补丁（见下），再起 hummingbot-api + EMQX + Postgres，端口只绑 127.0.0.1:8000
curl -s -u 用户名:密码 http://127.0.0.1:8000/executors/types/available | head -c 300
```

最后一条能返回执行器类型列表，说明 API 通了。**记下你在 setup 里设的 API 用户名**（默认 `admin`）。

**Demo 补丁**：上游 hummingbot 的 `binance_perpetual_testnet` 连接器把旧测试网域名写死（`TESTNET_BASE_URL` / `TESTNET_WS_URL`），
上游暂无 Demo 支持。`make hb-up` 会先跑 `make hb-patch`：拉 `hummingbot/hummingbot-api:latest`，用 `docker/hb-demo/` 在上面
再打一层，把这两个常量改成 `https://demo-fapi.binance.com/fapi/` 和 `wss://demo-fstream.binance.com/`，重新打成同名本地镜像，上游 compose 不用改。
补丁找不到原文就构建失败（上游改了要重审）。币安文档只给了这两个 Demo 域名，没有单独的用户数据流 / ws-fapi 域名，
所以**第一次冒烟要确认 hummingbot 的持仓/订单推送能收到**，收不到就把现象发出来。
回退旧测试网：`config/app.toml` 里 `sim_environment = "legacy_testnet"`，并用 `HB_SIM=legacy_testnet make hb-up`（会重新拉未打补丁的上游镜像覆盖本地补丁镜像）。两边必须一致，否则保命止损和执行器分在两个环境。冒烟时请确认 Demo 上 algo 条件单能挂、能查、能撤。

`make hb-up` 会把 hummingbot 连的环境写进 `data/hb_sim_environment`（数据目录不是 `data` 时用 `DATA_DIR=...`），`jiaoyi` 启动时和 `sim_environment` 对比，缺标记或不一致都拒绝启动。上游镜像钉了 digest（Makefile 的 `HB_IMAGE_DIGEST`），升级前先在 Demo 上跑冒烟。

把币安 Demo key 交给 hummingbot-api（它加密保存）：浏览器开 <http://127.0.0.1:8000/docs>，登录后用
`POST /accounts/add-credential/master_account/binance_perpetual_testnet`，在页面里直接填 key / secret。
不要把 key 发到聊天里。

## 4. 把密钥放进钥匙串

```bash
.venv/bin/jiaoyi setup-keys HUMMINGBOT_API_PASSWORD BINANCE_TESTNET_API_KEY BINANCE_TESTNET_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
```

每个会先问要不要写，再由系统的 `security` 命令在终端里提示你输入（输入不回显、不进命令行参数、不进历史）。
`BINANCE_TESTNET_API_KEY / SECRET` 填 **Demo** 的（变量名沿用，不改）（主网用另一组 `BINANCE_API_KEY / SECRET`，两组分开存，配错模式也不会串用），是我们自己的程序（保命止损 + 对账员）直连币安用的；
`HUMMINGBOT_API_PASSWORD` 是第 3 步 setup 里设的 API 密码。检查：

```bash
.venv/bin/jiaoyi key-status      # 只显示末四位
```

## 5. 冒烟：一笔完整交易

```bash
.venv/bin/python scripts/m0_smoke.py --symbol BTCUSDT --quantity 0.002 --hold-s 20 --hb-username 你的API用户名
```

脚本做的事和期望的输出：

1. 读账户权益 → `通过  读到账户权益 …`
2. 开一笔小仓（2x 杠杆，止损 1%，止盈 2%，保命止损 1.5%）→ `通过  开仓成交 …`
3. 查币安条件单里有 `lb-<position_id>` 这张只减仓 STOP_MARKET → `通过  币安上挂着保命止损 …`
4. 跑一次对账 → `通过  对账干净`
5. 持仓 20 秒后调用平仓 → 账本无在管持仓、保命止损已撤、币安该标的无仓位，三条都应 `通过`

任何一条 `失败`，把整段输出发给协调者。你也可以在币安测试网网页上肉眼确认：开仓后“条件单 / 未平条件单”里有一张 STOP_MARKET（只减仓），平仓后它消失。

再手动验证三件事（它们是这一层存在的理由）：

- **缺止损会补**：开仓后在测试网网页手动撤掉那张保命止损，等 ≤30 秒（对账间隔，来自 `reconcile_interval_s`；冒烟脚本只在开仓后跑一次对账，持续对账由接线后的主程序负责），应收到 Telegram“保命止损缺失，已补挂”。
- **孤儿仓位会报**：在测试网网页手动开一笔别的标的，下一轮对账应收到“发现无人管理的仓位”（只告警，不会替你平）。
- **补不上止损会平仓**：开仓后撤掉保命止损并让币安拒绝补挂（例如把价格推过止损位触发 -2021）应当轮平仓；其它原因连续 2 轮补挂失败后平仓。平仓也失败时每轮都发 critical。
- **静默重试会露出**：把测试网账户余额花光让开仓被拒，应收到“执行器异常”，里面带 hummingbot 的 `last_error`。

## 6. Condor（可选，M0 的人用界面）

Condor 官方是本机跑（uv + tmux），不在 Docker 里：

```bash
make condor-install     # 会问 Telegram bot token 和你的用户 ID，并连到本机 hummingbot-api
make condor-run         # tmux 会话 condor；make -C vendor/condor logs 查看，Ctrl+B 再 D 脱离
```

浏览器 <http://localhost:8088>（仅本机）。Condor 的 Portfolio 看资产，Trade 页用于手动 / 紧急平仓。

## 7. 要核对的上游细节（第一次跑真实环境时留意）

这些是读源码时无法 100% 确认、写代码时已按最稳的假设处理的点：

- `position_executor` 的 `custom_info.current_position_average_price`：开仓成交后应有成交均价，适配器靠它判断“已成交”。如果字段名不同，开仓会一直卡在 opening，15 秒后（`fill_timeout_s`）返回未成交状态，对账员不会误挂保命止损。
- 追踪止损：`trailing_stop_pct`/`trailing_activation_pct` 映射为 `triple_barrier_config.trailing_stop = {activation_price, trailing_delta}`（都是小数，0.003 = 0.3%）。请在测试网确认 hummingbot 接受这个形状。
- 币安 2025-12 起把条件单挪到 algo 订单接口。`ccxt>=4.5` 会自动走新接口；升级 ccxt 前先在测试网跑第 5 步。
- 账户权益 = hummingbot 报的 USDT 钱包余额 + 持仓浮动盈亏。请对比币安网页上的“保证金余额”是否一致。
- 认领标记：建执行器时把 `jy-<position_id>` 写进请求的 `controller_id`（hummingbot-api 会原样存下并在执行器列表里返回、可按它过滤）。超时 / 5xx 这类结果不明的失败，先按标记查，查到就认领；对账员也会按标记认领。请在测试网确认 `POST /executors/search` 带 `controller_ids` 能查到刚建的执行器。
- 日亏基准：0 点后第一次读账户若晚于 5 分钟，用币安 income 流水（`fapiPrivateGetIncome`：REALIZED_PNL、COMMISSION、FUNDING_FEE）倒推 0 点的钱包余额；查不到则取当前与上次落盘权益中更大的并告警。
- 保命止损用 `reduceOnly` + 数量，而不是 `closePosition=true`：后者会平掉该标的**全部**仓位，以后“先平一半”（两个执行器）时会误伤。

## 8. 切实盘：上线门槛

配置里写 `mode = "live"` **不够**。`build_execution(mode=live)` 在没解锁时会直接拒绝建立实盘连接：

```bash
.venv/bin/jiaoyi set-panel-password     # 先设面板密码（≥8 位，只存加盐哈希）
.venv/bin/jiaoyi go-live                # 输入确认词“确认切换实盘” + 面板密码
.venv/bin/jiaoyi go-paper               # 随时重新上锁
```

- 密码连错 **5 次锁 15 分钟**（锁定状态落盘，重启也不会重置；确认词输错不计次数）
- 解锁成功后，如果钥匙串里有 Telegram 的 token 和 chat id，会发一条 critical 通知
- 改面板密码要先输对旧密码，改完自动重新上锁
- 解锁时记下 `config/risk.toml` 的指纹：**之后改了硬上限，实盘启动会被拒**，要重新 `go-live`（`--risk` 指定要绑定的文件）
- 启动时会核对 hummingbot 与我们自己的币安连接是不是**同一个账户**（USDT 钱包余额 + 持仓列表）；对不上拒绝启动并发 critical。注意这是间接核对，两个全新空仓且余额相同的账户分不出来
- 同一个币已有在管持仓时，保镖和执行层都拒绝再开（含反向）
- 状态文件在 `data/live_gate.json`（权限 600），**不进仓库**

实盘前还需要：主网 key 用**子账户**、只开合约权限、**不开提现**、杠杆在交易所侧设上限（设计文档附录 B）。

## 9. 下单精度与决策日志（接线时用）

- `ExchangeInfoLotRules`（`execution/lot_rules.py`）实现保镖的 `LotRulesProvider`：`await refresh()` 去币安 exchangeInfo 拉 `stepSize / minQty / minNotional`，`lot_rules(symbol)` 只读缓存（同步，快环里可直接调）。启动时先拉一次，之后用 `run_lot_rules_refresh` 每小时刷新。**故障即关闭**：没拉成功过、缓存超过 6 小时、或合约不在列表里，都返回 `None`，保镖就拒单。
- `build_execution(..., decision_log=JsonlDecisionLog(...))` 会让执行层按 `acceptance.py` 的约定写决策日志：`executor` 成交 = action、平仓 = close（`output.net` 是净盈亏）、`lifeboat` 挂上 = action、`reconciler` 没修好的问题 = error（修好的 = action），三者都发心跳。用 `python -m jiaoyi.acceptance --stage m0 --hours 24` 验收。

## 10. 开机自启与每日备份（launchd，Mac mini）

```bash
scripts/install_launchd.sh              # 装 colima（brew service）、Condor、每日备份
scripts/install_launchd.sh --load-run   # 另外装 jiaoyi run（纸面跑通、你认可之后再加）
```

脚本可以重复运行：colima 已加载就跳过，其余 agent 先卸载再装；某个 agent 装失败会报错并以非零退出，其余照装。

- `com.jiaoyi.condor`：Condor 前台运行（`make run-fg`），退出后 30 秒自动重启。
- `com.jiaoyi.backup`：每天 04:10 把 `data/decision_log`、`module_status.json`、`positions.json` 打包到 `data/backups/<日期>/`，保留 30 天。
- `com.jiaoyi.run`：默认只生成到 `data/launchd/`，**不装**；加 `--load-run` 才进 LaunchAgents（登录后自启，崩溃自动重启），模式取 `config/app.toml`。切 live 仍要走第 8 节的门槛。
- `com.jiaoyi.watchdog`：每分钟跑一次 `jiaoyi watchdog`。主循环的心跳（`data/module_status.json`，30 秒写一次）超过 2 分钟没更新就发一条 Telegram「主进程停了」，恢复后再发一条；每次掉线只发一次。主循环从没启动过（没有 `data/runtime.json`）时不发。它和主进程分开跑，所以 `jiaoyi run` 崩了、卡死了也能报。验收：`launchctl bootout gui/$(id -u)/com.jiaoyi.run` 或杀掉 `jiaoyi run`，2–3 分钟内收到 Telegram。
- 你自己停掉 `jiaoyi run`（Ctrl+C、`launchctl bootout`、改配置重启）时也会收到一次「主进程停了」，这是正常的；重新启动后会收到「心跳已恢复」。
- 看门狗和主进程在同一台机器上：整台 Mac mini 断电、断网时它也发不出去。这种情况要靠机器外面的检查（例如外部定时 ping），还没做。
- 日志都在 `data/logs/`。脚本只写用户级 LaunchAgents，不需要 sudo；重复运行会先卸载再重装。
- 备份只有本机一份，不防硬盘损坏；`data_dir` 不是 `data` 时脚本要同步改。

## 11. 日常更新（装好之后只需要这一条）

```bash
make update
```

依次做：`git pull --ff-only` → `make install`（补装依赖）→ `make check-config`（配置不合格就停下，不会重启）→ 若 `com.jiaoyi.run` 已在 launchd 里，`launchctl kickstart -k` 重启它；没装就提示用 `scripts/install_launchd.sh --load-run`。

- 首次安装、`hb-setup`、密钥、`install_launchd.sh` 都只做一次，之后不用再配。
- 重启只要几秒，看门狗的阈值是 2 分钟，不会误报；但会收到一次「主进程停了」/「心跳已恢复」也属正常。
- 持仓不受重启影响：交易所上的保命止损单一直在，重启后按交易所实际持仓接管（见执行层的重启恢复）。
- 本地有未提交改动、或 `main` 不能快进时 `git pull --ff-only` 会失败并停下，不会覆盖你的东西。
- 改了 `config/risk.toml` 要重新 `go-live` 才能实盘（见第 8 节）。
