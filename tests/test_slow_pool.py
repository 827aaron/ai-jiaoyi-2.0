"""作战卡 = 指令池：每个币的指令按自己的有效期到点消失，新一轮只替换开了会的币。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from tests.test_slow_commander import FakeEngine, entry, make, pool

H = timedelta(hours=1)


async def test_new_round_only_replaces_met_symbols(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT"])
    c, _, clock = make(tmp_path, eng, now)
    p = lambda t: pool(t, entry("AAUSDT"), entry("BBUSDT"))  # noqa: E731
    await c.refresh(p(now), "scheduled")
    clock[0] = now + H
    eng.picks = ["BBUSDT"]
    card = await c.refresh(p(clock[0]), "scheduled")
    by = {s.symbol: s for s in card.symbols}
    assert set(by) == {"AAUSDT", "BBUSDT"}  # 这轮没开会的 AA 原样留在池里
    assert by["AAUSDT"].issued_at == now and by["BBUSDT"].issued_at == now + H
    assert by["AAUSDT"].expires_at < by["BBUSDT"].expires_at  # type: ignore[operator]
    assert by["BBUSDT"].meeting_id is not None and by["BBUSDT"].meeting_id.startswith("BBUSDT-")


async def test_symbol_disappears_at_its_own_expiry_but_held_stays(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = make(tmp_path, eng, now)
    p = lambda t: pool(t, entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT"))  # noqa: E731
    await c.refresh(p(now), "scheduled")
    clock[0] = now + H
    eng.picks = ["CCUSDT"]
    card = await c.refresh(p(clock[0]), "scheduled")
    first_exp = min(s.expires_at for s in card.symbols if s.symbol != "CCUSDT")  # type: ignore[type-var]
    c.set_held({"AAUSDT": Side.LONG})
    after = first_exp + timedelta(minutes=1)
    cur = c.current(after)
    assert cur is not None
    by = {s.symbol: s for s in cur.symbols}
    assert set(by) == {"AAUSDT", "CCUSDT"}  # BB 到期消失；持仓的 AA 留着但不再开新仓
    assert by["AAUSDT"].allowed_signals == [] and by["CCUSDT"].allowed_signals
    c.set_held({})
    cur = c.current(after)
    assert cur is not None and {s.symbol for s in cur.symbols} == {"CCUSDT"}


async def test_pool_survives_restart_with_original_expiry(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "battle_card.json"
    eng = FakeEngine(["AAUSDT"])
    c, _, _ = make(tmp_path, eng, now, card_path=path)
    card = await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    c2, _, _ = make(tmp_path / "b", FakeEngine([]), now + timedelta(minutes=10), card_path=path)
    back = c2.current(now + timedelta(minutes=10))
    assert back is not None
    assert back.symbols[0].issued_at == now
    assert back.symbols[0].expires_at == card.symbols[0].expires_at


async def test_held_symbol_stays_after_whole_card_expires(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, _ = make(tmp_path, eng, now)
    card = await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    c.set_held({"AAUSDT": Side.LONG})
    late = card.expires_at + timedelta(hours=3)
    cur = c.current(late)
    assert cur is not None and cur.is_valid(late)
    assert [s.symbol for s in cur.symbols] == ["AAUSDT"]
    assert cur.symbols[0].allowed_signals == []  # 只盯不开
    c.set_held({})
    assert c.current(late) is None


async def test_manual_refresh_reruns_every_meeting_even_inside_min_interval(
    tmp_path: Path, now: datetime
) -> None:
    from datetime import timedelta as td

    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = make(tmp_path, eng, now, symbol_min_interval=td(minutes=30))
    p = lambda t: pool(t, entry("AAUSDT"), entry("BBUSDT"))  # noqa: E731
    await c.refresh(p(now), "scheduled")
    clock[0] = now + td(minutes=5)
    eng.analyzed.clear()
    await c.refresh(p(clock[0]), "scheduled")
    assert eng.analyzed == []  # 间隔内沿用上一张卡
    clock[0] = now + td(minutes=6)
    await c.refresh(p(clock[0]), "manual")
    assert sorted(eng.analyzed) == ["AAUSDT", "BBUSDT"]  # 手动：整条链真跑


async def test_market_and_commander_report_last_and_next_run(tmp_path: Path, now: datetime) -> None:
    c, log, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now)
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    st = log.read_status()
    m, cmd = st["market"], st["commander"]
    assert m.last_run_at == now and m.symbol_count == 1 and "挑了 1 个币上会" in m.headline
    assert cmd.last_run_at == now and cmd.next_run_at is not None


async def test_market_status_is_refreshed_by_every_heartbeat(tmp_path: Path, now: datetime) -> None:
    c, log, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now)
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    first = log.read_status()["market"]
    assert first.headline == "挑了 1 个币上会" and first.next_run_at is not None
    clock[0] = now + timedelta(minutes=20)
    c.heartbeat(clock[0])  # 两轮之间的空闲心跳：总盘会也要带新的 ts
    later = log.read_status()["market"]
    assert later.ts == clock[0] and later.last_run_at == now and later.next_run_at is not None


def test_screener_follows_meeting_interval_by_default(tmp_path: Path) -> None:
    from jiaoyi.contracts.risk_config import AppConfig  # noqa: F401
    from jiaoyi.core.config import with_profile

    for tier, mins in (("15m", 15), ("1h", 60), ("24h", 1440)):
        data = with_profile({"meeting_interval": tier})
        assert data["screener_interval_min"] == mins == data["slow_loop_interval_min"]
    custom = with_profile({"meeting_interval": "1h", "screener_interval_min": 30})
    assert custom["screener_interval_min"] == 30  # 自己写的优先


async def test_each_finished_symbol_writes_a_card_record_without_double_cost(
    tmp_path: Path, now: datetime
) -> None:
    from jiaoyi.contracts import Outcome, Role

    c, log, _ = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT"]), now)
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    cards = [
        r
        for r in log.read_day(now.date().isoformat())
        if r.role is Role.COMMANDER and r.card_id and r.outcome is Outcome.ACTION
    ]
    assert len(cards) == 3  # 每个币开完一条（面板马上显示）+ 整轮结束一条
    assert [r.output["symbols"] for r in cards[:2]] and "还在开会" in cards[0].summary
    assert cards[-1].summary.startswith("出作战卡")
    assert sum(r.cost_usd for r in cards) == pytest.approx(0.01)  # 总盘会花费只记一次
    assert set(cards[-1].output["symbols"]) == {"AAUSDT", "BBUSDT"}


# ---------------------------------------------------------------- 币安限流
async def test_rate_limit_pauses_round_and_reopens_after_unban(
    tmp_path: Path, now: datetime
) -> None:
    import time

    from jiaoyi.contracts import Health
    from jiaoyi.slow import binance_gate
    from jiaoyi.slow.commander import CardRefreshError

    binance_gate.reset()
    try:
        eng = FakeEngine(["AAUSDT", "BBUSDT"], fail={"AAUSDT", "BBUSDT"})
        c, log, clock = make(tmp_path, eng, now)
        binance_gate._banned_until = time.time() + 300  # 币安 418：封 5 分钟
        with pytest.raises(CardRefreshError):
            await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
        assert c._fails == 0 and c._retry_at is None  # 不算会议失败、不退避
        assert c.may_refresh(now) is False
        c.heartbeat(now)
        st = log.read_status()["commander"]
        assert (
            st.health is Health.DEGRADED
            and "币安限流，" in st.headline
            and "分钟后重开" in st.headline
        )
        assert c.refresh_reason(now, pool(now, entry("AAUSDT")), {}) is None
        binance_gate.reset()
        assert c._rl_until is not None
        clock[0] = c._rl_until + timedelta(seconds=1)
        eng.fail.clear()
        assert c.refresh_reason(clock[0], pool(clock[0], entry("AAUSDT")), {}) == "scheduled"
        await c.refresh(pool(clock[0], entry("AAUSDT"), entry("BBUSDT")), "scheduled")
        assert c._rl_until is None and c.rate_limit_note(clock[0]) is None
    finally:
        binance_gate.reset()


def test_gate_parses_retry_after_shares_ban_and_stops_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from jiaoyi.slow import binance_gate

    binance_gate.reset()
    calls: list[str] = []

    def fake_get(url: str, params: object = None, timeout: float = 10.0) -> httpx.Response:
        calls.append(url)
        return httpx.Response(418, headers={"Retry-After": "90"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    try:
        with pytest.raises(binance_gate.RateLimited) as ei:
            binance_gate.get("https://fapi.binance.com/x")
        assert 80 < ei.value.until - __import__("time").time() <= 91
        with pytest.raises(binance_gate.RateLimited):
            binance_gate.get("https://fapi.binance.com/y")  # 封禁期内不再打给币安
        assert calls == ["https://fapi.binance.com/x"]
        assert binance_gate.banned_until() > 0
    finally:
        binance_gate.reset()
