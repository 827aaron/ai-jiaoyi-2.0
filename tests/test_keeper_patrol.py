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


async def _no_sleep(s: float) -> None:
    return None


class _Overloaded(Exception):
    """模拟 OpenRouter 529：TypeSafeInternalServerError … 529 system_overloaded。"""


class _FlakyTransport(MockJevTransport):
    """前 ``fails`` 次报 529，之后正常作答。"""

    def __init__(self, fails: int, **kw: Any) -> None:
        super().__init__(**kw)
        self.fails = fails

    async def evaluate(
        self, state: str, questions: Sequence[c.JevQuestion], timeout_s: float
    ) -> Any:
        if self.fails > 0:
            self.fails -= 1
            self.calls.append(state)
            raise _Overloaded("HTTP 529: system_overloaded")
        return await super().evaluate(state, questions, timeout_s)


async def test_busy_jev_retried_once_in_round(now: datetime) -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    t = _FlakyTransport(1, answer=close_p(0.2))
    v = await keeper(t, now, sleep=sleep).review(position(now), now)
    assert len(t.calls) == 2 and slept == [1.0]
    assert v.action == "hold" and "平仓概率 0.20" in v.reason


async def test_busy_jev_twice_holds_with_plain_text(now: datetime, tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    rec = RoleRecorder(c.Role.KEEPER, log)
    t = _FlakyTransport(2, answer=close_p(0.2))
    v = await keeper(t, now, sleep=_no_sleep, recorder=rec).review(position(now), now)
    assert len(t.calls) == 2
    assert v.reason == "Jev 服务方繁忙（已重试 1 次），本轮维持现状"
    r = log.read_day(now.date().isoformat())[-1]
    assert r.outcome is c.Outcome.ERROR and "529" not in r.summary
    assert "529" in str(r.output["jev"]["error"])  # 原始报错留在 output 里


async def test_other_jev_error_not_retried(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.2))
    t.fail = PermissionError("401 invalid key")
    v = await keeper(t, now, sleep=_no_sleep).review(position(now), now)
    assert len(t.calls) == 1 and v.reason == "Jev 调用出错，本轮维持现状"


def test_jev_error_kind() -> None:
    from jiaoyi.gate.jev import error_kind

    assert error_kind("Jev 调用失败：TypeSafeInternalServerError: … 529 HTTP 529") == "busy"
    assert error_kind("Jev 调用失败：APIStatusError: 503 Service Unavailable") == "busy"
    assert error_kind("Jev 超时（>1500ms）") == "timeout"
    assert error_kind("Jev 连续出错，冷却中（还剩 30 秒）") == "cooldown"
    assert error_kind("Jev 回复解析失败：缺少问题 action") == "other"


def test_interfaces() -> None:
    assert isinstance(keeper(MockJevTransport(), datetime.now().astimezone()), i.Keeper)
    assert isinstance(RulePatrol(FAST), i.Patrol)


async def test_jev_failure_holds_and_resets_streak(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.7))
    k = keeper(t, now, sleep=_no_sleep)
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


async def test_card_flip_directly_closes_without_model_gate(now: datetime) -> None:
    flipped = card(now, bias="short")
    k = keeper(MockJevTransport(answer=close_p(0.1)), now, cd=flipped)
    v1 = await k.review(position(now), now)
    assert v1.action == "close" and "方向" in v1.reason
    assert v1.jev_request_id is None and v1.consecutive == 1


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


def test_patrol_shock_skips_positions_in_profit(now: datetime, tmp_path: Path) -> None:
    """赚着钱冲高回吐一个止损距离，不全平，叫管家看；亏损或赚不过来回手续费照旧（Aaron 10-25）。"""
    log = JsonlDecisionLog(tmp_path)
    p = RulePatrol(FAST, RoleRecorder(c.Role.PATROL, log), fee_bps=5.0)
    pos = [position(now)]  # 开仓 68000，止损 67880（0.176%），冲击线 0.176%
    p.check(_trade(now, 68000.0), pos)
    p.check(_trade(now + timedelta(seconds=5), 69000.0), pos)  # 冲高
    # 回吐 0.29%（超过冲击线），现价 68800 仍比开仓价高 1.2%：不平，叫管家
    assert p.check(_trade(now + timedelta(seconds=10), 68800.0), pos) is None
    assert p.urgent == {"pos-1"}
    p.check(_trade(now + timedelta(seconds=15), 68790.0), pos)  # 还在盈利中，同一分钟不再写记录
    notes = [r for r in log.read_day(now.date().isoformat()) if "盈利中" in r.summary]
    assert len(notes) == 1 and notes[0].role == c.Role.PATROL
    # 赚的钱不到来回手续费（0.1%）：现价 68050 只高 0.07%，算没赚，照旧全平
    q = RulePatrol(FAST, fee_bps=5.0)
    q.check(_trade(now, 68400.0), pos)
    a = q.check(_trade(now + timedelta(seconds=5), 68050.0), pos)
    assert a is not None and a.action == "close"


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


