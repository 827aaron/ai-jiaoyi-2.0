# M0 执行层：在 Mac mini 上一步步跑通

目标：在币安合约**测试网**上跑通一笔完整的 开仓 → 止损/止盈 → 平仓，并确认**保命止损同时挂出、平仓后撤销**。全程不碰真钱。

> **关于“模拟盘”**：hummingbot 官方只有现货的纯模拟连接器，永续合约没有。所以 `mode = "paper"` 和 `"testnet"` 都连**币安合约测试网**（`binance_perpetual_testnet`），区别只在模式标记和面板横幅。测试网的钱是假的，行情撮合是真实币安测试环境。`live` 才连主网，而且要过上线门槛（见最后一节）。

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
- **币安合约测试网**账号和 API key：<https://testnet.binancefuture.com>（登录后右下角 API Key）。测试网账户自带假 USDT。
- 币安**测试网**账户要用**单向持仓模式**（One-way Mode），不能是对冲模式，否则保命止损的只减仓单会被拒

## 2. 拉代码并装依赖

```bash
git clone https://github.com/827aaron/ai-jiaoyi-2.0 && cd ai-jiaoyi-2.0
make install            # 建 .venv
make init-config        # 生成 config/risk.toml、config/app.toml；app.toml 里 mode 保持 "paper"
make check-config
make hb-fetch           # 拉上游 hummingbot-api 和 Condor 到 vendor/，钉在 Makefile 里的提交
```

## 3. 起 hummingbot-api（Docker）

```bash
make hb-setup           # 上游的交互式 setup：它会问 API 用户名 / 密码 / 配置密码，生成 vendor/hummingbot-api/.env
make hb-up              # 起 hummingbot-api + EMQX + Postgres，端口只绑 127.0.0.1:8000
curl -s -u 用户名:密码 http://127.0.0.1:8000/executors/types/available | head -c 300
```

最后一条能返回执行器类型列表，说明 API 通了。**记下你在 setup 里设的 API 用户名**（默认 `admin`）。

把币安测试网 key 交给 hummingbot-api（它加密保存）：浏览器开 <http://127.0.0.1:8000/docs>，登录后用
`POST /accounts/add-credential/master_account/binance_perpetual_testnet`，在页面里直接填 key / secret。
不要把 key 发到聊天里。

## 4. 把密钥放进钥匙串

```bash
.venv/bin/jiaoyi setup-keys HUMMINGBOT_API_PASSWORD BINANCE_API_KEY BINANCE_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
```

每个会先问要不要写，再由系统的 `security` 命令在终端里提示你输入（输入不回显、不进命令行参数、不进历史）。
`BINANCE_API_KEY / SECRET` 填**测试网**的，是我们自己的程序（保命止损 + 对账员）直连币安用的；
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
- 追踪止损：契约的 `OrderIntent` 只有 `trailing_stop_pct`，没有激活价，无法对应 hummingbot 的 `TrailingStop(activation_price, trailing_delta)`。M0 遇到就**拒绝执行**（`UnsupportedIntent`），不降级。需要的话由架构师加契约字段。
- 币安 2025-12 起把条件单挪到 algo 订单接口。`ccxt>=4.5` 会自动走新接口；升级 ccxt 前先在测试网跑第 5 步。
- 账户权益 = hummingbot 报的 USDT 钱包余额 + 持仓浮动盈亏。请对比币安网页上的“保证金余额”是否一致。
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
- 状态文件在 `data/live_gate.json`（权限 600），**不进仓库**

实盘前还需要：主网 key 用**子账户**、只开合约权限、**不开提现**、杠杆在交易所侧设上限（设计文档附录 B）。
