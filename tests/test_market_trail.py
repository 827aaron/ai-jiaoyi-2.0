"""大盘移动止损（方向闸门）：z 出 ±1 开段、从段内极值反向走 k 倍 15 分钟 ATR 锁这个方向的新仓。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import random
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core import market_trail as mt
from jiaoyi.core.hotconfig import HOT_FAST, patch_inplace
from jiaoyi.core.market_trail import (
    BTC,
    ETH,
    MIN_HISTORY,
    REGIME_CHECKS_DIR,
    SHADOW_DIR,
    Leg,
    MarketTrail,
    TrailSeries,
    ema_value,
    read_ema,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.loop import Runner
from tests import fastloop_helpers as h
from tests.conftest import ROOT, load_example
from tests.test_loop import _app

FIXTURE = ROOT / "tests" / "fixtures" / "market_trail_btc_eth_1m.json"
ON = h.FAST.model_copy(
    update={
        "signal_market_trail_on": True,
        "signal_market_trail_k": 1.0,
        "signal_market_trail_max_extreme_age_min": 0,
        "signal_market_ma_on": False,
    }
)
"""只开移动止损、不作废：下面老的用例钉这道闸自己的算法。"""
SHORT, LONG = c.Side.SHORT, c.Side.LONG
T0 = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)


class Log:
    def __init__(self) -> None:
        self.records: list[c.DecisionRecord] = []

    def write(self, record: c.DecisionRecord) -> None:
        self.records.append(record)

    def heartbeat(self, status: c.ModuleStatus) -> None:
        pass


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@cache
def _fixture() -> dict[str, Any]:
    return dict(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _minutes(until: str | None = None) -> list[tuple[datetime, float | None, float | None]]:
    fx = _fixture()
    start = datetime.fromisoformat(fx["start"])
    out = []
    for i, (b, e) in enumerate(zip(fx[BTC], fx[ETH], strict=True)):
        t = start + timedelta(minutes=i)
        if until is not None and t > datetime.fromisoformat(until):
            break
        out.append((t, b, e))
    return out


def _feed(tr: MarketTrail, clock: Clock, rows: list[tuple[datetime, float | None, float | None]]):  # type: ignore[no-untyped-def]
    for t, b, e in rows:
        ts = int(t.timestamp())
        if b:
            tr.observe_close(BTC, ts, b)
        if e:
            tr.observe_close(ETH, ts, e)
        clock.at = t + timedelta(seconds=5)
        tr.tick(clock.at)


def _episodes(rows: list[dict[str, Any]], ref: str, since: str) -> list[tuple[str, str]]:
    eps: list[tuple[str, str]] = []
    cur: dict[str, Any] | None = None
    for r in rows:
        if r["ref"] != ref or r["t"] < since:
            continue
        if r["lock"] and cur is None:
            cur = r
        elif cur is not None and r["lock"] != cur["lock"]:
            eps.append((cur["t"][11:16], r["t"][11:16]))
            cur = r if r["lock"] else None
    return eps


def _shadow_rows(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for f in sorted((root / SHADOW_DIR).glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
    ]


def _replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **cfg: Any):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(mt, "SAVE_EVERY_S", 10**9)
    fast = ON.model_copy(update={"signal_market_trail_shadow": True, **cfg})
    log = Log()
    start = _minutes()[0][0]
    clock = Clock(start)
    tr = MarketTrail(
        fast, tmp_path, recorder=RoleRecorder(c.Role.RISK_GUARD, log), clock=clock, seed=False
    )
    _feed(tr, clock, _minutes())
    return tr, log


# ---------------------------------------------------------------- 真实分钟数据
def test_market_trail_real_minutes_reproduce_the_replay_lock_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10-26 18:00 起的 BTC / ETH 永续 1 分钟收盘：和离线回放 direction-budget-2026-10-30 一致。"""
    _, log = _replay(tmp_path, monkeypatch)
    rows = _shadow_rows(tmp_path)
    assert _episodes(rows, "btc", "2026-10-29T20:00Z") == [
        ("20:09", "20:34"),
        ("20:51", "22:10"),
        ("22:23", "23:32"),
        ("00:07", "00:11"),
        ("00:38", "00:42"),
        ("00:54", "01:31"),
        ("01:37", "01:57"),
        ("02:14", "03:34"),
        ("03:59", "04:19"),
    ]
    # 影子：另两条参照照样每分钟算（15 分钟线按开盘 :00 / :15 / :30 / :45 切）
    assert _episodes(rows, "eth", "2026-10-29T20:05Z") == [
        ("20:09", "20:34"),
        ("20:51", "21:04"),
        ("21:08", "00:05"),
        ("00:20", "00:22"),
        ("00:38", "00:42"),
        ("00:53", "01:31"),
        ("01:33", "04:19"),
    ]
    assert _episodes(rows, "btc_eth", "2026-10-29T20:05Z") == [
        ("20:09", "20:34"),
        ("20:51", "23:46"),
        ("00:38", "00:42"),
        ("00:53", "01:31"),
        ("01:35", "02:05"),
        ("02:11", "04:19"),
    ]
    today = [r for r in log.records if r.ts >= T0]
    assert [f"{r.ts:%H:%M} {r.summary}" for r in today] == [
        "00:07 大盘移动止损：BTC 从跌段低点反弹 1.1 ATR（门槛 1.0），暂停新空单，创新低才恢复",
        "00:11 大盘移动止损：BTC 创跌段新低，恢复新空单（锁了 4 分钟）",
        "00:38 大盘移动止损：BTC 从跌段低点反弹 1.3 ATR（门槛 1.0），暂停新空单，创新低才恢复",
        "00:42 大盘移动止损：BTC 创跌段新低，恢复新空单（锁了 4 分钟）",
        "00:54 大盘移动止损：BTC 从跌段低点反弹 1.2 ATR（门槛 1.0），暂停新空单，创新低才恢复",
        "01:31 大盘移动止损：BTC 创跌段新低，恢复新空单（锁了 37 分钟）",
        "01:37 大盘移动止损：BTC 从跌段低点反弹 1.0 ATR（门槛 1.0），暂停新空单，创新低才恢复",
        "01:57 大盘移动止损：BTC 创跌段新低，恢复新空单（锁了 20 分钟）",
        "02:14 大盘移动止损：BTC 从跌段低点反弹 1.1 ATR（门槛 1.0），暂停新空单，创新低才恢复",
        "03:34 大盘移动止损：BTC 开了新的跌段（z -1.10），恢复新空单（锁了 80 分钟）",
        "03:59 大盘移动止损：BTC 从跌段低点反弹 1.1 ATR（门槛 1.0），暂停新空单，创新低才恢复",
        "04:19 大盘移动止损：BTC 开了新的涨段（z +1.49），恢复新空单（锁了 20 分钟）",
    ]
    lock = today[2]
    assert lock.outcome is c.Outcome.INFO and lock.ts == datetime(2026, 10, 30, 0, 38, tzinfo=UTC)
    out = lock.output["market_trail"]
    assert out["ref"] == "btc" and out["direction"] == "short" and out["kind"] == "lock"
    assert today[3].output["market_trail"]["locked_min"] == 4


