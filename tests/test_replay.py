"""回放框架本身的测试：成交模拟、延迟、手续费、日清零、故障即关闭。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.backtest import LatencyModel, PaperAccount, ReplayConfig, ReplayEngine, static_card
from jiaoyi.backtest.data import merge_events, parse_agg_trades, parse_klines
from jiaoyi.backtest.synthetic import synthetic_trades
from jiaoyi.contracts import Interval, MarketEvent, Side, Trade
from jiaoyi.contracts.common import Role
from jiaoyi.contracts.decision_log import Outcome
from jiaoyi.contracts.orders import OrderIntent
from tests.support import builders as b
from tests.support.contracts import assert_intent_within_limits
from tests.support.fakes import (
    FailingDecisionModel,
    FakeEntryGate,
    InMemoryDecisionLog,
    RecordedDecisionModel,
    RuleDecisionModel,
)
from tests.support.reference_risk import ReferenceRiskGuard
from tests.support.simple_pipeline import SimpleFeatureEngine, SimpleTrigger

START = datetime(2026, 10, 20, tzinfo=UTC)


def engine(
    model: object,
    *,
    log: InMemoryDecisionLog | None = None,
    latency: LatencyModel | None = None,
    config: ReplayConfig | None = None,
    risk_over: dict[str, object] | None = None,
) -> tuple[ReplayEngine, InMemoryDecisionLog]:
    risk = b.risk_config(**(risk_over or {}))
    lg = log or InMemoryDecisionLog()
    card = b.card([b.card_symbol()], now=START, ttl=timedelta(days=2))
    eng = ReplayEngine(
        features=SimpleFeatureEngine(),
        trigger=SimpleTrigger(),
        gate=FakeEntryGate(model),
        guard=ReferenceRiskGuard(risk),
        cards=static_card(card),
        risk=risk,
        log=lg,
        latency=latency or LatencyModel(order_ms=250.0, jitter=0.0),
        config=config,
    )
    return eng, lg


def synth(seconds: int = 7200, **kw: float) -> list[MarketEvent]:
    return list(synthetic_trades("BTCUSDT", START, seconds, **kw))  # type: ignore[arg-type]


# ---------------------------------------------------------------- 流程
async def test_end_to_end_smoke() -> None:
    eng, log = engine(RuleDecisionModel(take_prob=1.0))
    rep = await eng.run(synth())
    assert rep.triggers > 0
    assert rep.jev_calls == rep.triggers
    assert rep.risk_approved + rep.risk_rejected == rep.jev_calls
    assert rep.filled <= rep.risk_approved
    assert rep.closed == rep.filled, "回放结束时所有仓都要平掉"
    assert eng.account.positions == {}
    assert rep.wins + rep.losses == rep.closed
    assert rep.net_pnl == pytest.approx(rep.gross_pnl - rep.fees)
    assert rep.final_equity == pytest.approx(rep.initial_equity + rep.net_pnl)
    roles = {r.role for r in log.records}
    assert {Role.SENTINEL, Role.GATE, Role.RISK_GUARD} <= roles


async def test_is_deterministic() -> None:
    a, _ = engine(RuleDecisionModel(take_prob=0.6, seed=9))
    c, _ = engine(RuleDecisionModel(take_prob=0.6, seed=9))
    ra, rc = await a.run(synth()), await c.run(synth())
    assert ra == rc


async def test_every_filled_trade_has_a_risk_pass_record_and_obeys_limits() -> None:
    eng, log = engine(RuleDecisionModel(take_prob=1.0))
    rep = await eng.run(synth())
    assert rep.filled > 0
    passes = {
        r.signal_id: r
        for r in log.records
        if r.role is Role.RISK_GUARD and r.outcome is Outcome.PASS
    }
    cfg = b.risk_config()
    card = b.card([b.card_symbol()], now=START, ttl=timedelta(days=2))
    for t in eng.account.closed:
        assert t.signal_id in passes, "没有保镖放行记录就成交了"
        intent = OrderIntent.model_validate(passes[t.signal_id].output)
        assert_intent_within_limits(intent, cfg, b.account(equity=rep.initial_equity), card)


# ---------------------------------------------------------------- 故障即关闭
async def test_jev_down_means_no_trades() -> None:
    eng, log = engine(FailingDecisionModel())
    rep = await eng.run(synth())
    assert rep.triggers > 0
    assert rep.jev_failed == rep.jev_calls == rep.triggers
    assert rep.risk_approved == 0 and rep.filled == 0 and rep.closed == 0
    assert rep.net_pnl == 0.0
    assert all(r.outcome is Outcome.ERROR for r in log.records if r.role is Role.GATE)
    assert rep.rejection_reasons.get("决策模型不可用，故障即关闭") == rep.triggers


async def test_recorded_answers_missing_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "rec.jsonl"
    path.write_text("", encoding="utf-8")
    model = RecordedDecisionModel(path)
    eng, _ = engine(model)
    rep = await eng.run(synth())
    assert rep.filled == 0
    assert model.misses == rep.triggers > 0


async def test_recorded_answers_are_replayed(tmp_path: Path) -> None:
    # 先用 mock 跑一遍拿到 request_id，再把答复录下来，换成录制模式应得到同样的结果
    first, log1 = engine(RuleDecisionModel(take_prob=1.0))
    r1 = await first.run(synth())
    resp = [
        r.output | {"request_id": f"req-{r.signal_id}"} for r in log1.records if r.role is Role.GATE
    ]
    path = tmp_path / "rec.jsonl"
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in resp), encoding="utf-8")
    second, _ = engine(RecordedDecisionModel(path))
    r2 = await second.run(synth())
    assert (r2.triggers, r2.filled, r2.net_pnl) == (r1.triggers, r1.filled, r1.net_pnl)


async def test_no_valid_card_means_no_signals() -> None:
    risk = b.risk_config()
    expired = b.card(now=START - timedelta(days=1), ttl=timedelta(hours=1))
    eng = ReplayEngine(
        features=SimpleFeatureEngine(),
        trigger=SimpleTrigger(),
        gate=FakeEntryGate(RuleDecisionModel(take_prob=1.0)),
        guard=ReferenceRiskGuard(risk),
        cards=static_card(expired),
        risk=risk,
        log=InMemoryDecisionLog(),
    )
    rep = await eng.run(synth())
    assert rep.triggers == 0 and rep.filled == 0


# ---------------------------------------------------------------- 手续费与延迟
async def test_fees_reduce_pnl_and_scale_with_fee_bps() -> None:
    lo, _ = engine(RuleDecisionModel(take_prob=1.0), config=ReplayConfig(fee_bps=0.0))
    hi, _ = engine(RuleDecisionModel(take_prob=1.0), config=ReplayConfig(fee_bps=10.0))
    rlo, rhi = await lo.run(synth()), await hi.run(synth())
    assert rlo.fees == 0.0
    assert rhi.fees > 0.0
    assert rhi.net_pnl < rlo.net_pnl


async def test_high_fee_turns_small_takes_into_rejections() -> None:
    """手续费越高，“止盈太小不够手续费”的拒单越多——复现一日 BTC 回放里的主要拒单原因。"""
    low, _ = engine(RuleDecisionModel(take_prob=1.0), risk_over={"taker_fee_bps": 0.0})
    high, _ = engine(
        RuleDecisionModel(take_prob=1.0),
        risk_over={"taker_fee_bps": 20.0, "min_tp_fee_multiple": 10.0},
    )
    rl, rh = await low.run(synth()), await high.run(synth())
    fee_rej = "止盈太小，覆盖不了手续费"
    assert rh.rejection_reasons.get(fee_rej, 0) > rl.rejection_reasons.get(fee_rej, 0)
    assert rh.filled < rl.filled


async def test_latency_delays_fills() -> None:
    fast, lf = engine(RuleDecisionModel(take_prob=1.0, latency_ms=100.0))
    slow, ls = engine(
        RuleDecisionModel(take_prob=1.0, latency_ms=3000.0),
        latency=LatencyModel(order_ms=1500.0, jitter=0.0),
    )
    await fast.run(synth())
    await slow.run(synth())

    def first_gap(log: InMemoryDecisionLog) -> float:
        fill = next(
            r for r in log.records if r.role is Role.EXECUTOR and r.outcome is Outcome.ACTION
        )
        gate = next(r for r in log.records if r.role is Role.GATE and r.signal_id == fill.signal_id)
        return (fill.ts - gate.ts).total_seconds()

    assert first_gap(lf) < 1.0
    assert first_gap(ls) >= 4.5, "决策模型 3 秒 + 下单 1.5 秒，成交不可能更早"


def test_latency_is_reproducible() -> None:
    a, c = LatencyModel(seed=1), LatencyModel(seed=1)
    assert [a.order_delay() for _ in range(5)] == [c.order_delay() for _ in range(5)]


# ---------------------------------------------------------------- 纸面账户
def _intent(side: Side = Side.LONG, **over: object) -> OrderIntent:
    sig = b.signal(side=side)
    v = ReferenceRiskGuard(b.risk_config()).review(
        sig,
        b.jev_ok({"direction": "up" if side is Side.LONG else "down"}),
        b.card(),
        b.account(),
        b.T0,
    )
    assert v.intent is not None
    return v.intent.model_copy(update=over)


def test_long_stop_loss_and_fees() -> None:
    acct = PaperAccount(10_000.0, fee_bps=5.0, slippage_bps=0.0)
    intent = _intent()
    pos = acct.open_position(intent, 68_000.0, b.T0)
    stop = pos.stop_price
    assert stop < 68_000.0
    t1 = b.T0 + timedelta(seconds=2)
    assert acct.check_exits("BTCUSDT", stop + 1, t1, 1.0) == []
    (closed,) = acct.check_exits("BTCUSDT", stop - 1, t1 + timedelta(seconds=2), 1.0)
    assert closed.reason == "stop_loss"
    assert closed.gross_pnl < 0
    assert closed.fees == pytest.approx((68_000.0 + closed.exit_price) * intent.quantity * 0.0005)
    assert acct.equity() == pytest.approx(10_000.0 + closed.net_pnl)
    # 亏损应约等于一笔风险金额（跳过一两个价位的误差）
    assert abs(closed.gross_pnl) == pytest.approx(intent.risk_usdt, rel=0.1)


def test_short_take_profit() -> None:
    acct = PaperAccount(10_000.0, fee_bps=0.0, slippage_bps=0.0)
    intent = _intent(Side.SHORT)
    pos = acct.open_position(intent, 68_000.0, b.T0)
    assert pos.take_price < 68_000.0 < pos.stop_price
    (closed,) = acct.check_exits("BTCUSDT", pos.take_price - 1, b.T0 + timedelta(seconds=2), 1.0)
    assert closed.reason == "take_profit" and closed.gross_pnl > 0


def test_time_limit_closes() -> None:
    acct = PaperAccount(10_000.0, fee_bps=0.0, slippage_bps=0.0)
    intent = _intent(time_limit_s=60)
    acct.open_position(intent, 68_000.0, b.T0)
    assert acct.check_exits("BTCUSDT", 68_000.0, b.T0 + timedelta(seconds=30), 1.0) == []
    (closed,) = acct.check_exits("BTCUSDT", 68_000.0, b.T0 + timedelta(seconds=61), 1.0)
    assert closed.reason == "time_limit"


def test_exits_checked_at_most_once_per_interval() -> None:
    acct = PaperAccount(10_000.0, fee_bps=0.0, slippage_bps=0.0)
    pos = acct.open_position(_intent(), 68_000.0, b.T0)
    # 0.5 秒内价格穿了止损，但执行器每秒才查一次：此刻不平
    assert (
        acct.check_exits("BTCUSDT", pos.stop_price - 5, b.T0 + timedelta(milliseconds=500), 1.0)
        == []
    )
    assert acct.check_exits("BTCUSDT", pos.stop_price - 5, b.T0 + timedelta(seconds=1), 1.0)


def test_slippage_is_adverse_both_ways() -> None:
    acct = PaperAccount(10_000.0, fee_bps=0.0, slippage_bps=10.0)
    pos = acct.open_position(_intent(), 68_000.0, b.T0)
    assert pos.entry_price == pytest.approx(68_000.0 * 1.001)
    closed = acct.close_position(pos.position_id, 68_000.0, b.T0 + timedelta(seconds=5), "manual")
    assert closed.exit_price == pytest.approx(68_000.0 * 0.999)
    assert closed.gross_pnl < 0


def test_day_reset_at_bangkok_midnight() -> None:
    """曼谷 0 点 = UTC 17:00。清零前亏的钱不影响清零后的“当日盈亏”。"""
    acct = PaperAccount(10_000.0, fee_bps=0.0, slippage_bps=0.0, day_reset_tz="Asia/Bangkok")
    before = datetime(2026, 10, 20, 16, 59, tzinfo=UTC)
    after = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)
    acct.roll_day(before)
    acct.realized = -400.0  # 当日已亏 4%
    s1 = acct.state(before)
    assert s1.day_pnl_pct == pytest.approx(-4.0)
    s2 = acct.state(after)
    assert s2.day_pnl_pct == pytest.approx(0.0)
    assert s2.equity_usdt == pytest.approx(9_600.0)


def test_day_reset_uses_utc_when_configured() -> None:
    acct = PaperAccount(10_000.0, fee_bps=0.0, slippage_bps=0.0, day_reset_tz="UTC")
    acct.roll_day(datetime(2026, 10, 20, 23, 59, tzinfo=UTC))
    acct.realized = -400.0
    assert acct.state(datetime(2026, 10, 20, 23, 59, 30, tzinfo=UTC)).day_pnl_pct == pytest.approx(
        -4.0
    )
    assert acct.state(datetime(2026, 10, 21, 0, 0, tzinfo=UTC)).day_pnl_pct == pytest.approx(0.0)


async def test_daily_halt_blocks_new_entries_in_replay() -> None:
    """日亏超线后回放不再放行。突破后反向走，顺势单都被打止损，很快触发熔断。"""
    eng, log = engine(
        RuleDecisionModel(take_prob=1.0),
        config=ReplayConfig(initial_equity=1_000.0),  # 小账户：两三笔止损就到 0.5%
        risk_over={"daily_loss_halt_pct": 0.5},
    )
    rep = await eng.run(synth(14_400, reversal=4.0))
    halted = [r for r in log.records if r.role is Role.RISK_GUARD and "熔断" in r.summary]
    assert halted, "场景没有触发熔断，测试没有意义"
    first = halted[0].ts
    passes_after = [
        r
        for r in log.records
        if r.role is Role.RISK_GUARD and r.outcome is Outcome.PASS and r.ts > first
    ]
    assert passes_after == []
    assert rep.closed == rep.filled


# ---------------------------------------------------------------- 数据读取
def test_parse_agg_trades_sides() -> None:
    import io

    csv_text = (
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
        "1,68000.1,0.5,1,1,1760918400000,true\n"
        "2,68000.2,0.2,2,2,1760918400500,false\n"
    )
    t1, t2 = list(parse_agg_trades(io.StringIO(csv_text), "BTCUSDT"))
    assert t1.taker_side is Side.SHORT and t2.taker_side is Side.LONG
    assert t1.ts == datetime(2025, 10, 20, 0, 0, tzinfo=UTC)
    assert t2.ts - t1.ts == timedelta(milliseconds=500)


def test_parse_klines_with_and_without_header() -> None:
    import io

    row = "1760918400000,68000,68100,67900,68050,12.5,1760918459999,850000,100,6,400000,0\n"
    header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore\n"
    for text in (row, header + row):
        (k,) = list(parse_klines(io.StringIO(text), "BTCUSDT", Interval.M1))
        assert k.close == 68050.0 and k.quote_volume == 850000.0
        assert k.close_time - k.open_time == timedelta(seconds=59, milliseconds=999)


def test_merge_events_orders_by_time_and_kline_after_trade() -> None:
    from jiaoyi.contracts import Kline

    t0 = datetime(2026, 10, 20, tzinfo=UTC)
    trades = [
        Trade(
            symbol="BTCUSDT", ts=t0 + timedelta(seconds=s), price=1.0, qty=1.0, taker_side=Side.LONG
        )
        for s in (1, 59, 60)
    ]
    kline = Kline(
        symbol="BTCUSDT",
        interval=Interval.M1,
        open_time=t0,
        close_time=t0 + timedelta(seconds=60),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    )
    merged = list(merge_events(trades, [kline]))
    assert [type(e).__name__ for e in merged] == ["Trade", "Trade", "Trade", "Kline"]
