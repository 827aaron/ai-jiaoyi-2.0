"""拍板回放：历史行情 → 同一套特征和哨兵 → 抽样问 Jev → 按之后的 K 线算结果。"""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import (
    Category,
    FastLoopConfig,
    Interval,
    JevRequest,
    JevResponse,
    Kline,
    LevelKind,
    Side,
    SignalKind,
    Trade,
)
from jiaoyi.core import profiles
from jiaoyi.gate import replay
from jiaoyi.risk.guard import stop_atr
from tests.support import builders as b

FAST = FastLoopConfig(**{**profiles.defaults("2h", "fast_loop"), **profiles.PAPER_GATE})
DAY = date(2026, 10, 20)
DAY0 = datetime(2026, 10, 20, tzinfo=UTC)


def _bar(t: datetime, *, hi: float = 100.5, lo: float = 99.5, qv: float = 1000.0) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval=Interval.M1,
        open_time=t,
        close_time=t + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=100.0,
        high=hi,
        low=lo,
        close=100.0,
        volume=qv / 100.0,
        quote_volume=qv,
    )


def test_key_levels_from_previous_day() -> None:
    bars = [_bar(DAY0 - timedelta(minutes=1440 - i)) for i in range(1440)]
    bars[10] = _bar(bars[10].open_time, hi=110.0)  # 24h 高点（不在最后 4h）
    bars[-5] = _bar(bars[-5].open_time, lo=95.0)  # 4h 低点也是 24h 低点
    lv = {(k.note, k.kind): k.price for k in replay.key_levels(bars)}
    assert lv[("前 24h 高点", LevelKind.RESISTANCE)] == 110.0
    assert lv[("前 4h 高点", LevelKind.RESISTANCE)] == 100.5
    assert lv[("前 24h 低点", LevelKind.SUPPORT)] == lv[("前 4h 低点", LevelKind.SUPPORT)] == 95.0


def test_default_symbols_from_card_journal(tmp_path: Path) -> None:
    assert replay.default_symbols(tmp_path)[0] == ("BTCUSDT", Category.MAJOR)
    d = tmp_path / "card_review"
    d.mkdir()
    rows = [("SOLUSDT", "alt")] * 3 + [("ETHUSDT", "major")] * 2 + [("PEPEUSDT", "alt")]
    (d / "cards.jsonl").write_text(
        "".join(json.dumps({"symbol": s, "category": c}) + "\n" for s, c in rows) + "bad\n",
        encoding="utf-8",
    )
    assert replay.default_symbols(tmp_path, 2) == [
        ("SOLUSDT", Category.ALT),
        ("ETHUSDT", Category.MAJOR),
    ]