def test_market_trail_restart_continues_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _minutes("2026-10-30T01:10:00+00:00")
    cut = next(i for i, r in enumerate(rows) if r[0] == datetime(2026, 10, 30, 0, 40, tzinfo=UTC))
    monkeypatch.setattr(mt, "SAVE_EVERY_S", 10**9)
    clock_a = Clock(rows[0][0])
    a = MarketTrail(ON, tmp_path / "a", clock=clock_a, seed=False)
    _feed(a, clock_a, rows[: cut + 1])
    a.save()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / mt.STATE_FILE).write_bytes((tmp_path / "a" / mt.STATE_FILE).read_bytes())
    clock_b = Clock(clock_a.at)
    b = MarketTrail(ON, tmp_path / "b", clock=clock_b, seed=False)
    assert b.holds(SHORT, clock_b.at) == (
        "大盘移动止损：空单锁住中（BTC 反弹过 1.0 ATR 门槛，"
        "现在离低点 1.1 ATR，已锁 2 分钟；创新低才解锁）"
    )
    assert b.holds(LONG, clock_b.at) is None
    _feed(a, clock_a, rows[cut + 1 :])
    _feed(b, clock_b, rows[cut + 1 :])
    for name in mt.SERIES:
        assert b.state(name).to_json() == a.state(name).to_json()
    assert a.state("btc").last_t == int(datetime(2026, 10, 30, 1, 8, tzinfo=UTC).timestamp())


def test_market_trail_seeds_from_regime_checks_without_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _minutes("2026-10-30T00:40:00+00:00")
    folder = tmp_path / REGIME_CHECKS_DIR
    folder.mkdir()
    by_day: dict[str, list[str]] = {}
    for t, b, e in rows:
        row = {"sample_at": t.isoformat(), "btc_px": b, "prices": {ETH: e} if e else {}}
        by_day.setdefault(t.date().isoformat(), []).append(json.dumps(row))
        by_day[t.date().isoformat()].append(json.dumps({"sample_at": None, "prices": {}}))
    for day, lines in by_day.items():
        (folder / f"{day}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log = Log()
    end = datetime(2026, 10, 30, 0, 40, 30, tzinfo=UTC)
    seeded = MarketTrail(
        ON, tmp_path, recorder=RoleRecorder(c.Role.RISK_GUARD, log), clock=Clock(end)
    )
    assert log.records == []
    assert seeded.holds(SHORT, end) == (
        "大盘移动止损：空单锁住中（BTC 反弹过 1.0 ATR 门槛，"
        "现在离低点 1.1 ATR，已锁 2 分钟；创新低才解锁）"
    )
    monkeypatch.setattr(mt, "SAVE_EVERY_S", 10**9)
    clock = Clock(rows[0][0])
    live = MarketTrail(ON, tmp_path / "live", clock=clock, seed=False)
    _feed(live, clock, rows)
    for name in ("btc", "eth", "btc_eth"):
        assert seeded.state(name).to_json() == live.state(name).to_json()


# ---------------------------------------------------------------- 状态机（z、ATR 写死）
def _series(
    monkeypatch: pytest.MonkeyPatch, zs: list[float | None], atr: float = 0.25
) -> TrailSeries:
    s = TrailSeries(count=MIN_HISTORY + 1, atr=atr)
    it = iter(zs)
    monkeypatch.setattr(s, "_z", lambda: next(it))
    monkeypatch.setattr(s, "_bar", lambda t, x: None)
    return s


def _run(s: TrailSeries, xs: list[float], k: float = 1.0) -> list[list[str]]:
    return [[e["kind"] for e in s.step(60 * (i + 1), x, k)] for i, x in enumerate(xs)]


def test_market_trail_down_leg_locks_shorts_after_k_atr_bounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _series(monkeypatch, [-0.5, -1.2, -1.1, -0.4])
    assert _run(s, [0.0, -1.0, -0.875, -0.75]) == [[], ["leg"], [], ["lock"]]
    assert s.leg == Leg(-1, -1.0, 120, True, 240)
    assert s.locked == -1


def test_market_trail_up_leg_locks_longs(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [0.3, 1.5, 0.9, 0.9])
    assert _run(s, [0.0, 1.0, 1.5, 1.25]) == [[], ["leg"], [], ["lock"]]
    assert s.leg == Leg(1, 1.5, 180, True, 240)


def test_market_trail_k_scales_the_lock_distance(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [-1.2, -0.5, -0.5])
    assert _run(s, [-1.0, -0.75, -0.5], k=2.0) == [["leg"], [], ["lock"]]


def test_market_trail_new_extreme_unlocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [-1.2, -0.4, -0.4, -0.4, -0.9])
    assert _run(s, [-1.0, -0.5, -0.625, -0.75, -1.125]) == [
        ["leg"],
        ["lock"],
        [],
        [],
        ["unlock"],
    ]
    assert s.leg == Leg(-1, -1.125, 300, False, None)


def test_market_trail_touching_the_extreme_does_not_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [-1.2, -0.4, -0.4])
    assert _run(s, [-1.0, -0.5, -1.0]) == [["leg"], ["lock"], []]
    assert s.locked == -1


def test_market_trail_unlock_event_reports_minutes_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [-1.2, -0.4, -0.4, -0.9])
    s.step(60, -1.0, 1.0)
    s.step(120, -0.5, 1.0)
    s.step(180, -0.6, 1.0)
    (ev,) = s.step(420, -1.25, 1.0)
    assert ev["kind"] == "unlock" and ev["why"] == "extreme" and ev["locked_min"] == 5


def test_market_trail_opposite_leg_takes_over(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [-1.2, -0.4, 1.3, 0.5])
    assert _run(s, [-1.0, -0.75, -0.5, -0.75]) == [["leg"], ["lock"], ["unlock", "leg"], ["lock"]]
    assert s.leg == Leg(1, -0.5, 180, True, 240)


def test_market_trail_opposite_leg_while_unlocked_has_no_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _series(monkeypatch, [-1.2, 1.3])
    assert _run(s, [-1.0, -0.875]) == [["leg"], ["leg"]]
    assert s.leg == Leg(1, -0.875, 120)


def test_market_trail_z_back_inside_does_not_end_the_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [-1.2, -0.2, 0.4, 0.9])
    assert _run(s, [-1.0, -0.875, -0.875, -0.75]) == [["leg"], [], [], ["lock"]]
    assert s.leg is not None and s.leg.side == -1


