# 测试与回放（测试工程师）

## 跑测试

```bash
make check                      # lint + 类型检查 + 全部测试（CI 同款）
.venv/bin/pytest tests/test_replay.py -q
```

## 给你的模块套契约套件

`tests/support/contracts.py` 里每个接口有一套可复用的测试。继承并实现工厂方法，pytest 自动收集：

```python
from tests.support.contracts import RiskGuardApprovalContract, EntryGateBehaviorContract


class TestMyRiskGuard(RiskGuardApprovalContract):
    def make_guard(self, cfg):
        return MyRiskGuard(cfg)


class TestMyGate(EntryGateBehaviorContract):
    def make_gate(self, model):
        return MyEntryGate(model)
```

| 套件 | 适用 | 检查什么 |
|---|---|---|
| `RiskGuardSafetyContract` | 风控员（桩也要过） | Jev 失败 / 无卡 / 过期卡 / 方向不符 / 暂停 / 日亏熔断 / 价差 / 满仓 一律拒，拒绝带原因且不带 intent |
| `RiskGuardApprovalContract` | 风控员真实现 | 放行的指令满足全部硬上限（`assert_intent_within_limits`）、随机 400 组合扫描、日亏次日恢复 |
| `EntryGateSafetyContract` / `BehaviorContract` | 拍板人 | 模型失败 / 抛异常 → `ok=False`；问的是标准 6 问 |
| `TriggerSafetyContract` | 哨兵 | 无卡、过期卡、卡里没有该标的、`no_trade` 不出信号 |
| `KeeperSafetyContract` | 持仓管家 | Jev 失败 → `hold` |
| `PatrolSafetyContract` | 巡逻兵 | 只会要求平仓，且只针对持有的标的 |
| `ExecutorBehaviorContract`、`LifeboatBehaviorContract` | 执行器、保命止损 | 开 / 平 / 查；保命止损挂 / 查 / 撤 |
| `DecisionModelContract` | 决策模型 | 回复和请求对得上，`ok=False` 必有 error 且无答案 |

夹具在 `tests/support/builders.py`（`b.card()`、`b.signal()`、`b.account()`、`b.jev_ok()`、`b.jev_failed()` 等），假实现在 `tests/support/fakes.py`。`tests/support/reference_risk.py` 是风控员的**测试替身**（不是生产代码），用来让回放在真风控员做好前跑通，并证明套件本身是对的。

`tests/test_bypass_guards.py` 扫描整个 `src/jiaoyi`，除 `risk/` 和 `contracts/` 外，任何构造 `OrderIntent` 的写法（别名导入、`model_validate`、`model_construct`、对 `*intent*` 变量 `model_copy`、继承、`getattr`）都会失败。

## 回放

```bash
python -m jiaoyi.backtest --synthetic --hours 2                # 合成行情，不要网络
python -m jiaoyi.backtest --symbol BTCUSDT --date 2026-10-20   # 真实历史，自动下载到 data/history
python -m jiaoyi.backtest ... --fee-bps 10 --jev-latency-ms 800 --order-latency-ms 400
python -m jiaoyi.backtest ... --jev recorded:data/jev_rec.jsonl
```

数据来自 data.binance.vision（K 线 + aggTrades，不用 API key）。回放引擎在 `src/jiaoyi/backtest/`，通过接口对象接线；默认接线 `tests/support/wiring.py` 用假的特征 / 哨兵 / 风控员。真实现做好后复制一份换成真类，用 `--wiring 模块:函数` 指过去。

已知简化：aggTrades 没有盘口，价差用固定值；止损 / 止盈每秒检查并按检查时的价格市价平（偏保守）；单标的；没有持仓管家和巡逻兵。
