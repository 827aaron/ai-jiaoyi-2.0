"""主循环行情热路径压测：合成行情（不连网）喂真的 ``Runner.on_event``，量每秒行情要多少 CPU。

18 个币：主流币逐笔成交每秒上百笔，每个币 depth20@100ms、markPrice@1s、kline_1m，偶尔强平；
4 笔持仓，作战卡覆盖全部币（哨兵规则都跑）。消息先编成 JSON 字符串，计时段里照实盘一样
``json.loads`` → ``parse_message`` → ``on_event``。

    PYTHONPATH=$PWD/src .venv/bin/python scripts/bench_on_event.py [--profile out.prof]

输出：预热 5 分钟（填满成交窗口）后，模拟 ``--seconds`` 秒行情的 CPU 秒数（÷ 模拟秒数），
以及按真实节奏回放 ``--paced`` 秒时事件循环的延迟（每 0.1 秒醒一次，记超时）。
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import math
import pstats
import random
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.app import App
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.loop import Runner
from jiaoyi.market.binance_ws import parse_message

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "src" / "jiaoyi" / "contracts" / "examples"

# (币, 起始价, 每秒成交笔数)
SYMBOLS: list[tuple[str, float, float]] = [
    ("BTCUSDT", 68000.0, 120.0),
    ("ETHUSDT", 3500.0, 90.0),
    ("SOLUSDT", 180.0, 40.0),
    ("XRPUSDT", 0.6, 25.0),
    ("DOGEUSDT", 0.15, 25.0),
    ("BNBUSDT", 600.0, 15.0),
    ("ADAUSDT", 0.45, 10.0),
    ("AVAXUSDT", 35.0, 10.0),
    ("LINKUSDT", 15.0, 10.0),
    ("SUIUSDT", 1.8, 15.0),
    ("TRXUSDT", 0.12, 5.0),
    ("LTCUSDT", 80.0, 8.0),
    ("DOTUSDT", 6.5, 6.0),
    ("NEARUSDT", 5.0, 8.0),
    ("APTUSDT", 9.0, 8.0),
    ("ARBUSDT", 1.1, 8.0),
    ("OPUSDT", 2.2, 8.0),
    ("WIFUSDT", 2.5, 15.0),
]
HELD = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT")
LIQ_PER_S = 0.5


def _ms(t: float) -> int:
    return int(t * 1000)


class Market:
    """各币随机游走的价格，按时间顺序吐出币安原样的消息字典。"""

    def __init__(self, start: float, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.t = start
        self.px = {s: p for s, p, _ in SYMBOLS}
        self.rate = {s: r for s, _, r in SYMBOLS}
        self.bar: dict[str, list[float]] = {}
        self.agg = 0

    def _step_price(self, sym: str, dt: float) -> float:
        p = self.px[sym] * math.exp(self.rng.gauss(0.0, 0.0004 * math.sqrt(dt)))
        self.px[sym] = p
        bar = self.bar.setdefault(sym, [p, p, p, p, 0.0])
        bar[1], bar[2], bar[3] = max(bar[1], p), min(bar[2], p), p
        return p

    def messages(self, seconds: float) -> Iterator[tuple[float, dict[str, Any]]]:
        """``seconds`` 秒的消息，每 100 毫秒一格：(时间戳, 消息)。"""
        rng = self.rng
        for _ in range(int(seconds * 10)):
            t0 = self.t
            out: list[tuple[float, dict[str, Any]]] = []
            for sym, _, _ in SYMBOLS:
                for _ in range(self._poisson(self.rate[sym] * 0.1)):
                    t = t0 + rng.random() * 0.1
                    p = self._step_price(sym, 0.01)
                    q = rng.expovariate(1.0) * 2000.0 / p
                    self.bar[sym][4] += p * q
                    self.agg += 1
                    out.append(
                        (
                            t,
                            {
                                "stream": f"{sym.lower()}@aggTrade",
                                "data": {
                                    "e": "aggTrade",
                                    "s": sym,
                                    "a": self.agg,
                                    "p": f"{p:.8g}",
                                    "q": f"{q:.6g}",
                                    "T": _ms(t),
                                    "m": rng.random() < 0.5,
                                },
                            },
                        )
                    )
                tb = t0 + 0.05
                p = self.px[sym]
                tick = p * 1e-5
                out.append(
                    (
                        tb,
                        {
                            "stream": f"{sym.lower()}@depth20@100ms",
                            "data": {
                                "e": "depthUpdate",
                                "s": sym,
                                "E": _ms(tb),
                                "T": _ms(tb),
                                "b": [
                                    [f"{p - tick * (i + 1):.8g}", f"{rng.uniform(1, 50):.3f}"]
                                    for i in range(20)
                                ],
                                "a": [
                                    [f"{p + tick * (i + 1):.8g}", f"{rng.uniform(1, 50):.3f}"]
                                    for i in range(20)
                                ],
                            },
                        },
                    )
                )
            second = math.floor(t0 + 0.1) != math.floor(t0)
            if second:
                ts = math.floor(t0 + 0.1)
                for sym, _, _ in SYMBOLS:
                    p = self.px[sym]
                    out.append(
                        (
                            ts,
                            {
                                "data": {
                                    "e": "markPriceUpdate",
                                    "s": sym,
                                    "E": _ms(ts),
                                    "p": f"{p:.8g}",
                                    "i": f"{p:.8g}",
                                    "r": "0.0001",
                                    "T": _ms(ts + 3600),
                                }
                            },
                        )
                    )
                if ts % 60 == 0:
                    for sym, _, _ in SYMBOLS:
                        o, h, lo, cl, qv = self.bar.pop(sym, [self.px[sym]] * 4 + [0.0])
                        out.append((ts, _kline(sym, ts - 60, o, h, lo, cl, qv)))
            for _ in range(self._poisson(LIQ_PER_S * 0.1)):
                sym = rng.choice(SYMBOLS)[0]
                p = self.px[sym]
                t = t0 + rng.random() * 0.1
                out.append(
                    (
                        t,
                        {
                            "data": {
                                "e": "forceOrder",
                                "o": {
                                    "s": sym,
                                    "S": rng.choice(("BUY", "SELL")),
                                    "q": f"{5000 / p:.6g}",
                                    "p": f"{p:.8g}",
                                    "ap": f"{p:.8g}",
                                    "z": f"{5000 / p:.6g}",
                                    "T": _ms(t),
                                },
                            }
                        },
                    )
                )
            self.t = t0 + 0.1
            # 每个币的成交本身按时间排（交易所推送就是这样），格子内不同类型的顺序打乱一点
            out.sort(key=lambda r: r[0])
            yield from out

    def _poisson(self, lam: float) -> int:
        n, p, threshold = 0, 1.0, math.exp(-lam)
        while True:
            p *= self.rng.random()
            if p <= threshold:
                return n
            n += 1


def _kline(sym: str, start: float, o: float, h: float, lo: float, cl: float, qv: float) -> Any:
    return {
        "data": {
            "e": "kline",
            "s": sym,
            "k": {
                "t": _ms(start),
                "T": _ms(start + 60) - 1,
                "i": "1m",
                "o": f"{o:.8g}",
                "h": f"{h:.8g}",
                "l": f"{lo:.8g}",
                "c": f"{cl:.8g}",
                "v": f"{qv / cl:.6g}",
                "q": f"{qv:.6g}",
                "x": True,
            },
        }
    }


def _card(now: datetime) -> c.BattleCard:
    data = json.loads((EXAMPLES / "battle_card.json").read_text())
    tmpl = data["symbols"][0]
    rows = []
    for i, (sym, px, _) in enumerate(SYMBOLS):
        row = dict(tmpl)
        row.update(
            symbol=sym,
            category="major",
            bias="long" if i % 2 == 0 else "short",
            key_levels=[
                {"price": px * 1.004, "kind": "resistance", "note": ""},
                {"price": px * 0.996, "kind": "support", "note": ""},
            ],
            entry_zone=None,
            issued_at=(now - timedelta(minutes=10)).isoformat(),
            expires_at=(now + timedelta(hours=3)).isoformat(),
        )
        rows.append(row)
    data.update(
        symbols=rows,
        created_at=(now - timedelta(minutes=10)).isoformat(),
        expires_at=(now + timedelta(hours=3)).isoformat(),
    )
    return c.BattleCard.model_validate(data)


class Cards:
    def __init__(self, card: c.BattleCard) -> None:
        self.card = card

    def current(self, now: datetime) -> c.BattleCard | None:
        return self.card

    def refresh_reason(self, *a: Any, **k: Any) -> str | None:
        return None


def _positions(prices: dict[str, float], now: datetime) -> list[c.PositionRecord]:
    base = json.loads((EXAMPLES / "position_record.json").read_text())
    out = []
    for i, sym in enumerate(HELD):
        px = prices[sym]
        long = i % 2 == 0
        out.append(
            c.PositionRecord.model_validate(
                {
                    **base,
                    "position_id": f"pos-{i}",
                    "symbol": sym,
                    "side": "long" if long else "short",
                    "entry_price": px,
                    "intended_price": px,
                    # 止损放远：巡逻兵照常算，但别真把仓平掉
                    "stop_loss_price": px * (0.9 if long else 1.1),
                    "take_profit_price": px * (1.2 if long else 0.8),
                    "opened_at": (now - timedelta(minutes=5)).isoformat(),
                    "lifeboat": None,
                }
            )
        )
    return out


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)


def build(data_dir: Path, start: float) -> tuple[Runner, Market, Clock]:
    clock = Clock(start)
    cfg = load_app_config(ROOT / "config" / "app.example.toml")
    fast = cfg.fast_loop.model_copy(update={"paper_stop_on_mainnet": "mark"})
    cfg = cfg.model_copy(
        update={"data_dir": str(data_dir), "fast_loop": fast, "book_depth_levels": 10}
    )
    app = App(
        load_risk_config(ROOT / "config" / "risk.example.toml"), cfg, clock=clock, offline=True
    )
    import dataclasses

    now = clock()
    app.m = dataclasses.replace(app.m, cards=Cards(_card(now)))
    runner = Runner(app)
    mkt = Market(start - 1500 * 60)
    feats = app.m.features
    # 1 分钟线回填 25 小时（24 小时波动、ATR），7 天 ATR 历史给中位数
    for sym, _, _ in SYMBOLS:
        hist = []
        p = mkt.px[sym]
        for k in range(672 + 20):
            o = p
            p *= math.exp(mkt.rng.gauss(0.0, 0.003))
            t0 = start - (692 - k) * 900
            hist.append(
                c.Kline(
                    symbol=sym,
                    interval=c.Interval.M15,
                    open_time=datetime.fromtimestamp(t0, UTC),
                    close_time=datetime.fromtimestamp(t0 + 900 - 0.001, UTC),
                    open=o,
                    high=max(o, p) * 1.001,
                    low=min(o, p) * 0.999,
                    close=p,
                    volume=1.0,
                )
            )
        feats.seed_atr_history(sym, hist)
    for minute in range(1500):
        ts = start - (1500 - minute) * 60
        for sym, _, _ in SYMBOLS:
            o = mkt.px[sym]
            cl = mkt._step_price(sym, 60.0)
            mkt.bar.pop(sym, None)
            ev = parse_message(_kline(sym, ts - 60, o, max(o, cl), min(o, cl), cl, 5e5))
            assert ev is not None
            feats.update(ev)
    mkt.t = start
    runner.positions = _positions(mkt.px, clock())
    return runner, mkt, clock


async def feed(runner: Runner, clock: Clock, rows: list[tuple[float, str]]) -> None:
    for t, raw in rows:
        clock.t = t
        ev = parse_message(json.loads(raw))
        if ev is not None:
            await runner.on_event(ev)


def gen(mkt: Market, seconds: float) -> list[tuple[float, str]]:
    return [(t, json.dumps(m)) for t, m in mkt.messages(seconds)]


async def paced(
    runner: Runner, clock: Clock, rows: list[tuple[float, str]]
) -> tuple[list[float], float, float]:
    """按消息时间戳的真实节奏回放，结构照实盘：收消息的任务按点把消息放进队列（像 WS 读线程），
    处理的任务 ``await queue.get()`` 再 ``on_event``；另一个任务每 0.1 秒醒一次，记晚醒多少秒。
    返回（延迟列表, 墙钟秒数, 处理完时落后多少秒）。"""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[tuple[float, str] | None] = asyncio.Queue()
    lags: list[float] = []
    done = asyncio.Event()

    async def monitor() -> None:
        while not done.is_set():
            t = loop.time()
            await asyncio.sleep(0.1)
            lags.append(loop.time() - t - 0.1)

    async def produce() -> None:
        i = 0
        while i < len(rows):
            now = loop.time()
            while i < len(rows) and t_wall0 + (rows[i][0] - t_sim0) <= now:
                q.put_nowait(rows[i])
                i += 1
            await asyncio.sleep(0.01)
        q.put_nowait(None)

    async def consume() -> None:
        while (row := await q.get()) is not None:
            t, raw = row
            clock.t = t
            ev = parse_message(json.loads(raw))
            if ev is not None:
                await runner.on_event(ev)

    t_wall0, t_sim0 = loop.time(), rows[0][0]
    mon = asyncio.create_task(monitor())
    await asyncio.gather(produce(), consume())
    wall = loop.time() - t_wall0
    done.set()
    await mon
    return lags, wall, wall - (rows[-1][0] - t_sim0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--paced", type=float, default=20.0)
    ap.add_argument("--profile", type=Path)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    start = float(math.floor(datetime(2026, 10, 30, 6, 0, tzinfo=UTC).timestamp()))
    with tempfile.TemporaryDirectory() as tmp:
        runner, mkt, clock = build(Path(tmp), start)
        await feed(runner, clock, gen(mkt, 300.0))  # 预热：填满 5 分钟成交窗口

        rows = gen(mkt, args.seconds)
        n_trades = sum(1 for _, r in rows if '"aggTrade"' in r)
        prof = cProfile.Profile() if args.profile else None
        cpu0 = time.process_time()
        if prof is not None:
            prof.enable()
        await feed(runner, clock, rows)
        if prof is not None:
            prof.disable()
        cpu = time.process_time() - cpu0
        print(
            f"消息 {len(rows)} 条（成交 {n_trades}），模拟 {args.seconds:.0f} 秒："
            f"CPU {cpu:.2f} 秒 = 每模拟秒 {cpu / args.seconds:.3f} CPU 秒"
            f"{'（开着 cProfile）' if prof else ''}"
        )
        if prof is not None:
            prof.dump_stats(args.profile)
            st = pstats.Stats(prof, stream=sys.stdout)
            st.sort_stats("cumulative").print_stats(args.top)
            st.sort_stats("tottime").print_stats(args.top)

        if args.paced > 0:
            rows = gen(mkt, args.paced)
            cpu0 = time.process_time()
            lags, wall, behind = await paced(runner, clock, rows)
            cpu = time.process_time() - cpu0
            lags.sort()
            print(
                f"真实节奏回放 {args.paced:.0f} 秒（墙钟 {wall:.1f} 秒，"
                f"处理完落后 {behind:.1f} 秒）："
                f"CPU {cpu / wall:.0%} 一个核；事件循环延迟（{len(lags)} 次采样）"
                f"p50 {statistics.median(lags) * 1000:.1f} ms，"
                f"p99 {lags[max(int(len(lags) * 0.99) - 1, 0)] * 1000:.1f} ms，"
                f"最大 {lags[-1] * 1000:.1f} ms"
            )
        for t in list(runner._tasks):
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