def test_market_trail_same_side_cross_restarts_only_a_stopped_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _series(monkeypatch, [-1.2, -0.5, -1.3, -0.5, -0.5, -1.4])
    assert _run(s, [-1.0, -0.875, -0.9, -0.75, -0.7, -0.8]) == [
        ["leg"],
        [],
        [],  # 没止损：同方向再出 −1 不重开，极值还是 −1.0
        ["lock"],
        [],
        ["unlock", "leg"],  # 止损了：同方向再出 −1 开新段，极值从这一分钟算
    ]
    assert s.leg == Leg(-1, -0.8, 360)


def test_market_trail_no_z_no_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _series(monkeypatch, [None, None])
    assert _run(s, [0.0, -5.0]) == [[], []]
    assert s.leg is None


def test_market_trail_atr_uses_only_closed_15m_candles(monkeypatch: pytest.MonkeyPatch) -> None:
    s = TrailSeries()
    monkeypatch.setattr(s, "_z", lambda: None)
    base = int(T0.timestamp())
    first = [0.0, 0.0, 0.5, 0.0, 0.0, 0.0, -0.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.125]
    for i, x in enumerate(first[:-1], start=1):
        s.step(base + 60 * i, x, 1.0)
    assert s.atr is None  # 00:00 开的那根 15 分钟线 00:15 才收
    s.step(base + 900, first[-1], 1.0)
    assert s.atr == 0.75
    for i in range(16, 30):
        s.step(base + 60 * i, 1.0 if i != 20 else 9.0, 1.0)
    assert s.atr == 0.75  # 盘中的大波动不算，收盘才算
    s.step(base + 1800, 1.0, 1.0)
    assert s.atr == 0.75 + (9.0 - 0.125 - 0.75) / 14


def test_market_trail_disabled_until_24h_of_history() -> None:
    s = TrailSeries()
    for i in range(MIN_HISTORY):
        s.step(60 * (i + 1), 0.001 * ((i * 7) % 13), 1.0)
    assert s.z is None and not s.warm
    s.step(60 * (MIN_HISTORY + 1), 0.001 * 3, 1.0)
    assert s.warm and s.z is not None


# ---------------------------------------------------------------- 闸门（holds / 状态行）
def _lock(
    tr: MarketTrail, name: str, side: int, at: datetime, *, count: int = MIN_HISTORY + 1
) -> None:
    s = tr.state(name)
    s.count = count
    s.atr = 0.001
    s.xs.append(11.0011)
    t = int(at.timestamp())
    s.leg = Leg(side, 11.0 if side < 0 else 11.0022, t, True, t)


def test_market_trail_holds_only_the_locked_side_of_the_chosen_ref(tmp_path: Path) -> None:
    fast = ON.model_copy()
    tr = MarketTrail(fast, tmp_path, seed=False)
    _lock(tr, "btc", -1, T0)
    _lock(tr, "eth", 1, T0)
    later = T0 + timedelta(minutes=12)
    assert tr.holds(SHORT, later) == (
        "大盘移动止损：空单锁住中（BTC 反弹过 1.0 ATR 门槛，"
        "现在离低点 1.1 ATR，已锁 12 分钟；创新低才解锁）"
    )
    assert tr.holds(LONG, later) is None
    assert tr.status_line(later) == (
        "大盘闸门：空单暂停\n"
        "大盘移动止损：空单锁住中（BTC 现在离低点 1.1 ATR · 门槛 1.0 · 已锁 12 分钟）"
    )
    patch_inplace(fast, fast.model_copy(update={"signal_market_trail_ref": "eth"}), HOT_FAST)
    assert tr.holds(SHORT, later) is None
    assert tr.holds(LONG, later) == (
        "大盘移动止损：多单锁住中（ETH 回落过 1.0 ATR 门槛，"
        "现在离高点 1.1 ATR，已锁 12 分钟；创新高才解锁）"
    )
    patch_inplace(fast, fast.model_copy(update={"signal_market_trail_on": False}), HOT_FAST)
    assert tr.holds(LONG, later) is None


def test_market_trail_short_history_never_holds(tmp_path: Path) -> None:
    tr = MarketTrail(ON, tmp_path, seed=False)
    _lock(tr, "btc", -1, T0, count=600)
    assert tr.holds(SHORT, T0) is None
    assert tr.status_line(T0) == (
        "大盘闸门：不拦\n大盘移动止损（BTC）：历史不足 24 小时（已有 10 小时），暂不拦"
    )


def test_market_trail_status_when_leg_is_open(tmp_path: Path) -> None:
    tr = MarketTrail(ON, tmp_path, seed=False)
    _lock(tr, "btc", -1, T0)
    leg = tr.state("btc").leg
    assert leg is not None
    leg.stopped, leg.stopped_at = False, None
    head = "大盘闸门：不拦\n"
    assert tr.status_line(T0) == f"{head}大盘移动止损：BTC 跌段，空单放行（离低点 1.1 / 1 ATR）"
    tr.state("btc").leg = None
    assert tr.status_line(T0) == f"{head}大盘移动止损（BTC）：还没有跌段 / 涨段，不拦"
    tr.state("btc").last_t = int(T0.timestamp())
    assert tr.status_line(T0 + timedelta(minutes=6)) == head + (
        "大盘移动止损（BTC）：还没有跌段 / 涨段，不拦；数据停在 07:00，状态不变"
    )


# ---------------------------------------------------------------- 收数据
def _kline(open_at: datetime, close: float, symbol: str = BTC) -> c.Kline:
    return c.Kline(
        symbol=symbol,
        interval=c.Interval.M1,
        open_time=open_at,
        close_time=open_at + timedelta(seconds=59.999),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
    )


def _warm_clock(at: datetime) -> Clock:
    return Clock(at - timedelta(minutes=20))


def test_market_trail_two_coin_ref_waits_then_fills_the_missing_coin(tmp_path: Path) -> None:
    clock = _warm_clock(T0)
    tr = MarketTrail(ON, tmp_path, clock=clock, seed=False)
    base = int(T0.timestamp())
    for i in range(1, 4):
        tr.observe_close(BTC, base + 60 * i, 100.0 + i)
        tr.observe_close(ETH, base + 60 * i, 10.0)
    tr.observe_close(BTC, base + 240, 104.0)
    tr.tick(datetime.fromtimestamp(base + 245, UTC))
    assert tr.state("btc").last_t == base + 240
    assert tr.state("btc_eth").last_t == base + 180  # 等 ETH 这一分钟
    tr.tick(datetime.fromtimestamp(base + 240 + mt.GRACE_S, UTC))
    s = tr.state("btc_eth")
    assert s.last_t == base + 240 and s.count == 4
    assert s.xs[-1] == pytest.approx((4.6443908991413725 + 2.302585092994046) / 2)
    assert tr.state("eth").last_t == base + 180  # 后面还没有 ETH，不往前补


