# 预测员（Kronos）

官方 [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos)（MIT），Kronos-small，零训练。
**只用来估波动范围**，方向近似抛硬币，`Forecast.p_up` 不要当方向信号。

## 行为

- 开关：`config/app.toml` 的 `predictor_enabled`。关闭时 `latest()` 返回 `None`，不加载模型、不报错；运行中可调 `set_enabled()`。
- 每根已收盘的 5 分钟线触发一次后台推理（独立任务 + 单线程池，`on_kline` 立即返回，不卡行情循环；退出时 `close()` 取消）；攒够 `min_bars` 根才开始，K 线有缺口则重新攒。
- 实时只有 1 分钟线：预测员自己按 UTC 整 5 分钟聚合；启动时主循环用 REST 回填 5 分钟线（`backfill`，只补不算）。默认按需模式（`[predictor] mode = "on_demand"`）只算作战卡里的币和有持仓的币（主循环 `set_wanted`），哨兵出信号缺预测时 `request` 后台补算；模型尺寸 fixed / adaptive 见 docs/fast_loop.md。
- 范围 = 多条采样路径（默认 4 条）的包络；`p_up` = 收在现价之上的路径占比。两个收盘周期后过期，过期等于没有预测。
- 推理失败、超时、模型输出异常：只记状态，`latest()` 返回 `None`。连续 3 次失败上报 `down`。
- MPS/CUDA 推理报错自动退回 CPU 重试。
- 每次推理写一条决策日志（含耗时），并上报 `ModuleStatus`（`ok/degraded/down/disabled`、今日次数、平均耗时、最近错误）。

## 装模型（权重和源码都不进仓库）

```bash
uv pip install --python .venv/bin/python -e ".[kronos]"      # torch、pandas 等
make kronos-fetch   # 官方源码（不是 pip 包），钉在 Makefile 里的提交号，vendor/ 已被忽略
```

权重 `NeoQuasar/Kronos-small` 与分词器 `NeoQuasar/Kronos-Tokenizer-base` 第一次推理时按 `KronosSettings` 里钉的提交号由
HuggingFace 自动下载到 `~/.cache/huggingface`。要提前下载：

```bash
.venv/bin/huggingface-cli download NeoQuasar/Kronos-small --revision 901c26c1332695a2a8f243eb2f37243a37bea320
.venv/bin/huggingface-cli download NeoQuasar/Kronos-Tokenizer-base --revision 0e0117387f39004a9016484a186a908917e22426
```

接线示例（`jiaoyi.app` 里）：

```python
from jiaoyi.predictor import KronosForecaster, KronosSettings

predictor = KronosForecaster(app_cfg.predictor_enabled, KronosSettings(), log_sink=decision_log)
```

没有 GPU 或权重时，测试用假后端（`tests/test_predictor.py`），`make check` 不需要任何模型。