def test_scan_symbol_fires_same_rules_as_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 天平稳预热 + 半小时平稳成交，之后一分钟大量主动买 → 量能突增（多）。"""
    warm = {
        DAY - timedelta(days=i): [_bar(DAY0 - timedelta(days=i, minutes=-m)) for m in range(1440)]
        for i in (1, 2, 3)
    }
    trades: list[Trade] = []
    for s in range(30 * 60):
        side = Side.LONG if s % 2 else Side.SHORT
        t = DAY0 + timedelta(seconds=s)
        trades.append(Trade(symbol="BTCUSDT", ts=t, price=100.0, qty=1000 / 6000, taker_side=side))
    for s in range(60):
        t = DAY0 + timedelta(minutes=30, seconds=s)
        trades.append(Trade(symbol="BTCUSDT", ts=t, price=100.0, qty=10.0, taker_side=Side.LONG))
    day_bars = [_bar(DAY0 + timedelta(minutes=m)) for m in range(30)]
    monkeypatch.setattr(replay, "_klines_only", lambda sym, day, cache: warm[day])
    monkeypatch.setattr(replay, "fetch_day", lambda sym, day, cache: (Path("k"), Path("t")))
    monkeypatch.setattr(replay, "load_klines", lambda path, sym, *a: day_bars)
    monkeypatch.setattr(replay, "iter_agg_trades", lambda path, sym: iter(trades))
    found = list(replay.scan_symbol("BTCUSDT", Category.MAJOR, [DAY], FAST, dedup_s=60))
    kinds = {(f.signal.kind, f.signal.side_hint) for f in found}
    assert (SignalKind.VOLUME_SURGE, Side.LONG) in kinds
    f = found[0]
    assert f.card.source == "replay" and f.card.is_valid(f.signal.ts)
    assert f.signal.features.atr > 0 and f.signal.features.book is None
    # 同一去重键 60 秒内只发一次
    assert sum(1 for x in found if x.signal.kind == SignalKind.VOLUME_SURGE) == 1


def _found(i: int, price: float = 100.0) -> replay.Found:
    sig = b.signal(price=price, atr=0.5, now=DAY0 + timedelta(minutes=10 * i), signal_id=f"s{i}")
    sig = sig.model_copy(update={"features": sig.features.model_copy(update={"book": None})})
    card = replay.replay_card("BTCUSDT", Category.MAJOR, [], FAST, DAY)
    return replay.Found(sig, card)


def test_candidate_uses_jev_stop_tier_and_guard_atr() -> None:
    f = _found(1)
    resp = b.jev_ok({"stop_width": "wide"}, confidence=0.8)
    c = replay.candidate(f, resp, FAST, 120)
    assert c is not None
    atr = stop_atr(f.signal.features, FAST, f.signal.ts, {})
    assert c.stop_bps == pytest.approx(FAST.max_stop_atr * atr / 100.0 * 1e4)
    assert c.tp_bps == pytest.approx(c.stop_bps * FAST.min_tp_r)
    assert c.stop_src == "jev:wide" and c.verdict == "volume_breakout"
    assert not c.gate_would_block  # 做 0.8 / 真 0.8 / 毒 0.2 都过 paper 门槛
    low = b.jev_ok({"take": "skip"}, confidence=0.8)
    c2 = replay.candidate(f, low, FAST, 120)
    assert c2 is not None and c2.gate_would_block and c2.p_take == pytest.approx(0.2)
    assert replay.candidate(f, b.jev_failed(), FAST, 120) is None


def test_pick_is_reproducible_and_sorted() -> None:
    found = [_found(i) for i in range(50)]
    a, b2 = replay.pick(found, 10, seed=1), replay.pick(found, 10, seed=1)
    assert [x.signal.signal_id for x in a] == [x.signal.signal_id for x in b2]
    assert len(a) == 10 and all(x.signal.ts <= y.signal.ts for x, y in itertools.pairwise(a))
    assert len(replay.pick(found[:5], 10)) == 5


class _Model:
    def __init__(self) -> None:
        self.seen: list[JevRequest] = []

    async def ask(self, request: JevRequest) -> JevResponse:
        self.seen.append(request)
        return b.jev_ok(request_id=request.request_id)


def test_run_asks_jev_and_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = [_found(i) for i in range(6)]
    monkeypatch.setattr(replay, "_scan_all", lambda *a: found)
    model = _Model()

    async def fetch(sym: str, a: datetime, b2: datetime) -> list[Kline]:
        # 一路涨：都先碰止盈
        return [
            _bar(a + timedelta(minutes=m), hi=100 + 0.2 * m, lo=100.0)
            for m in range(int((b2 - a).total_seconds() // 60))
        ]

    out: list[str] = []
    res: dict[str, Any] = asyncio.run(
        replay.run(
            data_dir=tmp_path,
            model=model,  # type: ignore[arg-type]
            fast=FAST,
            symbols=[("BTCUSDT", Category.MAJOR)],
            days=[DAY],
            n=4,
            fee_bps=5.0,
            dedup_s=30,
            concurrency=2,
            fetch=fetch,
            now=DAY0 + timedelta(days=2),
            echo=out.append,
        )
    )
    assert len(model.seen) == 4
    snap = json.loads(model.seen[0].snapshot)
    assert snap["book"] is None and snap["hold_min"] == FAST.time_limit_s // 60
    assert res["asked"] == 4 and res["failed"] == 0 and res["signals_found"] == 6
    assert res["current_thresholds"]["would_pass"] == 4
    q = res["templates"][next(iter(res["templates"]))]
    assert q["by_exit"]["tp"]["n"] == 4
    saved = json.loads((tmp_path / "debug" / "jev_replay.json").read_text(encoding="utf-8"))
    assert saved["missing"] == replay.MISSING
    run_dir = Path(res["dir"])
    assert len((run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 4
    text = "\n".join(replay.render(res))
    assert "按现在门槛" in text and "盘口" in text


class _Flaky(_Model):
    """第 1 个超时、第 2 个出错，其余正常（延迟 2000ms）。"""

    async def ask(self, request: JevRequest) -> JevResponse:
        self.seen.append(request)
        n = len(self.seen)
        if n == 1:
            return b.jev_failed("Jev 超时（>30000ms）", request_id=request.request_id)
        if n == 2:
            return b.jev_failed("HTTP 500", request_id=request.request_id)
        return b.jev_ok(request_id=request.request_id).model_copy(update={"latency_ms": 2000.0})


def test_run_counts_timeouts_and_latency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "_scan_all", lambda *a: [_found(i) for i in range(5)])

    async def fetch(sym: str, a: datetime, b2: datetime) -> list[Kline]:
        return [
            _bar(a + timedelta(minutes=m), hi=100 + 0.2 * m, lo=100.0)
            for m in range(int((b2 - a).total_seconds() // 60))
        ]

    res: dict[str, Any] = asyncio.run(
        replay.run(
            data_dir=tmp_path,
            model=_Flaky(),  # type: ignore[arg-type]
            fast=FAST,
            symbols=[("BTCUSDT", Category.MAJOR)],
            days=[DAY],
            n=5,
            fee_bps=5.0,
            dedup_s=30,
            concurrency=1,
            fetch=fetch,
            now=DAY0 + timedelta(days=2),
            echo=lambda _: None,
            live_timeout_ms=1500,
        )
    )
    assert res["jev_calls"] == {"ok": 3, "timeout": 1, "error": 1}
    assert res["failed"] == 0  # 超时 / 出错不算「没算出结果」，也不进统计
    assert res["current_thresholds"]["would_pass"] == 3
    lat = res["latency_ms"]
    assert lat["p50"] == 2000.0 and lat["over_live_budget"] == 1.0
    text = "\n".join(replay.render(res))
    assert "Jev 成功 3 / 超时 1 / 出错 1" in text and "会超时 100%" in text