def test_market_trail_no_fill_until_the_btc_backfill_arrives(tmp_path: Path) -> None:
    base = int(T0.timestamp())
    clock = Clock(datetime.fromtimestamp(base + 245, UTC))
    tr = MarketTrail(ON, tmp_path, clock=clock, seed=False)
    tr.observe_close(BTC, base + 60, 100.0)
    tr.observe_close(BTC, base + 180, 101.0)
    tr.tick(datetime.fromtimestamp(base + 400, UTC))
    assert tr.state("btc").last_t == base + 60  # 回填 K 线可能还在路上
    tr.release_fill()
    tr.tick(datetime.fromtimestamp(base + 401, UTC))
    assert tr.state("btc").last_t == base + 180 and tr.state("btc").xs[-2] == pytest.approx(
        4.605170186
    )


def test_market_trail_fills_after_the_wait_without_a_backfill(tmp_path: Path) -> None:
    base = int(T0.timestamp())
    tr = MarketTrail(ON, tmp_path, clock=Clock(datetime.fromtimestamp(base, UTC)), seed=False)
    tr.observe_close(BTC, base + 60, 100.0)
    tr.observe_close(BTC, base + 180, 101.0)
    tr.tick(datetime.fromtimestamp(base + 599, UTC))
    assert tr.state("btc").last_t == base + 60
    tr.tick(datetime.fromtimestamp(base + 600, UTC))
    assert tr.state("btc").last_t == base + 180 and tr.state("btc").xs[-2] == pytest.approx(
        4.605170186
    )


def test_market_trail_no_long_gap_reset_before_the_backfill(tmp_path: Path) -> None:
    clock = Clock(T0)
    tr = MarketTrail(ON, tmp_path, clock=clock, seed=False)
    base = int(T0.timestamp())
    s = tr.state("btc")
    s.last_t, s.count, s.fill = base - 8 * 3600, 2000, {BTC: 4.6}
    s.xs.append(4.6)
    tr.observe_close(BTC, base + 60, 100.0)  # 大盘翻转的取样先到
    tr.tick(T0 + timedelta(seconds=65))
    assert tr.state("btc").count == 2000
    for m in range(1, 8 * 60 + 1):  # 回填把停机那段补上
        tr.observe_close(BTC, base - 8 * 3600 + 60 * m, 100.0)
    tr.release_fill()
    tr.tick(T0 + timedelta(seconds=66))
    assert tr.state("btc").count == 2481 and tr.state("btc").last_t == base + 60


def test_market_trail_long_gap_resets_the_series(tmp_path: Path) -> None:
    clock = _warm_clock(T0)
    tr = MarketTrail(ON, tmp_path, clock=clock, seed=False)
    base = int(T0.timestamp())
    tr.observe_close(BTC, base + 60, 100.0)
    tr.tick(datetime.fromtimestamp(base + 65, UTC))
    later = base + 60 + (mt.MAX_FILL_MIN + 1) * 60
    tr.observe_close(BTC, later, 90.0)
    tr.tick(datetime.fromtimestamp(later + 5, UTC))
    s = tr.state("btc")
    assert s.count == 1 and s.last_t == later
    both = MarketTrail(ON, tmp_path / "y", clock=clock, seed=False)
    both.observe_close(BTC, base + 60, 100.0)
    both.observe_close(BTC, base + 120, 100.0)
    both.observe_close(BTC, later + 60, 90.0)  # 一次 tick 里先算完整的分钟、再遇到长缺口
    both.tick(datetime.fromtimestamp(later + 65, UTC))
    assert both.state("btc").count == 1 and both.state("btc").last_t == later + 60
    short = MarketTrail(ON, tmp_path / "x", clock=clock, seed=False)
    short.observe_close(BTC, base + 60, 100.0)
    short.tick(datetime.fromtimestamp(base + 65, UTC))
    short.observe_close(BTC, base + 60 + mt.MAX_FILL_MIN * 60, 90.0)
    short.tick(datetime.fromtimestamp(base + 160 + mt.MAX_FILL_MIN * 60, UTC))
    assert short.state("btc").count == 361  # 6 小时以内按上一分钟补


def test_market_trail_alt_basket_from_regime_samples(tmp_path: Path) -> None:
    clock = _warm_clock(T0)
    tr = MarketTrail(ON, tmp_path, clock=clock, seed=False)
    t1, t2 = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    t3 = T0 + timedelta(minutes=3)
    first = {"SOLUSDT": 200.0, "SUIUSDT": 4.0, "BNBUSDT": 600.0, "WIFUSDT": 1.0, ETH: 10.0}
    tr.observe_sample(t1, 100.0, first)
    second = {"SOLUSDT": 202.0, "SUIUSDT": 4.0, "BNBUSDT": 630.0, "WIFUSDT": 2.0, "DOGEUSDT": 1.0}
    tr.observe_sample(t2, 101.0, second)
    tr.observe_sample(t3, 101.0, {"SOLUSDT": 202.0, "SUIUSDT": 8.0})
    tr.tick(t3 + timedelta(seconds=5))
    alts = tr.state("alts")
    assert alts.count == 3
    # SOL +1%、SUI 0；BNB 不算、WIF 翻倍当坏数据；DOGE 第一分钟没有前值
    assert alts.xs[1] == pytest.approx(0.009950330853168083 / 2)
    # SUI 在第二分钟还在、第三分钟翻倍当坏数据，SOL 0
    assert alts.xs[2] == alts.xs[1]
    assert set(alts.fill) == {"SOLUSDT", "SUIUSDT"}  # 掉出池子的币不留旧价
    assert tr.state("btc").count == 3 and tr.state("eth").count == 1


def test_market_trail_ws_kline_minute_is_the_close_time(tmp_path: Path) -> None:
    tr = MarketTrail(ON, tmp_path, clock=_warm_clock(T0), seed=False)
    k = _kline(T0 + timedelta(minutes=37), 108000.5)
    tr.observe_kline(k)
    tr.observe_kline(k.model_copy(update={"symbol": "SOLUSDT"}))
    tr.tick(T0 + timedelta(minutes=38, seconds=1))
    assert tr.state("btc").last_t == int((T0 + timedelta(minutes=38)).timestamp())


def test_market_trail_shadow_off_writes_nothing(tmp_path: Path) -> None:
    tr = MarketTrail(ON, tmp_path, clock=_warm_clock(T0), seed=False)
    tr.observe_close(BTC, int(T0.timestamp()) + 60, 100.0)
    tr.tick(T0 + timedelta(minutes=1, seconds=5))
    assert tr.state("btc").count == 1
    assert not (tmp_path / SHADOW_DIR).exists()


def test_market_trail_hot_keys_and_choices() -> None:
    assert {
        "signal_market_trail_on",
        "signal_market_trail_k",
        "signal_market_trail_ref",
        "signal_market_trail_shadow",
    } <= HOT_FAST
    off = c.FastLoopConfig.model_fields
    assert off["signal_market_trail_on"].default is False
    assert off["signal_market_trail_shadow"].default is False
    assert off["signal_market_trail_ref"].default == "btc"
    with pytest.raises(ValueError, match="signal_market_trail_ref"):
        h.FAST.model_validate({**h.FAST.model_dump(), "signal_market_trail_ref": "sol"})


