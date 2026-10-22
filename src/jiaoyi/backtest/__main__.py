"""命令行：``python -m jiaoyi.backtest``。

例子::

    # 合成行情，几秒钟跑通整条流程（不需要网络）
    python -m jiaoyi.backtest --synthetic --hours 2

    # 真实一天的币安 BTC 数据（自动下载并缓存到 data/history）
    python -m jiaoyi.backtest --symbol BTCUSDT --date 2026-10-20

结果：控制台汇总 + ``data/replay/<名字>/`` 下的决策日志（jsonl）和 report.json。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from jiaoyi.backtest.data import (
    DEFAULT_CACHE,
    fetch_day,
    iter_agg_trades,
    load_klines,
    merge_events,
)
from jiaoyi.backtest.engine import ReplayConfig, ReplayEngine
from jiaoyi.backtest.sim import LatencyModel
from jiaoyi.backtest.synthetic import synthetic_trades
from jiaoyi.core.config import ConfigError, load_risk_config
from jiaoyi.core.store import JsonlDecisionLog


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m jiaoyi.backtest", description="历史回放")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--date", help="UTC 日期 YYYY-MM-DD；与 --synthetic 二选一")
    p.add_argument("--synthetic", action="store_true", help="用合成行情")
    p.add_argument("--hours", type=float, default=2.0, help="合成行情的时长")
    p.add_argument("--risk", default="config/risk.toml", help="硬上限；没有就用示例文件")
    p.add_argument("--jev", default="rule:0.5", help="rule:<做的概率> 或 recorded:<jsonl>")
    p.add_argument("--jev-latency-ms", type=float, default=350.0, help="决策模型延迟")
    p.add_argument("--order-latency-ms", type=float, default=250.0, help="曼谷→币安下单延迟")
    p.add_argument("--fee-bps", type=float, default=None, help="单边手续费，默认取硬上限里的值")
    p.add_argument("--slippage-bps", type=float, default=1.0)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--cache", default=str(DEFAULT_CACHE))
    p.add_argument("--out", default="data/replay")
    p.add_argument("--name", default=None)
    p.add_argument("--wiring", default="tests.support.wiring:build", help="接线函数 模块:函数")
    return p


def _events(args: argparse.Namespace) -> tuple[datetime, object]:
    if args.synthetic or not args.date:
        start = datetime(2026, 10, 20, tzinfo=UTC)
        return start, synthetic_trades(args.symbol, start, int(args.hours * 3600))
    day = date.fromisoformat(args.date)
    k_path, t_path = fetch_day(args.symbol, day, Path(args.cache))
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, merge_events(
        load_klines(k_path, args.symbol), iter_agg_trades(t_path, args.symbol)
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    risk_path = Path(args.risk)
    if not risk_path.is_file():
        risk_path = Path("config/risk.example.toml")
    try:
        risk = load_risk_config(risk_path)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    start, events = _events(args)
    mod, _, fn = args.wiring.partition(":")
    build = getattr(importlib.import_module(mod), fn)
    w = build(risk, args.symbol, start, jev=args.jev, jev_latency_ms=args.jev_latency_ms)

    name = (
        args.name
        or f"{args.symbol}-{'synthetic' if args.synthetic or not args.date else args.date}"
    )
    out = Path(args.out) / name
    out.mkdir(parents=True, exist_ok=True)
    engine = ReplayEngine(
        features=w.features,
        trigger=w.trigger,
        gate=w.gate,
        guard=w.guard,
        cards=w.cards,
        risk=risk,
        log=JsonlDecisionLog(out),
        latency=LatencyModel(order_ms=args.order_latency_ms, jev_ms=None),
        config=ReplayConfig(
            initial_equity=args.equity, fee_bps=args.fee_bps, slippage_bps=args.slippage_bps
        ),
    )
    report = asyncio.run(engine.run(events))  # type: ignore[arg-type]
    (out / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(report.render_text())
    print(f"\n决策日志和 report.json 在 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
