"""作战卡 = 指令池：每个币的指令按自己的有效期到点消失，新一轮只替换开了会的币。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

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