# ---------------------------------------------------------------- 主循环
async def test_runner_rejects_locked_side_everywhere(tmp_path: Path) -> None:
    app = _app(tmp_path, T0)
    patch_inplace(app.app_cfg.fast_loop, ON, HOT_FAST)
    log = Log()
    app.recorders[c.Role.RISK_GUARD] = RoleRecorder(c.Role.RISK_GUARD, log)
    app.market_trail = MarketTrail(app.app_cfg.fast_loop, tmp_path, seed=False)
    _lock(app.market_trail, "btc", -1, T0 - timedelta(minutes=3))
    card = h.card(T0)

    class Cards:
        def current(self, now: datetime) -> c.BattleCard:
            return card

    class Gate:
        async def evaluate(self, signal: c.CandidateSignal, cd: object, now: datetime) -> None:
            raise AssertionError("锁住方向的信号不该问 Jev")

    app.m = dataclasses.replace(app.m, cards=Cards(), gate=Gate())
    r = Runner(app)
    why = (
        "大盘移动止损：空单锁住中（BTC 反弹过 1.0 ATR 门槛，"
        "现在离低点 1.1 ATR，已锁 3 分钟；创新低才解锁）"
    )
    await r.handle_signal(h.signal(T0, side=SHORT))
    rej = log.records[-1]
    assert rej.outcome is c.Outcome.REJECT and rej.summary == f"BTCUSDT {why}"
    market, cs = card.current_market, card.for_symbol("BTCUSDT")
    assert cs is not None and cs.issued_at is not None
    expires = card.symbol_expires_at("BTCUSDT")
    assert expires is not None
    assert rej.output["detail"] == {
        "by": "market_trail",
        "gates": ["trail"],
        "card_issued_at": cs.issued_at.isoformat(),
        "card_expires_at": expires.isoformat(),
        "regime_direction": market.regime_direction if market else None,
    }
    assert not r._trail_blocked(h.signal(T0, side=LONG), card, T0)
    intent = c.OrderIntent.model_validate(
        {**load_example("order_intent"), "symbol": "BTCUSDT", "side": "short"}
    )
    assert why in await r._may_chase(intent)


async def test_runner_feeds_klines_and_regime_samples(tmp_path: Path) -> None:
    app = _app(tmp_path, T0)
    app.market_trail = MarketTrail(app.app_cfg.fast_loop, tmp_path, clock=_warm_clock(T0))
    r = Runner(app)
    k = _kline(T0 - timedelta(minutes=1), 108000.5)
    await r.on_event(k)
    assert app.market_trail.state("btc").last_t == int(T0.timestamp())


def test_heartbeat_puts_the_lock_on_the_risk_guard_line(tmp_path: Path) -> None:
    app = _app(tmp_path, T0)
    patch_inplace(app.app_cfg.fast_loop, ON, HOT_FAST)
    _lock(app.market_trail, "btc", -1, T0 - timedelta(minutes=12))
    app.heartbeat_all()
    st = app.log.read_status()[c.Role.RISK_GUARD.value]
    assert st.headline == (
        "大盘闸门：空单暂停\n"
        "大盘移动止损：空单锁住中（BTC 现在离低点 1.1 ATR · 门槛 1.0 · 已锁 12 分钟）"
    )


def test_runner_backfill_of_btc_releases_the_fill(tmp_path: Path) -> None:
    tr = MarketTrail(ON, tmp_path, clock=Clock(T0), seed=False)
    base = int(T0.timestamp())
    tr.observe_close(BTC, base + 60, 100.0)
    tr.observe_close(BTC, base + 180, 101.0)
    Runner._trail_backfilled(tr, ETH, [_kline(T0, 10.0, ETH)])
    tr.tick(T0 + timedelta(seconds=300))
    assert tr.state("btc").last_t == base + 60
    Runner._trail_backfilled(tr, BTC, [])  # 回填失败也算回填过
    tr.tick(T0 + timedelta(seconds=301))
    assert tr.state("btc").last_t == base + 180


# ---------------------------------------------------------------- 大盘均线 + 极值作废
GATE = ON.model_copy(
    update={
        "signal_market_trail_k": 0.5,
        "signal_market_trail_max_extreme_age_min": 120,
        "signal_market_ma_on": True,
    }
)
BKK = "Asia/Bangkok"


def test_market_trail_ema_value_is_on_closes_seeded_with_the_first_minute() -> None:
    xs = [math.log(p) for p in (100.0, 130.0, 70.0)]
    assert ema_value(xs[:2], 3) is None
    # alpha = 2 / 4：100 → 115 → 92.5（对 ln 价做 EMA 再还原是 90.45…）
    assert ema_value(xs, 3) == pytest.approx(92.5)


def _ema_series(*, last_t: int, atr: float | None = 0.01, filled: int | None = None) -> TrailSeries:
    s = TrailSeries()
    s.xs.extend([math.log(100.0)] * 2 + [math.log(110.0)])
    s.last_t, s.atr, s.filled_t = last_t, atr, filled
    return s


@pytest.mark.parametrize(
    ("last_age", "atr", "filled_age", "ema", "state", "side"),
    [
        (60, 0.01, None, 105.0, "block", -1),
        (60, 0.01, None, 115.0, "block", 1),
        (60, 0.01, None, 110.0, "equal", 0),
        (60, 0.01, None, None, "warmup", 0),
        (60, None, None, 105.0, "warmup", 0),
        (301, 0.01, None, 105.0, "stale", 0),
        (300, 0.01, None, 105.0, "block", -1),
        (60, 0.01, 60 * 29, 105.0, "filled", 0),
        (60, 0.01, 60 * 30, 105.0, "block", -1),
    ],
)
def test_market_trail_read_ema_when_it_blocks(
    last_age: int, atr: float | None, filled_age: int | None, ema: float | None,
    state: str, side: int,
) -> None:  # fmt: skip
    now = 1_000_000 * 60
    last = now - last_age
    filled = None if filled_age is None else last - filled_age
    r = read_ema(_ema_series(last_t=last, atr=atr, filled=filled), 30, now, ema)
    assert (r.state, r.side) == (state, side)


def _gate(tmp_path: Path, fast: c.FastLoopConfig, *, last: datetime, price: float) -> MarketTrail:
    """收盘 100 走 40 分钟后最后一分钟到 ``price``；ATR 固定 0.01。"""
    tr = MarketTrail(fast, tmp_path, seed=False)
    s = tr.state("btc")
    s.xs.extend([math.log(100.0)] * 40 + [math.log(price)])
    s.last_t, s.atr, s.count = int(last.timestamp()), 0.01, 41
    return tr


