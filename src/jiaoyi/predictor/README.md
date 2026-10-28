# 预测员（Kronos）

官方 [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos)（MIT），默认 Kronos-base（最大），零训练。
照官方回测用法：每个币一次采样 10 条路径、模型平均成一条，按平均终点涨幅在所有币里**排名**（`latest()` 现算 `rank_pct` / `rank_view`）；单个币的涨跌方向不可信，只看名次。

## 行为

- 开关：`config/app.toml` 的 `predictor_enabled`。关闭时 `latest()` 返回 `None`，不加载模型、不报错；运行中可调 `set_enabled()`。
- 每根已收盘的 5 分钟线触发一次后台推理（独立任务 + 单线程池，`on_kline` 立即返回，不卡行情循环；退出时 `close()` 取消）；攒够 `min_bars` 根才开始，K 线有缺口则重新攒。
- 实时只有 1 分钟线：预测员自己按 UTC 整 5 分钟聚合；启动时主循环用 REST 回填 5 分钟线（`backfill`，只补不算）。默认按需模式（`[predictor] mode = "on_demand"`）只算作战卡里的币和有持仓的币（主循环 `set_wanted`），哨兵出信号缺预测时 `request` 后台补算；模型尺寸 fixed / adaptive 见 docs/fast_loop.md。
- 一次 `predict(sample_count=paths, T=temperature, top_p=0.9)`，返回平均路径；范围 = 平均路径的高低；`score` = 终点 ÷ 现价 − 1；`p_up` = 终点在现价之上 1 / 之下 0 / 持平 0.5（二值，不是「几成路径向上」；10-27 / 28 实测 77% 是 0，管家快照用它会多空不对称，见 docs/fast_loop.md 的 `keeper_snapshot`）。两个收盘周期后过期，过期等于没有预测。
- 名次：同一时刻所有没过期、有分数的预测里比它低的占几成（并列取中间）；不到 `rank_min_peers` 个币为 `None`；前 / 后 `rank_band` 为 `top` / `bottom`。
- 推理失败、超时、模型输出异常：只记状态，`latest()` 返回 `None`。连续 3 次失败上报 `down`。
- MPS/CUDA 推理报错自动退回 CPU 重试。
- 每次推理写一条决策日志（含耗时），并上报 `ModuleStatus`（`ok/degraded/down/disabled`、今日次数、平均耗时、最近错误）。

## 装模型（权重和源码都不进仓库）

```bash
uv pip install --python .venv/bin/python -e ".[kronos]"      # torch、pandas 等
make kronos-fetch   # 官方源码（不是 pip 包，vendor/ 已被忽略）+ 预先拉最大 / 标准模型权重，都钉提交号
```

权重和分词器按 `MODEL_SPECS` 里钉的提交号放在 `~/.cache/huggingface`：`make kronos-fetch` 和 `make update`
都会跑 `python -m jiaoyi.predictor.fetch_weights`（默认拉 base 和 small，已有缓存就跳过）。没拉到的话第一次推理时会现下载。

- 模型：`[predictor] model_size`，设置页「预测模型」：最大（base，Mac mini MPS 每币约 3.5 秒、峰值约 1.2 GB）/ 标准（small，约 1 秒、约 0.5 GB）/ 最快（mini）。
- 所选模型加载失败（权重没拉下来、内存不够）就改用标准模型，直到重启；面板状态写原因。标准模型也加载不了就当推理失败。
- 决策日志和模块状态里的版本写成「模型名@权重提交号前 7 位」，例如 `Kronos-base@2b55474`。

接线示例（`jiaoyi.app` 里）：

```python
from jiaoyi.predictor import KronosForecaster, KronosSettings

predictor = KronosForecaster(app_cfg.predictor_enabled, KronosSettings(), log_sink=decision_log)
```

没有 GPU 或权重时，测试用假后端（`tests/test_predictor.py`），`make check` 不需要任何模型。
