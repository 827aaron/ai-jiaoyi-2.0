"""预测员连续看反作战卡 → 事件 / 查询（慢环拿来开单币临时会）。"""

from __future__ import annotations

from datetime import datetime, timedelta

from jiaoyi import contracts as c
from jiaoyi.predictor.against import AgainstCard, AgainstCardWatch
from tests.fastloop_helpers import card


def fc(now: datetime, k: int, p_up: float, symbol: str = "BTCUSDT") -> c.Forecast:
    ts = now + timedelta(minutes=5 * k)
    return c.Forecast(
        symbol=symbol, ts=ts, horizon_min=120, price_low=1.0, price_high=2.0, p_up=p_up,
        model="Kronos-small", stale_after=ts + timedelta(minutes=10),
    )  # fmt: skip


def test_two_in_a_row_against_long_card_fires_once(now: datetime) -> None:
    cd = card(now)  # BTCUSDT 做多
    w = AgainstCardWatch(lambda t: cd)
    got: list[AgainstCard] = []
    w.subscribe(got.append)
    w.observe(fc(now, 0, 0.25))
    assert got == [] and w.current(now) == {}
    w.observe(fc(now, 0, 0.25))  # 同一条预测不重复算
    assert got == []
    w.observe(fc(now, 1, 0.0))
    assert [(a.symbol, a.streak, a.since) for a in got] == [("BTCUSDT", 2, now)]
    assert set(w.current(now)) == {"BTCUSDT"}
    w.observe(fc(now, 2, 0.25))  # 第 3 次不再重复发
    assert len(got) == 1 and w.current(now)["BTCUSDT"].streak == 3
    w.observe(fc(now, 3, 0.75))  # 转回同向：清掉
    assert w.current(now) == {}


def test_neutral_breaks_streak_and_short_card_needs_up(now: datetime) -> None:
    w = AgainstCardWatch(lambda t: card(now, bias="short"))
    w.observe(fc(now, 0, 0.75))
    w.observe(fc(now, 1, 0.5))  # 不算任何一边
    w.observe(fc(now, 2, 1.0))
    assert w.current(now) == {}
    w.observe(fc(now, 3, 0.75))
    assert w.current(now)["BTCUSDT"].card_bias == c.Bias.SHORT


def test_no_trade_both_missing_or_flipped_card_do_not_count(now: datetime) -> None:
    for cd in (card(now, bias="no_trade"), card(now, bias="both"), None):
        w = AgainstCardWatch(lambda t, cd=cd: cd)
        for k in range(3):
            w.observe(fc(now, k, 0.0))
        assert w.current(now) == {}
    holder = {"card": card(now)}
    w = AgainstCardWatch(lambda t: holder["card"])
    w.observe(fc(now, 0, 0.0))
    w.observe(fc(now, 1, 0.0))
    holder["card"] = card(now, bias="short")  # 卡上方向变了：旧状态不再算
    assert w.current(now) == {}
    w.observe(fc(now, 2, 1.0))
    assert w.current(now) == {}  # 新方向从头数