def test_market_trail_ema_alone_blocks_with_its_own_text(tmp_path: Path) -> None:
    tr = _gate(tmp_path, GATE, last=T0, price=100.3)
    assert tr.blocks(SHORT, T0) == (
        "大盘均线：BTC 在 30 分钟均线上方 0.3 ATR，暂停新空单",
        ["ema"],
    )
    assert tr.blocks(LONG, T0) == (None, [])
    assert tr.status_line(T0).split("\n")[:2] == [
        "大盘闸门：空单暂停",
        "大盘均线：BTC 在 30 分钟均线上方 0.3 ATR，暂停新空单",
    ]
    down = _gate(tmp_path / "d", GATE, last=T0, price=99.7)
    assert down.holds(LONG, T0) == "大盘均线：BTC 在 30 分钟均线下方 0.3 ATR，暂停新多单"


def test_market_trail_ema_blocks_while_the_trail_is_still_warming_up(tmp_path: Path) -> None:
    tr = _gate(tmp_path, GATE, last=T0, price=100.3)
    assert not tr.state("btc").warm
    assert tr.holds(SHORT, T0) is not None


def test_market_trail_ema_stale_does_not_block_but_a_lock_stays(tmp_path: Path) -> None:
    tr = _gate(tmp_path, GATE, last=T0, price=100.3)
    s = tr.state("btc")
    t = int(T0.timestamp())
    s.count, s.leg = MIN_HISTORY + 1, Leg(-1, math.log(99.0), t, True, t)
    later = T0 + timedelta(minutes=6)
    assert tr.blocks(SHORT, later)[1] == ["trail"]
    assert "均线数据停在 07:00，暂不拦" in tr.status_line(later)


def test_market_trail_both_gates_keep_the_trail_text_and_report_both(tmp_path: Path) -> None:
    tr = _gate(tmp_path, GATE, last=T0, price=100.3)
    s = tr.state("btc")
    t = int(T0.timestamp())
    s.count, s.leg = MIN_HISTORY + 1, Leg(-1, math.log(99.0), t, True, t)
    why, gates = tr.blocks(SHORT, T0)
    assert gates == ["ema", "trail"]
    assert why is not None
    assert why.startswith("大盘移动止损：空单锁住中") and why.endswith(
        "创新低或 09:00 作废才解锁）"
    )


def test_market_trail_ema_and_lock_on_opposite_sides_say_both_paused(tmp_path: Path) -> None:
    tr = _gate(tmp_path, GATE, last=T0, price=100.3)
    s = tr.state("btc")
    t = int(T0.timestamp())
    s.count, s.leg = MIN_HISTORY + 1, Leg(1, math.log(101.0), t, True, t)
    assert tr.status_line(T0).split("\n")[0] == "大盘闸门：多空都暂停"


@pytest.mark.parametrize(
    ("update", "gates"),
    [
        ({"signal_market_trail_on": False}, []),
        ({"signal_market_ma_on": False}, []),
    ],
)
def test_market_trail_switches_turn_the_ema_off(
    tmp_path: Path, update: dict[str, bool], gates: list[str]
) -> None:
    tr = _gate(tmp_path, GATE.model_copy(update=update), last=T0, price=100.3)
    assert tr.blocks(SHORT, T0) == (None, gates)


def test_market_trail_ema_needs_n_minutes(tmp_path: Path) -> None:
    tr = MarketTrail(GATE, tmp_path, seed=False)
    s = tr.state("btc")
    s.xs.extend([math.log(100.0)] * 28 + [math.log(101.0)])
    s.last_t, s.atr = int(T0.timestamp()), 0.01
    assert tr.holds(SHORT, T0) is None
    assert "大盘均线：数据不满 30 分钟，暂不拦" in tr.status_line(T0)


def _locked_leg(s: TrailSeries, t: int, *, stopped: bool = True) -> Leg:
    s.xs.append(4.0)
    s.atr = 0.01
    leg = Leg(-1, 3.99, t, stopped, t + 600 if stopped else None)
    s.leg = leg
    return leg


@pytest.mark.parametrize(
    ("age_min", "stopped", "expired", "kinds"),
    [
        (119, True, False, []),
        (120, True, True, [("unlock", "expire")]),
        (120, False, True, []),
    ],
)
def test_market_trail_step_expires_a_stale_extreme(
    age_min: int, stopped: bool, expired: bool, kinds: list[tuple[str, str]]
) -> None:
    s = TrailSeries()
    t0 = 1_000_000 * 60
    leg = _locked_leg(s, t0, stopped=stopped)
    t = t0 + age_min * 60
    events = s.step(t, 3.995, 0.5, 120)
    assert [(e["kind"], e.get("why")) for e in events] == kinds
    assert (leg.extreme, leg.extreme_at) == ((3.995, t) if expired else (3.99, t0))
    assert leg.stopped is (stopped and not expired)


def test_market_trail_step_expiry_runs_before_the_new_extreme_check() -> None:
    s = TrailSeries()
    t0 = 1_000_000 * 60
    _locked_leg(s, t0)
    events = s.step(t0 + 7200, 3.98, 0.5, 120)
    assert [e.get("why") for e in events] == ["expire"]


def test_market_trail_expired_extreme_can_lock_again() -> None:
    s = TrailSeries()
    t0 = 1_000_000 * 60
    leg = _locked_leg(s, t0)
    s.step(t0 + 7200, 3.995, 0.5, 120)
    events = s.step(t0 + 7260, 4.1, 0.5, 120)
    assert [e["kind"] for e in events] == ["lock"]
    assert leg.stopped and leg.stopped_at == t0 + 7260


def test_market_trail_zero_age_never_expires() -> None:
    s = TrailSeries()
    t0 = 1_000_000 * 60
    leg = _locked_leg(s, t0)
    assert s.step(t0 + 86_400, 3.995, 0.5, 0) == []
    assert leg.stopped


