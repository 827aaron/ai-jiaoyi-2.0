"""拍板回测：历史拍板记录 + 之后的 1 分钟线 → 「做了会怎样」，按概率分桶给门槛推荐。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jiaoyi.contracts import DecisionRecord, Interval, Kline, Outcome, Role
from jiaoyi.gate import outcomes

T0 = datetime(2026, 10, 23, 13, 0, 30, tzinfo=UTC)


def _write(data_dir: Path, recs: list[DecisionRecord]) -> None:
    d = data_dir / "decision_log"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "2026-10-23.jsonl").open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(r.model_dump_json() + "\n")


def _gate(
    sid: str, side: str, p_take: float, *, hold: int | None = 60, tpl: str = "q3"
) -> DecisionRecord:
    snap: dict[str, object] = {"sym": "BTCUSDT", "side": side, "px": 100.0, "atr_bps": 20.0}
    if hold is not None:
        snap["hold_min"] = hold
    return DecisionRecord(
        record_id=f"g-{sid}",
        ts=T0,
        role=Role.GATE,
        symbol="BTCUSDT",
        signal_id=sid,
        outcome=Outcome.PASS if p_take >= 0.5 else Outcome.REJECT,
        summary="BTCUSDT 多：…",
        input={
            "jev": {"template": tpl, "called": True, "kind": "entry", "snapshot": json.dumps(snap)}
        },
        output={
            "jev": {
                "ok": True,
                "answers": {
                    "take": {"take": p_take, "skip": 1 - p_take},
                    "genuine": {"real": 0.6, "fake": 0.4},
                    "toxicity": {"low": 0.7, "high": 0.3},
                },
            }
        },
    )


def _guard(sid: str, outcome: Outcome, detail: dict[str, object], **out: object) -> DecisionRecord:
    return DecisionRecord(
        record_id=f"r-{sid}",
        ts=T0,
        role=Role.RISK_GUARD,
        symbol="BTCUSDT",
        signal_id=sid,
        outcome=outcome,
        summary="BTCUSDT 多 …",
        input={"trigger_price": 100.0, "kind": "breakout", "side": "long"},
        output={**out, "detail": detail},
    )


def _bars(prices: list[tuple[float, float, float]]) -> list[Kline]:
    """(high, low, close) 每分钟一根，从信号那一分钟开始。"""
    start = T0.replace(second=0)
    return [
        Kline(
            symbol="BTCUSDT",
            interval=Interval.M1,
            open_time=start + timedelta(minutes=i),
            close_time=start + timedelta(minutes=i + 1) - timedelta(milliseconds=1),
            open=100.0,
            high=h,
            low=lo,
            close=c,
            volume=1.0,
        )
        for i, (h, lo, c) in enumerate(prices)
    ]


def test_candidates_join_gate_and_guard(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            _gate("a", "long", 0.7),
            _guard("a", Outcome.PASS, {}, stop_loss_pct=0.5, take_profit_pct=1.0),
            _gate("b", "short", 0.3, hold=None, tpl="q2"),
            _guard("b", Outcome.REJECT, {"by": "gate", "stop_pct": 0.4}),
            _gate("c", "long", 0.2),  # 没有保镖记录：按 atr 估止损
        ],
    )
    cs = {c.signal_id: c for c in outcomes.candidates(tmp_path, T0 - timedelta(hours=1), 240)}
    a, b, c = cs["a"], cs["b"], cs["c"]
    assert (a.verdict, a.stop_bps, a.tp_bps, a.stop_src, a.p_take) == (
        "opened",
        50.0,
        100.0,
        "guard",
        0.7,
    )
    assert (b.verdict, b.hold_min, b.template, b.stop_bps, b.side) == (
        "gate",
        240,
        "q2",
        40.0,
        "short",
    )
    assert (c.verdict, c.stop_src, c.stop_bps, c.tp_bps) == ("none", "est", 30.0, 45.0)


def _cand(
    side: str = "long", stop: float | None = 50.0, tp: float | None = 100.0
) -> outcomes.Candidate:
    return outcomes.Candidate(
        signal_id="x", symbol="BTCUSDT", side=side, ts=T0, entry=100.0, hold_min=5,
        template="q3", verdict="opened", gate_would_block=False, stop_bps=stop, tp_bps=tp,
        stop_src="guard", p_take=0.6, p_real=0.6, p_toxic=0.3,
    )  # fmt: skip


def test_simulate_stop_tp_time() -> None:
    # 信号那一分钟（第 0 根）不算；第 2 根先碰止盈
    bars = _bars([(99.0, 90.0, 99), (100.5, 99.8, 100.2), (101.2, 99.9, 101.0), (101, 99, 100)])
    r = outcomes.simulate(_cand(), bars, fee_bps=5.0)
    assert r is not None and (r.exit, r.gross_bps, r.net_bps, r.r) == ("tp", 100.0, 90.0, 1.8)
    # 同一根两边都碰到：按先止损
    r = outcomes.simulate(_cand(), _bars([(100, 100, 100), (101.5, 99.4, 100)]), fee_bps=5.0)
    assert r is not None and (r.exit, r.gross_bps) == ("stop", -50.0)
    # 空单、都没碰到：按窗口最后一根收盘
    bars = _bars([(100, 100, 100)] + [(100.2, 99.8, 99.9)] * 7)
    r = outcomes.simulate(_cand("short"), bars, fee_bps=5.0)
    assert r is not None and r.exit == "time" and round(r.gross_bps, 6) == 10.0
    assert r.mfe_bps == 20.0 and r.mae_bps == -20.0


def test_backfill_caches_and_skips_open_windows(tmp_path: Path) -> None:
    _write(tmp_path, [_gate("a", "long", 0.7), _gate("b", "long", 0.3, hold=600)])
    calls: list[str] = []

    async def fetch(sym: str, a: datetime, b: datetime) -> list[Kline]:
        calls.append(sym)
        return _bars([(100, 100, 100)] + [(100.3, 99.9, 100.2)] * 70)

    now = T0 + timedelta(hours=2)  # b 的 10 小时窗口还没走完
    kw = {"since": T0 - timedelta(days=1), "now": now, "default_hold_min": 60, "fee_bps": 5.0}
    assert asyncio.run(outcomes.backfill(tmp_path, fetch=fetch, **kw)) == (1, 0)  # type: ignore[arg-type]
    assert asyncio.run(outcomes.backfill(tmp_path, fetch=fetch, **kw)) == (0, 0)  # type: ignore[arg-type]
    assert len(calls) == 1
    rows = outcomes.read_results(tmp_path)
    assert [r["signal_id"] for r in rows] == ["a"] and rows[0]["exit"] == "time"


def test_report_buckets_and_recommends() -> None:
    rows = []
    for i in range(40):
        p = 0.2 + 0.015 * i  # 0.2–0.785
        good = p >= 0.5
        rows.append(
            {"signal_id": str(i), "template": "q3", "p_take": p, "p_real": 0.6, "p_toxic": 0.3,
             "exit": "tp" if good else "stop", "net_bps": 90.0 if good else -60.0,
             "r": 1.8 if good else -1.2}
        )  # fmt: skip
    rows.append({**rows[0], "signal_id": "old", "template": "q2"})
    rep = outcomes.report(rows)
    assert set(rep) == {"q2", "q3"} and rep["q2"]["all"]["n"] == 1
    q3 = rep["q3"]
    assert q3["by_exit"]["tp"]["n"] + q3["by_exit"]["stop"]["n"] == 40
    assert q3["by_take"]["0.6–1.0"]["win"] == 1.0
    assert q3["recommend"]["min_p_take"]["threshold"] == 0.5
    assert q3["recommend"]["toxicity_below"] is not None
    text = "\n".join(outcomes.render(rep))
    assert "模板 q3" in text and "做概率 ≥ 0.50" in text
