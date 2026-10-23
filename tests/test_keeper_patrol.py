"""持仓管家（只能更安全、失败维持现状、连续确认）和巡逻兵（只平仓）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.gate.rule_model import RuleDecisionModel
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.patrol.rules import RulePatrol
from tests.conftest import load_example
from tests.fastloop_helpers import FAST, card, features


def position(now: datetime, **over: Any) -> c.PositionRecord:
    data = load_example("position_record")
    data.update(
        position_id="pos-1",
        symbol="BTCUSDT",
        side="long",
        status="open",
        entry_price=68000.0,
        stop_loss_price=67880.0,
        take_profit_price=68180.0,
        opened_at=(now - timedelta(minutes=5)).isoformat(),
    )
    data.update(over)
    return c.PositionRecord.model_validate(data)


def close_p(p: float) -> Any:
    def f(state: str, qs: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        return {
            "thesis_intact": {"yes": 1 - p, "no": p},
            "adverse_risk": {"low": 1 - p, "high": p},
            "action": {"hold": 1 - p, "close": p},
        }

    return f


def keeper(
    t: MockJevTransport,
    now: datetime,
    cd: c.BattleCard | None = None,
    feats: bool = True,
    **kw: Any,
) -> JevKeeper:
    m = JevDecisionModel(t, FAST, timeout_ms=200)
    snap = features(now)
    return JevKeeper(
        m,
        FAST,
        lambda s: snap if feats else None,
        lambda n: cd if cd is not None else card(now),
        **kw,
    )


def test_interfaces() -> None:
    assert isinstance(keeper(MockJevTransport(), datetime.now().astimezone()), i.Keeper)
    assert isinstance(RulePatrol(FAST), i.Patrol)


async def test_jev_failure_holds_and_resets_streak(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.7))
    k = keeper(t, now)
    v1 = await k.review(position(now), now)
    assert v1.action == "hold" and v1.consecutive == 1
    t.fail = TimeoutError()
    v2 = await k.review(position(now), now)
    assert v2.action == "hold" and "维持现状" in v2.reason
    t.fail = None
    v3 = await k.review(position(now), now)
    assert v3.action == "hold" and v3.consecutive == 1  # 失败打断了连续计数


async def test_two_consecutive_closes(now: datetime) -> None:
    k = keeper(MockJevTransport(answer=close_p(0.7)), now)
    assert (await k.review(position(now), now)).action == "hold"
    v = await k.review(position(now), now)
    assert v.action == "close" and v.consecutive == 2


async def test_strong_close_acts_immediately(now: datetime) -> None:
    v = await keeper(MockJevTransport(answer=close_p(0.9)), now).review(position(now), now)
    assert v.action == "close" and v.consecutive == 1


async def test_low_probability_resets(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.7))
    k = keeper(t, now)
    await k.review(position(now), now)
    t.answer = close_p(0.2)
    assert (await k.review(position(now), now)).action == "hold"
    t.answer = close_p(0.7)
    assert (await k.review(position(now), now)).consecutive == 1


async def test_card_flip_pushes_toward_close_only(now: datetime) -> None:
    flipped = card(now, bias="short")
    k = keeper(MockJevTransport(answer=close_p(0.1)), now, cd=flipped)
    v1 = await k.review(position(now), now)
    assert v1.action == "hold" and "方向" in v1.reason
    v2 = await k.review(position(now), now)
    assert v2.action == "close"


async def test_no_trade_or_missing_from_card_does_not_vote(now: datetime) -> None:
    """新卡只看不做 / 卡里没有这个币：不投平仓票，交给决策模型、止损、最长持仓时间。"""
    for cd in (card(now, bias="no_trade"), card(now, symbol="ETHUSDT")):
        k = keeper(MockJevTransport(answer=close_p(0.1)), now, cd=cd)
        for _ in range(3):
            v = await k.review(position(now), now)
            assert v.action == "hold" and v.consecutive == 1


async def test_blocked_symbol_still_votes_close(now: datetime) -> None:
    """下架公告封掉的币（卡里读成只看不做）照样投平仓票：要下架的币留仓更危险。"""
    m = JevDecisionModel(MockJevTransport(answer=close_p(0.1)), FAST, timeout_ms=200)
    snap = features(now)
    k = JevKeeper(
        m, FAST, lambda s: snap, lambda n: card(now, bias="no_trade"),
        blocked=lambda: {"BTCUSDT": "币安下架公告"},
    )  # fmt: skip
    v1 = await k.review(position(now), now)
    assert v1.action == "hold" and "下架" in v1.reason
    assert (await k.review(position(now), now)).action == "close"


async def test_no_features_holds(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.99))
    v = await keeper(t, now, feats=False).review(position(now), now)
    assert v.action == "hold" and t.calls == []


async def test_rule_model_hold_path_and_log(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    snap = features(now)
    k = JevKeeper(
        RuleDecisionModel(c.RunMode.PAPER),
        FAST,
        lambda s: snap,
        lambda n: card(now),
        RoleRecorder(c.Role.KEEPER, log),
    )
    v = await k.review(position(now), now)
    assert v.action in ("hold", "close")
    recs = log.read_day(now.date().isoformat())
    assert len(recs) == 1 and recs[0].role == c.Role.KEEPER and recs[0].position_id == "pos-1"


def _trade(t: datetime, px: float) -> c.Trade:
    return c.Trade(symbol="BTCUSDT", ts=t, price=px, qty=1.0, taker_side=c.Side.SHORT)


def test_patrol_shock_closes_once(now: datetime) -> None:
    p = RulePatrol(FAST)
    pos = [position(now)]
    assert p.check(_trade(now, 68000.0), pos) is None
    a = p.check(_trade(now + timedelta(seconds=20), 66600.0), pos)  # -2.06%
    assert a is not None and a.action == "close" and a.symbols == ["BTCUSDT"]
    assert p.check(_trade(now + timedelta(seconds=25), 66500.0), pos) is None  # 30 秒内不重复


def test_patrol_alert_marks_urgent(now: datetime) -> None:
    p = RulePatrol(FAST)
    pos = [position(now, stop_loss_price=None)]  # 没有止损价：按档位百分比
    p.check(_trade(now, 68000.0), pos)
    assert p.check(_trade(now + timedelta(seconds=10), 67250.0), pos) is None  # -1.1%
    assert p.urgent == {"pos-1"}


def test_patrol_ignores_favourable_moves_and_other_symbols(now: datetime) -> None:
    p = RulePatrol(FAST)
    pos = [position(now)]
    p.check(_trade(now, 68000.0), pos)
    assert p.check(_trade(now + timedelta(seconds=10), 70000.0), pos) is None
    assert p.check(_trade(now + timedelta(seconds=10), 66000.0), []) is None
    short = [position(now, side="short")]
    p2 = RulePatrol(FAST)
    p2.check(_trade(now, 68000.0), short)
    assert p2.check(_trade(now + timedelta(seconds=5), 69500.0), short) is not None


def test_patrol_window_forgets_old_highs(now: datetime) -> None:
    p = RulePatrol(FAST)
    pos = [position(now)]
    p.check(_trade(now, 70000.0), pos)
    # 2 分钟后的价格和 1 分钟窗口外的高点比，不算冲击
    assert p.check(_trade(now + timedelta(minutes=2), 68000.0), pos) is None


def test_patrol_lines_follow_the_stop(now: datetime) -> None:
    """冲击线 = 1.0 × 止损%，报警线 = 0.5 × 止损%；没有止损价才用档位百分比。"""
    p = RulePatrol(FAST)
    wide = [position(now, stop_loss_price=68000.0 * (1 - 0.03))]  # 止损 3%
    assert p.lines(wide[0]) == pytest.approx((1.5, 3.0))
    ethfi = position(now, stop_loss_price=68000.0 * (1 - 0.0102))  # 止损 1.02%：冲击线跟着收紧
    assert p.lines(ethfi) == pytest.approx((0.51, 1.02))
    bare = position(now, stop_loss_price=None)
    assert p.lines(bare) == (FAST.patrol_alert_pct_1m, FAST.patrol_shock_pct_1m)
    p.check(_trade(now, 68000.0), wide)
    # -2.06%：超过档位 2% 但没到止损 3%，只让管家复查
    assert p.check(_trade(now + timedelta(seconds=20), 66600.0), wide) is None
    assert "pos-1" in p.urgent
    a = p.check(_trade(now + timedelta(seconds=30), 65900.0), wide)  # -3.09%
    assert a is not None and "≥3.00%" in a.reason