def _ref_gate(ts: list[int], xs: list[float], k: float, w: int, n: int) -> list[tuple[bool, bool]]:
    """回放口径（gate.py 变体 f + ma_sim.py 的 EMA）：每分钟（拦多，拦空）。"""
    out: list[tuple[bool, bool]] = []
    bar: list[float] | None = None
    pc = atr = m = None
    zone = 0
    leg: list[Any] | None = None
    alpha = 2.0 / (n + 1)
    for i, (t, x) in enumerate(zip(ts, xs, strict=True)):
        start = (t - 60) // 900 * 900
        if bar is None:
            bar = [start, x, x, x]
        else:
            bar[1], bar[2], bar[3] = max(bar[1], x), min(bar[2], x), x
        if t % 900 == 0:
            tr = bar[1] - bar[2] if pc is None else max(bar[1], pc) - min(bar[2], pc)
            atr = tr if atr is None else atr + (tr - atr) / 14
            pc, bar = bar[3], None
        z = None
        if i + 1 > 1440:
            win = [
                xs[j] - xs[j - 240]
                for j in range(i - 15, max(i - 4560 + 240, i - 4320, 240) - 1, -15)
            ]
            mean = sum(win) / len(win)
            sd = math.sqrt(sum((v - mean) ** 2 for v in win) / len(win))
            z = (x - xs[i - 240] - mean) / sd if sd > 0 else None
        if z is not None:
            zz = 1 if z > 1 else -1 if z < -1 else 0
            if zz and zz != zone and (leg is None or leg[0] != zz or leg[3]):
                leg = [zz, x, i, False]
            zone = zz
        lock = 0
        if leg is not None:
            s = leg[0]
            if i - leg[2] >= w:
                leg[1], leg[2], leg[3] = x, i, False
            if s * (x - leg[1]) > 0:
                leg[1], leg[2], leg[3] = x, i, False
            elif not leg[3] and atr and s * (leg[1] - x) >= k * atr:
                leg[3] = True
            lock = s if leg[3] else 0
        c_ = math.exp(x)
        m = c_ if m is None else m + alpha * (c_ - m)
        ema_side = 0
        if i >= n - 1 and atr:
            d = x - math.log(m)
            ema_side = 1 if d < 0 else -1 if d > 0 else 0
        out.append((1 in (lock, ema_side), -1 in (lock, ema_side)))
    return out


