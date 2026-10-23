# AI 交易机器人币安版（ai-jiaoyi-2.0）

币安 U 本位永续合约短线系统（加密货币 + 美股、黄金等 TradFi 合约），持仓几分钟到几小时，跑在曼谷的 Mac mini 上。

- **设计文档（唯一权威）**：[AI 交易系统设计 v1](https://claude.ai/artifact/6h25UAzwM8Ui2yv11VJWjd)
- **怎么参与开发**：[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)

## 一句话结构

慢环（军师团 TradingAgents + DeepSeek）提前写**作战卡**；快环在信号出现时让拍板人（Jev）一秒内判断，保镖按硬上限算好数量，交给 **hummingbot-api** 下单和盯仓，再在币安挂一张**保命止损**。AI 只给判断，永远不直接下单。

```
行情(ccxt WS) → 特征/盘口员 → 哨兵 → 拍板人(Jev) → 保镖 → hummingbot-api 执行器 → 保命止损(ccxt)
                                 ↑作战卡                         ↓
                      军师团(慢环) ← 选币              持仓管家 / 巡逻兵 / 对账员（只能更安全）
```

## 当前状态

M0 之前：仓库骨架、共享契约、模块接口和桩实现。所有桩都“故障即关闭”：不出信号、不开仓。

## 目录

| 路径 | 内容 |
|---|---|
| `src/jiaoyi/contracts/` | 共享契约（pydantic）：作战卡、候选信号、Jev 请求/回复、决策日志、持仓、硬上限、候选池、模块状态。`examples/` 里是每种契约的 JSON 示例 |
| `src/jiaoyi/interfaces.py` | 模块边界（Protocol） |
| `src/jiaoyi/<角色>/stub.py` | 各角色的桩实现，真实现写在同一个包里 |
| `src/jiaoyi/core/` | 配置读取（缺项/超范围拒绝启动）、决策日志落盘、密钥读取 |
| `config/*.example.toml` | 硬上限和运行配置的保守默认值 |
| `docker-compose.yml` | 引入上游 hummingbot-api（Docker） |
| `tests/` | 测试 |

## 本机开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
make install        # 建 .venv 并安装依赖
make init-config    # 从示例复制 config/risk.toml、config/app.toml（已存在则不覆盖）
make check-config   # 检查配置，不合格会说哪一项错
make check          # lint + 类型检查 + 测试（CI 跑的就是这个）
```

## hummingbot-api 和 Condor（M0 用，执行工程师负责）

两者都用上游官方仓库，钉在设计时读过的提交上（见 `Makefile` 里的 `HB_API_REF`、`CONDOR_REF`）：

```bash
make hb-fetch       # 拉上游到 vendor/（不进仓库）
make hb-setup       # 跑上游的 setup，生成它自己的 .env 和 MQTT 账号
make hb-up          # docker compose 起 hummingbot-api + EMQX + Postgres，只绑 127.0.0.1
make condor-install # Condor 官方是本机 uv + tmux 运行，不走 Docker
```

Condor 默认发匿名统计，运行时设 `CONDOR_TELEMETRY=off`。hummingbot-api 端口不要暴露到公网。

## 面板

`jiaoyi panel` 起局域网网页（默认 :8090，只接受局域网访问；暂停、平仓、改设置走指令文件，面板自己不下单），见 [docs/panel.md](docs/panel.md)。

## 密钥

API key **绝不**进仓库、聊天或网页。正式环境用 `setup-keys` 存进 macOS 钥匙串（service `jiaoyi`），开发时可以放在本机 `.env`（已在 `.gitignore`）。`jiaoyi key-status` 只显示末四位。
