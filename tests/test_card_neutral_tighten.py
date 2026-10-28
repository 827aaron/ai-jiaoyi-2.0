"""作战卡把持仓的币改成中性（card_neutral_action）：复用 #335 反向卡的收紧 / 锚定（合成数据）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core import hotconfig
from jiaoyi.execution.exchange import ExecutionBook
from jiaoyi.slow.tradingagents_engine import stale_card
from tests.fastloop_helpers import FAST, card
from tests.test_execution import START
from tests.test_keeper_patrol import position
from tests.test_opposite_card_tighten import (
    TIGHT,
    TightenExecutor,
    TrailHarness,
    _runner,
    drain,
    make_keeper,
)

NEUTRAL = FAST.model_copy(update={"card_neutral_action": "tighten"})
KEY = "BTCUSDT|2026-10-22T06:30:00+00:00|no_trade|neutral"


def short_pos(now: datetime, **over: Any) -> c.PositionRecord:
    """NEAR / QNT 那种：照旧卡开的空单。"""
    return position(now, side="short", entry_price=68000.0, stop_loss_price=68120.0, **over)


def neutral_card(now: datetime, **over: Any) -> c.BattleCard:
    """开仓（now − 5 分钟）之后发布的新卡把 BTCUSDT 改成 no_trade。"""
    data: dict[str, Any] = {"bias": "no_trade", "allowed_signals": [], "issued_at": now.isoformat()}
    data.update(over)
    return card(now, **data)


def stale_watch(now: datetime, bias: str = "short", reason: str | None = None) -> c.BattleCard:
    base = card(now, bias=bias, issued_at=now.isoformat())
    cs = stale_card(base.symbols[0], "现价已到失效价") if reason is None else base.symbols[0]
    if reason is not None:
        cs = cs.model_copy(update={"allowed_signals": [], "reason": reason})
    return base.model_copy(update={"symbols": [cs, *base.symbols[1:]]})


# ---------------------------------------------------------------- 默认 = 现状


def test_default_hold_ignores_neutral_card(now: datetime) -> None:
    assert FAST.card_neutral_action == "hold" and FAST.card_neutral_on_absent is False
    k, rec, _, _ = make_keeper(now, FAST, cd=neutral_card(now))
    pos = short_pos(now)
    assert k.review_card_rule(pos, now) is None
    assert not k.card_rule_applies(pos, now) and k.card_rule_key(pos, now) is None
    assert rec.rows == []


def test_settings_are_hot_reloadable() -> None:
    hot = set(hotconfig.hot_keys())
    for name in (
        "card_neutral_action",
        "card_neutral_on_absent",
        "card_neutral_trail_distance_mult",
    ):
        assert f"fast_loop.{name}" in hot


# ---------------------------------------------------------------- 什么算中性


def test_no_trade_after_open_asks_to_tighten_and_logs_once(now: datetime) -> None:
    k, rec, model, _ = make_keeper(now, NEUTRAL, cd=neutral_card(now))
    pos = short_pos(now)
    assert k.card_rule_applies(pos, now) and k.card_rule_watch(pos, now)
    assert k.card_rule_key(pos, now) == KEY
    assert k.card_rule_tighten(pos, now) == {
        "kind": "neutral_card",
        "key": KEY,
        "allow_close": True,
    }
    for _ in range(3):
        v = k.review_card_rule(pos, now)
        assert v is not None and v.action == "hold"
    assert len(rec.rows) == 1 and model.ask.call_count == 0
    rule = rec.rows[0]["output"]["card_rule"]
    assert rule["kind"] == "neutral_card" and rule["branch"] == "neutral_tighten"
    assert rule["card_bias"] == "no_trade" and rule["instruction_key"] == KEY
    assert rule["price"] == 68000.0 and rule["counterfactual"] == {"legacy_action": "hold"}
    assert "作战卡转中性" in rec.rows[0]["summary"]


def test_stale_on_arrival_same_direction_is_neutral(now: datetime) -> None:
    k, _, _, _ = make_keeper(now, NEUTRAL, cd=stale_watch(now))
    v = k.review_card_rule(short_pos(now), now)
    assert v is not None and v.action == "hold" and "stale_on_arrival" in v.reason
    assert k.card_rule_key(short_pos(now), now) == "BTCUSDT|2026-10-22T06:30:00+00:00|short|neutral"


def test_stale_on_arrival_marker_survives_long_reason(now: datetime) -> None:
    cs = card(now, reason="长" * 500).symbols[0]
    voided = stale_card(cs, "现价已到失效价")
    assert len(voided.reason) == 500 and voided.reason.endswith(
        "（stale_on_arrival：现价已到失效价）"
    )


@pytest.mark.parametrize(
    "cd",
    [
        lambda now: stale_watch(now, reason="波动太大，先只看不做"),  # 别的原因的只看不做
        lambda now: card(now, bias="short", issued_at=now.isoformat()),  # 方向没变
        lambda now: neutral_card(now, issued_at=(now - timedelta(minutes=6)).isoformat()),
        lambda now: card(now).model_copy(
            update={"symbols": card(now).symbols[1:], "created_at": now}
        ),  # 开仓后的新卡里没有这个币
    ],
    ids=["plain_watch", "same_direction", "issued_before_open", "absent_default"],
)
def test_not_neutral(now: datetime, cd: Any) -> None:
    k, rec, _, _ = make_keeper(now, NEUTRAL, cd=cd(now))
    assert k.review_card_rule(short_pos(now), now) is None
    assert not k.card_rule_applies(short_pos(now), now) and rec.rows == []


def test_absent_symbol_counts_only_when_switched_on(now: datetime) -> None:
    base = card(now)
    cd = base.model_copy(update={"symbols": base.symbols[1:], "created_at": now})
    cfg = NEUTRAL.model_copy(update={"card_neutral_on_absent": True})
    k, rec, _, _ = make_keeper(now, cfg, cd=cd)
    v = k.review_card_rule(short_pos(now), now)
    assert v is not None and v.action == "hold" and "没有这个币" in v.reason
    assert k.card_rule_key(short_pos(now), now) == f"BTCUSDT|{now.isoformat()}|absent"
    assert rec.rows[0]["output"]["card_rule"]["card_bias"] is None


def test_opposite_card_takes_over_from_neutral(now: datetime) -> None:
    cfg = TIGHT.model_copy(update={"card_neutral_action": "tighten"})
    holder: list[c.BattleCard] = [neutral_card(now)]
    k, _, _, _ = make_keeper(now, cfg, price=[67990.0])
    k._card = lambda _: holder[0]
    pos = short_pos(now)
    assert k.card_rule_tighten(pos, now)["kind"] == "neutral_card"  # type: ignore[index]
    holder[0] = card(now, bias="long", issued_at=(now + timedelta(minutes=1)).isoformat())
    later = now + timedelta(minutes=2)
    spec = k.card_rule_tighten(pos, later)
    assert spec == {
        "kind": "opposite_card",
        "key": "BTCUSDT|2026-10-22T06:31:00+00:00|long",
        "allow_close": True,
    }
    v = k.review_card_rule(pos, later)
    assert v is not None and "反向" in v.reason


# ---------------------------------------------------------------- close / 行情过旧


def test_close_mode_closes_directly_but_not_on_stale_price(now: datetime) -> None:
    cfg = FAST.model_copy(update={"card_neutral_action": "close", "keeper_price_max_age_s": 20.0})
    k, rec, _, _ = make_keeper(now, cfg, cd=neutral_card(now))
    pos = short_pos(now)
    assert k.card_rule_watch(pos, now) is False  # 立刻平的不再排模型复查
    later = now + timedelta(seconds=60)  # 行情快照还停在 now：旧了 60 秒
    stale = k.review_card_rule(pos, later)
    assert stale is not None and stale.action == "hold" and k.card_rule_tighten(pos, later) is None
    assert rec.rows[-1]["output"]["card_rule"]["branch"] == "no_market_data_hold"
    fresh = k.review_card_rule(pos, now)
    assert fresh is not None and fresh.action == "close"
    assert rec.rows[-1]["output"]["card_rule"]["branch"] == "neutral_close"


def test_tighten_on_stale_price_never_allows_close(now: datetime) -> None:
    cfg = NEUTRAL.model_copy(update={"keeper_price_max_age_s": 20.0})
    k, rec, _, _ = make_keeper(now, cfg, cd=neutral_card(now))
    later = now + timedelta(seconds=60)
    v = k.review_card_rule(short_pos(now), later)
    assert v is not None and v.action == "hold"
    assert k.card_rule_tighten(short_pos(now), later)["allow_close"] is False  # type: ignore[index]
    rule = rec.rows[-1]["output"]["card_rule"]
    assert rule["branch"] == "no_market_data_tighten" and rule["price"] is None
    assert rule["price_stale_s"] == 60
    k._features = lambda _: None
    assert k.card_rule_tighten(short_pos(now), now)["allow_close"] is False  # type: ignore[index]


# ---------------------------------------------------------------- 主循环接线


async def test_loop_losing_position_closes(tmp_path: Path, now: datetime) -> None:
    k, _, _, _ = make_keeper(now, NEUTRAL, cd=neutral_card(now))
    ex = TightenExecutor(short_pos(now), {"action": "close", "why": "target_crossed"})
    runner = _runner(tmp_path, now, k, ex)
    runner.positions = [short_pos(now)]
    rows: list[dict[str, Any]] = []
    runner.app.recorders[c.Role.KEEPER].record = lambda *a, **kw: rows.append(kw)  # type: ignore[method-assign]
    attempted = await runner._review_card_rules()
    await drain(runner)
    assert attempted == set()  # 模型复查照常
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]
    fl = runner.app.app_cfg.fast_loop
    assert ex.extra == [("trail", fl.card_neutral_trail_distance_mult, KEY)]
    assert ex.tighten_calls[0][2:] == (
        fl.opposite_tighten_buffer_bps,
        fl.opposite_tighten_mark_gap_bps,
    )
    assert ex.allow_close == [True]
    logged = [r["output"] for r in rows if "opposite_tighten" in r.get("output", {})]
    assert logged[0]["card_rule_kind"] == "neutral_card"


async def test_loop_profitable_anchors_once_per_instruction(tmp_path: Path, now: datetime) -> None:
    k, _, _, _ = make_keeper(now, NEUTRAL, cd=neutral_card(now))
    ex = TightenExecutor(short_pos(now), {"action": "tightened", "why": "sync_armed"})
    runner = _runner(tmp_path, now, k, ex)
    fl = runner.app.app_cfg.fast_loop
    new = fl.model_copy(update={"card_neutral_trail_distance_mult": 0.5})
    hotconfig.patch_inplace(fl, new, ["card_neutral_trail_distance_mult"])
    runner.positions = [short_pos(now)]
    for _ in range(3):
        await runner._review_card_rules()
        await drain(runner)
    assert ex.closed == [] and len(ex.tighten_calls) == 1
    assert ex.extra == [("trail", 0.5, KEY)]


async def test_loop_stale_price_tightens_without_closing(tmp_path: Path, now: datetime) -> None:
    k, _, _, _ = make_keeper(now, NEUTRAL, cd=neutral_card(now))
    k._features = lambda _: None
    ex = TightenExecutor(short_pos(now), {"action": "hold", "why": "no_close_target_crossed"})
    runner = _runner(tmp_path, now, k, ex)
    runner.positions = [short_pos(now)]
    for _ in range(2):
        await runner._review_card_rules()
        await drain(runner)
    assert ex.closed == [] and ex.allow_close == [False, False]  # 没收紧成：下一轮再试


async def test_loop_close_from_old_executor_is_held_when_close_not_allowed(
    tmp_path: Path, now: datetime
) -> None:
    k, _, _, _ = make_keeper(now, NEUTRAL, cd=neutral_card(now))
    k._features = lambda _: None
    ex = TightenExecutor(short_pos(now), {"action": "close", "why": "not_server_managed"})
    runner = _runner(tmp_path, now, k, ex)
    runner.positions = [short_pos(now)]
    await runner._review_card_rules()
    await drain(runner)
    assert ex.closed == []


async def test_loop_close_mode_closes_without_tightening(tmp_path: Path, now: datetime) -> None:
    cfg = FAST.model_copy(update={"card_neutral_action": "close"})
    k, _, _, _ = make_keeper(now, cfg, cd=neutral_card(now))
    ex = TightenExecutor(short_pos(now), None)
    runner = _runner(tmp_path, now, k, ex)
    runner.positions = [short_pos(now)]
    assert await runner._review_card_rules() == {"pos-1"}
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)] and ex.tighten_calls == []


async def test_loop_default_hold_does_nothing(tmp_path: Path, now: datetime) -> None:
    k, _, _, _ = make_keeper(now, FAST, cd=neutral_card(now))
    ex = TightenExecutor(short_pos(now), None)
    runner = _runner(tmp_path, now, k, ex)
    runner.positions = [short_pos(now)]
    assert await runner._review_card_rules() == set()
    await drain(runner)
    assert ex.closed == [] and ex.tighten_calls == []


# ---------------------------------------------------------------- 执行层（真托管保护）


async def neutral_trail(h: TrailHarness, key: str, allow_close: bool = True) -> dict[str, Any]:
    return await h.adapter.tighten_stop(
        "pos-1",
        reason="作战卡转中性",
        buffer_bps=5.0,
        mark_gap_bps=10.0,
        profit_mode="trail",
        distance_mult=1.0,
        card_key=key,
        anchor_retry_s=0.0,
        allow_close=allow_close,
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_anchor_once_then_restart_and_opposite_card_keep_it(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    await h.protect()
    first = await neutral_trail(h, "BTCUSDT|t1|no_trade|neutral")
    assert first["action"] == "tightened" and first["mode"] == "trail"
    anchored = h.state().opposite_anchor_price
    assert anchored == (100.5 if side is Side.LONG else 99.5)
    h.mark = 101.0 if side is Side.LONG else 99.0
    h.restart_adapter()

    async def book(symbol: str) -> ExecutionBook:
        raise AssertionError("已锚定不再读盘口")

    h.exchange.execution_book = book  # type: ignore[attr-defined]
    again = await neutral_trail(h, "BTCUSDT|t1|no_trade|neutral")
    assert again["action"] == "kept" and again["why"] == "already_tightened"
    opposite = await h.trail(key="BTCUSDT|t2|short")  # 之后卡转反向：不重锚、不更松
    assert opposite["action"] == "kept" and opposite["anchor_price"] == anchored
    assert h.state().opposite_card_key == "BTCUSDT|t1|no_trade|neutral"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_losing_closes_unless_close_not_allowed(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    await h.protect()
    h.mark = 100.1 if side is Side.LONG else 99.9  # 扣费保本价挂不上 = 亏着 / 赚得不够
    held = await neutral_trail(h, "k|neutral", allow_close=False)
    assert held["action"] == "hold" and held["why"] == "no_close_target_crossed"
    assert h.state().opposite_anchor_price is None and h.critical() == []
    closed = await neutral_trail(h, "k|neutral")
    assert closed["action"] == "close" and closed["why"] == "target_crossed"
    assert h.critical() == [] and h.state().last_error is None


async def test_unreadable_evidence_without_close_alerts_but_holds(tmp_path: Path) -> None:
    h = TrailHarness(tmp_path, Side.LONG)
    await h.protect()

    async def spiky(symbol: str) -> ExecutionBook:
        return ExecutionBook(symbol, h.mark * 1.01, h.mark * 1.011, START, START)

    h.exchange.execution_book = spiky  # type: ignore[attr-defined]
    result = await neutral_trail(h, "k|neutral", allow_close=False)
    assert result["action"] == "hold" and result["why"] == "no_close_evidence_unavailable"
    alerts = h.critical()
    assert [a.level for a in alerts] == [c.AlertLevel.CRITICAL]
    assert "这次不平" in alerts[0].body