class _Rows:
    def __init__(self) -> None:
        self.summaries: list[str] = []

    def record(self, now: datetime, outcome: c.Outcome, summary: str, **kw: Any) -> None:
        self.summaries.append(summary)


def _stale_keeper(
    now: datetime, t: MockJevTransport, snap: dict[str, Any]
) -> tuple[JevKeeper, _Rows]:
    rows = _Rows()
    fast = FAST.model_copy(update={"keeper_price_max_age_s": 20.0})
    model = JevDecisionModel(t, FAST, timeout_ms=200)
    k = JevKeeper(
        model,
        fast,
        lambda s: snap["f"],
        lambda n: card(now),
        rows,  # type: ignore[arg-type]
        review_interval_s=40.0,
    )
    return k, rows


async def test_stale_price_skips_the_round_without_closing(now: datetime) -> None:
    """管家价格过旧（keeper_price_max_age_s）：这一轮跳过、不问模型、不平仓；短暂断流
    （不超过 2 个复查间隔）连续计数不变，同一段断流只记一条。"""
    t = MockJevTransport(answer=close_p(0.7))
    snap = {"f": features(now)}
    k, rows = _stale_keeper(now, t, snap)
    v1 = await k.review(position(now), now)
    assert v1.action == "hold" and v1.consecutive == 1 and len(t.calls) == 1
    for sec in (40, 60):  # 行情停在 now
        v = await k.review(position(now), now + timedelta(seconds=sec))
        assert v.action == "hold" and f"行情已旧 {sec} 秒" in v.reason and len(t.calls) == 1
    assert sum("行情已旧" in x for x in rows.summaries) == 1
    later = now + timedelta(seconds=60)
    snap["f"] = features(later)
    v3 = await k.review(position(now), later)
    assert v3.action == "close" and v3.consecutive == 2  # 跳过的那几轮没打断连续计数


async def test_long_outage_drops_the_pre_outage_close_vote(now: datetime) -> None:
    """断流超过 2 个复查间隔：断流前那一票「该平」不能和恢复后的一票凑成连续两次。"""
    t = MockJevTransport(answer=close_p(0.7))
    snap = {"f": features(now)}
    k, rows = _stale_keeper(now, t, snap)
    assert (await k.review(position(now), now)).consecutive == 1
    later = now + timedelta(seconds=81)
    v = await k.review(position(now), later)
    assert v.action == "hold" and "行情已旧 81 秒" in v.reason
    snap["f"] = features(later)
    v2 = await k.review(position(now), later)
    assert v2.action == "hold" and v2.consecutive == 1
    # 恢复后再断：新的一段断流再记一条
    await k.review(position(now), later + timedelta(seconds=30))
    assert sum("行情已旧" in x for x in rows.summaries) == 2


async def test_stale_price_is_refreshed_before_the_review(now: datetime) -> None:
    """rest_warm_start：复查前行情层把旧了的快照从 REST 补新，这一轮照常判，不跳过。"""
    t = MockJevTransport(answer=close_p(0.7))
    later = now + timedelta(seconds=60)
    snap = {"f": features(now)}
    asked: list[str] = []

    async def refresh(sym: str) -> None:
        asked.append(sym)
        snap["f"] = features(later)

    k, _ = _stale_keeper(now, t, snap)
    k._refresh = refresh
    v = await k.review(position(now), later)
    assert asked == ["BTCUSDT"] and "行情已旧" not in v.reason and len(t.calls) == 1


async def test_stale_price_check_is_off_by_default(now: datetime) -> None:
    assert c.FastLoopConfig().keeper_price_max_age_s == 0
    t = MockJevTransport(answer=close_p(0.9))
    v = await keeper(t, now).review(position(now), now + timedelta(minutes=10))
    assert v.action == "close"  # 现状：不看行情多旧