@pytest.mark.parametrize("seed", [7, 23])
def test_market_trail_gate_matches_the_replay_rule_minute_by_minute(
    tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    ts, xs = [], []
    base = int(T0.timestamp())
    x = math.log(70000.0)
    for i in range(3600):
        drift = 0.0004 * math.sin(i / 170.0)
        x += drift + rng.gauss(0.0, 0.0006)
        ts.append(base + 60 * (i + 1))
        xs.append(x)
    want = _ref_gate(ts, xs, 0.5, 120, 30)
    now = [T0]
    tr = MarketTrail(GATE.model_copy(), tmp_path, clock=lambda: now[0], seed=False)
    tr.release_fill()
    got, locks = [], 0
    for t, x in zip(ts, xs, strict=True):
        now[0] = datetime.fromtimestamp(t + 1, UTC)
        tr.observe_close(BTC, t, math.exp(x))
        tr.tick(now[0])
        locks += len(tr.take_fresh_locks())
        got.append((tr.holds(LONG, now[0]) is not None, tr.holds(SHORT, now[0]) is not None))
    assert got == want
    assert locks > 5 and sum(a and b for a, b in got) > 100  # 序列确实走到了锁和两边都拦


def _fresh(tmp_path: Path, at: datetime) -> tuple[MarketTrail, Clock]:
    clock = Clock(at)
    tr = MarketTrail(ON.model_copy(), tmp_path, clock=clock, seed=False)
    tr.release_fill()
    s = tr.state("btc")
    s.xs.extend([math.log(100.0)] * 5)
    s.count, s.atr = MIN_HISTORY + 1, 0.001
    t = int(at.timestamp())
    s.last_t = t
    s.leg = Leg(-1, math.log(100.0), t)
    return tr, clock


def _bounce(tr: MarketTrail, clock: Clock, at: datetime, px: float = 100.2) -> None:
    tr.observe_close(BTC, int(at.timestamp()), px)
    tr.tick(clock())


def test_market_trail_fresh_lock_is_handed_over_once_and_survives_restart(tmp_path: Path) -> None:
    at = T0 + timedelta(minutes=1)
    tr, clock = _fresh(tmp_path, T0)
    clock.at = at
    _bounce(tr, clock, at)
    assert [(e["ref"], e["side"]) for e in tr.take_fresh_locks()] == [("btc", -1)]
    assert tr.take_fresh_locks() == []
    raw = json.loads((tmp_path / mt.STATE_FILE).read_text(encoding="utf-8"))
    assert raw["notified"] == [["btc", int(at.timestamp())]]
    assert set(raw["series"]["btc"]["leg"]) == set(Leg.__dataclass_fields__)
    again = MarketTrail(ON.model_copy(), tmp_path, clock=clock, seed=False)
    again._fresh.append({"ref": "btc", "side": -1, "t": int(at.timestamp())})
    again._settle_locks(at.timestamp())
    assert again.take_fresh_locks() == []


def test_market_trail_old_lock_recomputed_late_is_not_fresh(tmp_path: Path) -> None:
    tr, clock = _fresh(tmp_path, T0)
    at = T0 + timedelta(minutes=1)
    clock.at = at + timedelta(seconds=121)
    _bounce(tr, clock, at)
    assert tr.state("btc").locked == -1
    assert tr.take_fresh_locks() == []


def test_market_trail_lock_undone_in_the_same_advance_is_not_fresh(tmp_path: Path) -> None:
    tr, clock = _fresh(tmp_path, T0)
    at = T0 + timedelta(minutes=2)
    clock.at = at
    tr.observe_close(BTC, int((T0 + timedelta(minutes=1)).timestamp()), 100.2)
    tr.observe_close(BTC, int(at.timestamp()), 99.0)
    tr.tick(clock())
    assert tr.state("btc").locked is None
    assert tr.take_fresh_locks() == []


def test_market_trail_old_state_file_without_new_fields_loads(tmp_path: Path) -> None:
    tr, clock = _fresh(tmp_path, T0)
    tr.save()
    path = tmp_path / mt.STATE_FILE
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["notified"], raw["today"]
    for d in raw["series"].values():
        d.pop("filled_t")
    path.write_text(json.dumps(raw), encoding="utf-8")
    back = MarketTrail(ON.model_copy(), tmp_path, clock=clock, seed=False)
    assert back.state("btc").filled_t is None and back.state("btc").count == MIN_HISTORY + 1


def test_market_trail_ema_after_restart_equals_the_running_one(tmp_path: Path) -> None:
    tr = _gate(tmp_path, GATE.model_copy(), last=T0, price=100.3)
    before = tr.blocks(SHORT, T0)
    tr.save()
    back = MarketTrail(GATE.model_copy(), tmp_path, clock=Clock(T0), seed=False)
    assert back.blocks(SHORT, T0) == before


def test_market_trail_filled_minute_is_marked_and_persisted(tmp_path: Path) -> None:
    clock = Clock(T0)
    tr = MarketTrail(GATE.model_copy(), tmp_path, clock=clock, seed=False)
    tr.release_fill()
    base = int(T0.timestamp())
    tr.observe_close(BTC, base + 60, 100.0)
    tr.observe_close(BTC, base + 180, 101.0)
    clock.at = T0 + timedelta(minutes=5)
    tr.tick(clock.at)
    assert tr.state("btc").filled_t == base + 120
    tr.save()
    back = MarketTrail(GATE.model_copy(), tmp_path, clock=clock, seed=False)
    assert back.state("btc").filled_t == base + 120


def test_market_trail_shadow_row_carries_the_ema_state(tmp_path: Path) -> None:
    fast = GATE.model_copy(update={"signal_market_trail_shadow": True})
    tr = _gate(tmp_path, fast, last=T0, price=100.3)
    at = T0 + timedelta(minutes=1)
    tr._series["btc"].fill[BTC] = math.log(100.3)
    tr.observe_close(BTC, int(at.timestamp()), 100.3)
    tr.tick(at + timedelta(seconds=1))
    path = tmp_path / SHADOW_DIR / f"{at.date().isoformat()}.jsonl"
    row = next(r for r in map(json.loads, path.read_text().splitlines()) if r["ref"] == "btc")
    assert (row["ma_block"], row["both"]) == ("short", False)
    assert row["ma_dist_atr"] > 0 and row["ema"] > 100.0


def test_market_trail_expire_record_has_its_own_text(tmp_path: Path) -> None:
    log = Log()
    fast = ON.model_copy(update={"signal_market_trail_max_extreme_age_min": 120})
    clock = Clock(T0)
    tr = MarketTrail(
        fast, tmp_path, recorder=RoleRecorder(c.Role.RISK_GUARD, log), clock=clock, seed=False
    )
    tr.release_fill()
    s = tr.state("btc")
    s.xs.extend([math.log(100.0)] * 5)
    s.count, s.atr = MIN_HISTORY + 1, 0.001
    t = int(T0.timestamp())
    s.last_t = t + 7140
    s.leg = Leg(-1, math.log(99.0), t, True, t + 600)
    at = T0 + timedelta(minutes=120)
    clock.at = at
    tr.observe_close(BTC, int(at.timestamp()), 100.0)
    tr.tick(at)
    rec = log.records[-1]
    assert rec.summary == (
        "大盘移动止损：BTC 跌段低点 120 分钟没刷新，作废、从现价重算，恢复新空单（锁了 110 分钟）"
    )
    assert rec.output["market_trail"]["why"] == "expire"


class _Desk:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def request_run(self, reason: str = "regime_flip") -> None:
        self.asked.append(reason)

    def due(self, now: datetime) -> None:
        return None


def _lock_event(side: int) -> dict[str, Any]:
    return {"ref": "btc", "side": side, "t": int(T0.timestamp())}


@pytest.mark.parametrize(("notify", "asked"), [(True, ["market_trail"]), (False, [])])
def test_runner_fresh_lock_wakes_the_desk(tmp_path: Path, notify: bool, asked: list[str]) -> None:
    app = _app(tmp_path, T0)
    patch_inplace(
        app.app_cfg.fast_loop,
        ON.model_copy(update={"signal_market_trail_notify": notify}),
        HOT_FAST,
    )
    app.desk = _Desk()
    Runner(app)._trail_locked(_lock_event(-1))
    assert app.desk.asked == asked


class _LockEx:
    """只有锁利这几个方法的执行器。"""

    def __init__(self, hit: dict[str, Any] | None) -> None:
        self.hit, self.locked, self.seen = hit, [], []  # type: ignore[var-annotated]
        self.mark: float | None = None
        self.gap = 0.0
        self.result: dict[str, Any] = {"action": "locked"}

    def note_profit_reached(
        self, position_id: str, price: float, *, trigger_r: float, lock_r: float, source: str
    ) -> dict[str, Any] | None:
        self.seen.append((position_id, price, trigger_r, lock_r, source))
        return self.hit

    def set_profit_lock_gap(self, gap_bps: float) -> None:
        self.gap = gap_bps

    def last_mark(self, position_id: str) -> float | None:
        return self.mark

    async def lock_profit_stop(self, position_id: str) -> dict[str, Any]:
        self.locked.append(position_id)
        return self.result


def _lock_runner(tmp_path: Path, ex: _LockEx, **fast: Any) -> tuple[Runner, Log]:
    app = _app(tmp_path, T0)
    patch_inplace(app.app_cfg.fast_loop, h.FAST.model_copy(update=fast), HOT_FAST)
    log = Log()
    app.recorders[c.Role.KEEPER] = RoleRecorder(c.Role.KEEPER, log)
    app.m = dataclasses.replace(app.m, executor=ex)
    return Runner(app), log


def _held(side: str = "long") -> c.PositionRecord:
    data = load_example("position_record")
    data.update(position_id="pos-1", symbol="BTCUSDT", side=side, status="open")
    return c.PositionRecord.model_validate(data)


async def test_runner_profit_lock_first_cross_records_and_spawns_once(tmp_path: Path) -> None:
    ex = _LockEx({"target": 100.25, "reached_r": 0.31, "price": 100.5, "source": "mainnet"})
    r, log = _lock_runner(tmp_path, ex)
    r._profit_lock_cross(_held(), 100.5, "mainnet")
    assert ex.seen == [("pos-1", 100.5, 0.3, 0.1, "mainnet")]
    assert log.records[-1].output["profit_lock"]["target"] == 100.25
    assert log.records[-1].summary == (
        "BTCUSDT 扣费后赚到 0.31R，锁利止损定在 100.25（约 +0.1R，未含滑点）"
    )
    r._spawn_profit_lock("pos-1")  # 第一次还在跑：不重复派
    assert r._lock_queued == {"pos-1"}
    await asyncio.gather(*r._tasks)
    assert ex.locked == ["pos-1"] and r._lock_queued == set()


def test_runner_profit_lock_off_switch_stops_new_triggers(tmp_path: Path) -> None:
    ex = _LockEx({"target": 1.0, "reached_r": 1.0, "price": 1.0, "source": "mainnet"})
    r, log = _lock_runner(tmp_path, ex, profit_lock_on=False)
    r._profit_lock_cross(_held(), 100.5, "mainnet")
    assert ex.seen == [] and log.records == []


async def test_runner_profit_lock_tick_only_judges_and_sets_the_gap(tmp_path: Path) -> None:
    ex = _LockEx(None)
    ex.mark = 100.5
    r, _ = _lock_runner(tmp_path, ex, profit_lock_mark_gap_bps=8.0)
    await r.profit_lock_tick()
    await asyncio.gather(*r._tasks)
    assert ex.locked == [] and ex.gap == 8.0  # 换单归 protection_tick 的同步，这里不再派


@pytest.mark.parametrize("why", ["not_placeable", "sync_failed"])
async def test_runner_profit_lock_records_a_failed_round(tmp_path: Path, why: str) -> None:
    ex = _LockEx(None)
    ex.result = {"action": "retry", "why": why}
    r, log = _lock_runner(tmp_path, ex)
    r._spawn_profit_lock("pos-1")
    await asyncio.gather(*r._tasks)
    rec = log.records[-1]
    assert rec.summary == f"pos-1 锁利止损这一轮没挂上（{why}），之后每轮接着试"
    assert rec.output["profit_lock_round"] == ex.result
