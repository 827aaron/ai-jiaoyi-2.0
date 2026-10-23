from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import (
    BattleCard,
    Bias,
    CardSymbol,
    Category,
    HardFilters,
    KeyLevel,
    LevelKind,
    Outcome,
    Range,
    Role,
    Side,
    SignalKind,
    SizeTier,
    SymbolPool,
    SymbolPoolEntry,
)
from jiaoyi.contracts.position import PositionRecord
from jiaoyi.core.config import load_risk_config
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.slow.commander import (
    Analysis,
    BattleCardCommander,
    CardRefreshError,
    Pick,
    PickResult,
    TierRules,
    clamp_to_risk,
)
from tests.conftest import ROOT

RISK = load_risk_config(ROOT / "config" / "risk.example.toml")


def entry(
    sym: str, *, pinned: bool = False, cat: Category = Category.ALT, tier: str | None = None
) -> SymbolPoolEntry:
    return SymbolPoolEntry(
        symbol=sym,
        category=cat,
        score=0.5,
        filters=HardFilters(
            quote_volume_24h_usdt=1e8, spread_bps=1, depth_usdt=1e6, listed_days=100, passed=True
        ),
        pinned=pinned,
        in_pool=True,
        tier=tier,  # type: ignore[arg-type]
    )


def pool(now: datetime, *syms: SymbolPoolEntry) -> SymbolPool:
    return SymbolPool(pool_id="p", ts=now, entries=list(syms))


def card_symbol(sym: str, **kw: object) -> CardSymbol:
    base: dict[str, object] = {
        "symbol": sym,
        "category": Category.ALT,
        "bias": Bias.LONG,
        "key_levels": [KeyLevel(price=100.0, kind=LevelKind.RESISTANCE)],
        "allowed_signals": [SignalKind.VOLUME_BREAKOUT],
        "size_tier_max": SizeTier.M,
        "notional_usdt": Range(min=100, max=500),
        "leverage": Range(min=1, max=3),
        "stop_atr": Range(min=1, max=2),
        "take_profit_r": Range(min=1.5, max=3),
        "reason": "测试",
    }
    return CardSymbol(**{**base, **kw})  # type: ignore[arg-type]


class FakeEngine:
    def __init__(self, picks: list[str], fail: set[str] | None = None) -> None:
        self.picks, self.fail = picks, fail or set()
        self.analyzed: list[str] = []
        self.running = self.peak = 0

    async def pick(
        self, candidates: Sequence[SymbolPoolEntry], max_n: int, previous: BattleCard | None
    ) -> PickResult:
        return PickResult("区间震荡", [Pick(s, "r") for s in self.picks][:max_n], 0.01)

    async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        self.analyzed.append(e.symbol)
        if e.symbol in self.fail:
            raise RuntimeError("deepseek 超时")
        return Analysis(card_symbol(e.symbol), 0.02)


def make(
    tmp_path: Path, engine: FakeEngine, now: datetime, **kw: object
) -> tuple[BattleCardCommander, JsonlDecisionLog, list[datetime]]:
    clock = [now]
    log = JsonlDecisionLog(tmp_path)
    c = BattleCardCommander(engine, RISK, log, clock=lambda: clock[0], **kw)  # type: ignore[arg-type]
    return c, log, clock


async def test_refresh_builds_card_with_cost_and_logs(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT", "NOPEUSDT"])
    c, log, _ = make(tmp_path, eng, now)
    assert c.current(now) is None
    card = await c.refresh(
        pool(now, entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT")), "scheduled"
    )
    assert [s.symbol for s in card.symbols] == ["AAUSDT", "BBUSDT"]  # 池外的 NOPE 被丢
    assert card.llm_cost_usd == pytest.approx(0.05)
    assert c.current(now) == card and c.current(now + timedelta(hours=3)) is None
    recs = log.read_day(now.date().isoformat())
    assert {r.role for r in recs} == {Role.COMMANDER}
    assert len(recs) == 3  # 两个标的 + 一张卡
    assert sum(r.cost_usd for r in recs) == pytest.approx(0.05)
    st = log.read_status()["commander"]
    assert st.today.cost_usd == pytest.approx(0.05)  # 与 card.llm_cost_usd 一致


async def test_picks_are_reported_before_symbol_meetings(tmp_path: Path, now: datetime) -> None:
    """总盘会一挑完就回调名单（含置顶），主循环马上订行情，不等作战卡。"""
    eng = FakeEngine(["BBUSDT", "NOPEUSDT"])
    c, _, _ = make(tmp_path, eng, now)
    got: list[list[str]] = []
    c.set_on_picks(got.append)
    await c.refresh(pool(now, entry("AAUSDT", pinned=True), entry("BBUSDT")), "scheduled")
    assert got == [["AAUSDT", "BBUSDT"]]


async def test_pinned_always_analyzed_and_pick_max(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["BBUSDT", "CCUSDT", "DDUSDT"])
    c, _, _ = make(tmp_path, eng, now, pick_max=2)
    card = await c.refresh(
        pool(now, entry("AAUSDT", pinned=True), entry("BBUSDT"), entry("CCUSDT"), entry("DDUSDT")),
        "scheduled",
    )
    assert [s.symbol for s in card.symbols] == ["AAUSDT", "BBUSDT"]


async def test_concurrency_is_bounded(tmp_path: Path, now: datetime) -> None:
    syms = [f"S{i}USDT" for i in range(10)]
    eng = FakeEngine(syms)
    c, _, _ = make(tmp_path, eng, now, concurrency=4)
    await c.refresh(pool(now, *(entry(s) for s in syms)), "scheduled")
    assert eng.peak == 4


async def test_partial_failure_omits_symbol_total_failure_raises(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"], fail={"BBUSDT"})
    c, _, _ = make(tmp_path, eng, now)
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    card = await c.refresh(p, "scheduled")
    assert [s.symbol for s in card.symbols] == ["AAUSDT"]

    eng2 = FakeEngine(["AAUSDT"], fail={"AAUSDT"})
    c2, log2, _ = make(tmp_path / "b", eng2, now)
    with pytest.raises(CardRefreshError):
        await c2.refresh(pool(now, entry("AAUSDT")), "scheduled")
    assert c2.current(now) is None  # 故障即关闭：没有卡
    assert log2.read_status()["commander"].health.value == "degraded"
    assert any(r.outcome is Outcome.ERROR for r in log2.read_day(now.date().isoformat()))


async def test_failed_refresh_keeps_old_card_until_expiry(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT"])
    c, _, clock = make(tmp_path, eng, now)
    p = pool(now, entry("AAUSDT"))
    old = await c.refresh(p, "scheduled")
    eng.fail.add("AAUSDT")
    clock[0] = now + timedelta(minutes=30)
    c.notify_news("AAUSDT")  # 大消息才会在间隔内重开会
    with pytest.raises(CardRefreshError):
        await c.refresh(p, "news")
    assert c.current(clock[0]) == old


async def test_version_increments_within_day(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT"])
    c, _, clock = make(tmp_path, eng, now)
    p = pool(now, entry("AAUSDT"))
    first = await c.refresh(p, "scheduled")
    clock[0] = now + timedelta(hours=1)
    second = await c.refresh(p, "scheduled")
    assert (first.version, second.version) == (1, 2) and first.card_id == second.card_id


async def test_out_of_bounds_meeting_output_means_no_conclusion(
    tmp_path: Path, now: datetime
) -> None:
    class Greedy(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
            if e.symbol == "BBUSDT":
                return Analysis(card_symbol(e.symbol, leverage=Range(min=1, max=50)), 0.0)
            return await super().analyze(e, regime)

    c, _, _ = make(tmp_path, Greedy(["AAUSDT", "BBUSDT"]), now)
    card = await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    assert [s.symbol for s in card.symbols] == ["AAUSDT"]


async def test_symbol_meeting_interval_reuses_previous_instruction(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = make(tmp_path, eng, now)
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    assert eng.analyzed == ["AAUSDT", "BBUSDT"]

    clock[0] = now + timedelta(minutes=90)  # 不足 2 小时：不再开会，沿用
    card = await c.refresh(p, "scheduled")
    assert eng.analyzed == ["AAUSDT", "BBUSDT"] and len(card.symbols) == 2
    assert card.llm_cost_usd == pytest.approx(0.01)  # 只剩总盘会的花费

    c.notify_news("AAUSDT")  # 大消息：只给 AA 重新开会
    clock[0] = now + timedelta(minutes=100)
    await c.refresh(p, "news")
    assert eng.analyzed == ["AAUSDT", "BBUSDT", "AAUSDT"]

    clock[0] = now + timedelta(hours=3, minutes=50)  # 超过间隔：BB 也重开
    await c.refresh(p, "scheduled")
    assert eng.analyzed[-2:] == ["AAUSDT", "BBUSDT"]


def test_clamp_to_risk() -> None:
    cs = card_symbol(
        "AAUSDT", notional_usdt=Range(min=1e6, max=2e6), leverage=Range(min=20, max=50)
    )
    # 只按权益倍数时慢环不夹名义价值（不知道实时权益），保镖下单时封顶
    assert clamp_to_risk(cs, RISK).notional_usdt == cs.notional_usdt
    topped = RISK.model_copy(update={"max_notional_per_trade_usdt": 2000.0})
    out = clamp_to_risk(cs, topped)
    assert out.notional_usdt.max == 2000.0
    assert out.notional_usdt.min <= out.notional_usdt.max
    assert out.leverage.max == RISK.max_leverage and out.leverage.min <= out.leverage.max


def test_clamp_zero_category_cap_forces_no_trade() -> None:
    risk = RISK.model_copy(
        update={"category_exposure_x": {**RISK.category_exposure_x, Category.ALT: 0.0}}
    )
    out = clamp_to_risk(card_symbol("AAUSDT"), risk)
    assert out.bias is Bias.NO_TRADE and out.allowed_signals == []


async def test_refresh_reasons(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT"])
    c, _, clock = make(tmp_path, eng, now, ttl=timedelta(hours=2))
    p = pool(now, entry("AAUSDT"))
    assert c.refresh_reason(now, p, {}) == "scheduled"  # 没卡
    await c.refresh(p, "scheduled")
    t = now + timedelta(minutes=10)
    clock[0] = t
    assert c.refresh_reason(t, p, {"AAUSDT": 99.0}) is None
    assert c.refresh_reason(t, p, {"AAUSDT": 101.0}) == "key_level_break"
    c._level_broken.clear()  # 突破记录在下次开会前一直有效
    c.notify_news("AAUSDT")
    assert c.refresh_reason(t, p, {}) == "news"
    p2 = pool(now, entry("AAUSDT"), entry("NEWUSDT"))
    c._news.clear()
    assert c.refresh_reason(t, p2, {}) == "new_entrant"
    late = now + timedelta(hours=1, minutes=55)
    assert c.refresh_reason(late, p, {}) == "scheduled"  # 临近到期
    assert c.refresh_reason(now + timedelta(minutes=2), p2, {}) is None  # 防抖


def test_ttl_must_be_positive(tmp_path: Path, now: datetime) -> None:
    with pytest.raises(ValueError):
        make(tmp_path, FakeEngine([]), now, ttl=timedelta(0))
    make(tmp_path, FakeEngine([]), now, ttl=timedelta(minutes=35))  # 15m 档


async def test_wrong_category_from_engine_is_rejected(tmp_path: Path, now: datetime) -> None:
    class LyingEngine(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
            return Analysis(card_symbol(e.symbol, category=Category.MAJOR), 0.0)

    c, _, _ = make(tmp_path, LyingEngine(["AAUSDT", "BBUSDT"]), now)
    with pytest.raises(CardRefreshError):  # 全部被拒 = 不出卡
        await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")

    # 其中一个正确、一个说谎：卡里只有正确的，且分组是候选池的
    class HalfLying(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
            cat = Category.MAJOR if e.symbol == "BBUSDT" else e.category
            return Analysis(card_symbol(e.symbol, category=cat), 0.0)

    c2, _, _ = make(tmp_path / "b", HalfLying(["AAUSDT", "BBUSDT"]), now)
    card = await c2.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    assert [(s.symbol, s.category) for s in card.symbols] == [("AAUSDT", Category.ALT)]


async def test_block_symbol_turns_card_entry_into_no_trade_immediately(
    tmp_path: Path, now: datetime
) -> None:
    c, log, _ = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT"]), now)
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    c.block_symbol("AAUSDT", "币安公告：将下架 AAUSDT")
    c.block_symbol("AAUSDT", "重复")  # 幂等，只记一次
    card = c.current(now)
    assert card is not None
    aa, bb = card.for_symbol("AAUSDT"), card.for_symbol("BBUSDT")
    assert aa is not None and aa.bias is Bias.NO_TRADE and aa.allowed_signals == []
    assert bb is not None and bb.bias is Bias.LONG
    blocks = [r for r in log.read_day(now.date().isoformat()) if "禁止新开仓" in r.summary]
    assert len(blocks) == 1
    c.unblock_symbol("AAUSDT")
    again = c.current(now)
    assert again is not None and again.for_symbol("AAUSDT") is not None
    assert again.for_symbol("AAUSDT").bias is Bias.LONG  # type: ignore[union-attr]


async def test_failures_back_off_and_success_resets(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT"], fail={"AAUSDT"})
    c, _, clock = make(tmp_path, eng, now)
    p = pool(now, entry("AAUSDT"))
    waits = []
    for _ in range(5):
        assert c.may_refresh(clock[0]) and c.refresh_reason(clock[0], p, {}) == "scheduled"
        with pytest.raises(CardRefreshError):
            await c.refresh(p, "scheduled")
        t0 = clock[0]
        assert not c.may_refresh(t0) and c.refresh_reason(t0, p, {}) is None
        step = 0
        while not c.may_refresh(clock[0] + timedelta(minutes=step)):
            step += 1
        waits.append(step)
        clock[0] = t0 + timedelta(minutes=step)
    assert waits == [5, 10, 30, 30, 30]  # 5→10→30 分钟，之后保持
    eng.fail.clear()
    await c.refresh(p, "scheduled")
    assert c.may_refresh(clock[0])  # 成功后清零


async def test_daily_budget_stops_meetings_and_notifies_once(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = make(tmp_path, eng, now, daily_cost_limit_usd=0.04)
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    assert c.may_refresh(clock[0])
    await c.refresh(p, "scheduled")  # 0.01 + 2×0.02 = 0.05 ≥ 0.04
    clock[0] += timedelta(minutes=10)
    assert not c.may_refresh(clock[0]) and c.refresh_reason(clock[0], p, {}) is None
    with pytest.raises(CardRefreshError, match="上限"):
        await c.refresh(p, "scheduled")
    notice = c.take_notice()
    assert notice is not None and "日上限" in notice
    assert c.take_notice() is None  # 每天只提示一次
    clock[0] += timedelta(days=1)  # 新的一天（曼谷）清零
    assert c.may_refresh(clock[0])


async def test_failed_meetings_and_pick_are_charged(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.slow.commander import MeetingError

    class Eng(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
            if e.symbol == "AAUSDT":
                raise MeetingError("结构化输出失败", cost_usd=0.03)  # 真实 usage
            raise RuntimeError("超时")  # 拿不到 usage → 估算

    eng = Eng(["AAUSDT", "BBUSDT"])
    c, log, _ = make(tmp_path, eng, now, failed_meeting_cost_usd=0.07)
    with pytest.raises(CardRefreshError):
        await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    # 选币 0.01（所有标的都失败也要计）+ 真实 0.03 + 估算 0.07
    assert log.read_status()["commander"].today.cost_usd == pytest.approx(0.11)


async def test_invalid_conclusion_still_charges_its_cost(tmp_path: Path, now: datetime) -> None:
    class Eng(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
            return Analysis(card_symbol("OTHERUSDT"), 0.04)  # 返回了别的标的 → 不合格

    c, _, _ = make(tmp_path, Eng(["AAUSDT"]), now)
    with pytest.raises(CardRefreshError):
        await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    assert c._stats.cost_usd == pytest.approx(0.05)  # 选币 0.01 + 废结论 0.04


async def test_daily_cost_survives_restart(tmp_path: Path, now: datetime) -> None:
    f = tmp_path / "data" / "slow_budget.json"
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, budget_path=f)
    await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    spent = c._stats.cost_usd
    assert spent > 0 and f.is_file()
    # 重启：新实例读回当天花费，日上限不清零
    c2, _, _ = make(
        tmp_path, FakeEngine(["AAUSDT"]), now, budget_path=f, daily_cost_limit_usd=spent
    )
    assert c2._stats.cost_usd == pytest.approx(spent)
    assert c2._stats.calls == c._stats.calls > 0  # 调用次数也恢复，面板不清零
    assert not c2.may_refresh(now)
    # 第二天不沿用
    c3, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now + timedelta(days=1), budget_path=f)
    assert c3._stats.cost_usd == 0
    # 坏文件不影响启动
    f.write_text("not json", encoding="utf-8")
    c4, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, budget_path=f)
    assert c4._stats.cost_usd == 0


def test_clamp_raises_too_tight_stop_into_guard_band() -> None:
    from jiaoyi.contracts import Range
    from jiaoyi.slow.commander import StopScale

    band = StopScale(15, 0.5, 4.0)
    tight = clamp_to_risk(card_symbol("AAUSDT", stop_atr=Range(min=0.2, max=0.4)), RISK, band)
    assert tight.stop_atr.min >= 0.5  # 不会被保镖的 min_stop_atr 整张拒掉
    wide = clamp_to_risk(card_symbol("AAUSDT", stop_atr=Range(min=3, max=9)), RISK, band)
    assert wide.stop_atr.max == 4.0
    ok = clamp_to_risk(card_symbol("AAUSDT", stop_atr=Range(min=1, max=2)), RISK, band)
    assert (ok.stop_atr.min, ok.stop_atr.max) == (1, 2)


def test_clamp_raises_too_low_take_profit_into_guard_band() -> None:
    from jiaoyi.contracts import Range
    from jiaoyi.slow.commander import StopScale

    band = StopScale(15, 0.5, 4.0, 1.0, 6.0)
    low = clamp_to_risk(card_symbol("AAUSDT", take_profit_r=Range(min=0.3, max=0.8)), RISK, band)
    assert low.take_profit_r.min >= 1.0  # 不会被保镖的 min_tp_r 整张拒掉
    high = clamp_to_risk(card_symbol("AAUSDT", take_profit_r=Range(min=3, max=12)), RISK, band)
    assert high.take_profit_r.max == 6.0
    ok = clamp_to_risk(card_symbol("AAUSDT", take_profit_r=Range(min=1.5, max=3)), RISK, band)
    assert (ok.take_profit_r.min, ok.take_profit_r.max) == (1.5, 3)


class _SlowSecond(FakeEngine):
    """第二个币开会时被打断（模拟重启）：此时第一个币的会早该落盘。"""

    async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
        if e.symbol == "BBUSDT":
            await asyncio.sleep(10)
        return await super().analyze(e, regime)


async def test_each_finished_meeting_publishes_and_saves_card(
    tmp_path: Path, now: datetime
) -> None:
    path = tmp_path / "battle_card.json"
    c, _, _ = make(tmp_path, _SlowSecond(["AAUSDT", "BBUSDT"]), now, card_path=path)
    task = asyncio.create_task(c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动"))
    await asyncio.sleep(0.3)
    mid = c.current(now)
    assert mid is not None and [s.symbol for s in mid.symbols] == ["AAUSDT"]
    # 假装此刻进程被杀：新进程从磁盘捡回已开完的那张
    c2, _, _ = make(tmp_path, FakeEngine([]), now, card_path=path)
    back = c2.current(now)
    assert back is not None and [s.symbol for s in back.symbols] == ["AAUSDT"]
    assert back.version == mid.version
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_version_bumps_with_every_published_meeting(tmp_path: Path, now: datetime) -> None:
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT"]), now)
    card = await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动")
    assert card.version == 2  # 两场会各出一次：快环拍板中途换了卡能认出来
    assert {s.symbol for s in card.symbols} == {"AAUSDT", "BBUSDT"}


async def test_expired_saved_card_is_not_restored(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "battle_card.json"
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, card_path=path)
    await c.refresh(pool(now, entry("AAUSDT")), "启动")
    late = now + timedelta(hours=3)
    c2, _, _ = make(tmp_path, FakeEngine([]), late, card_path=path)
    assert c2.current(late) is None


async def test_restored_card_is_clamped_to_current_risk(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "battle_card.json"
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, card_path=path)
    await c.refresh(pool(now, entry("AAUSDT")), "启动")
    tighter = RISK.model_copy(update={"max_leverage": 1})
    c2 = BattleCardCommander(
        FakeEngine([]), tighter, JsonlDecisionLog(tmp_path), clock=lambda: now, card_path=path
    )
    back = c2.current(now)
    assert back is not None
    assert back.symbols[0].leverage.max <= 1.0


class _SlowBB(FakeEngine):
    def __init__(self, picks: list[str]) -> None:
        super().__init__(picks)
        self.bb_delay = 0.0

    async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
        if e.symbol == "BBUSDT":
            await asyncio.sleep(self.bb_delay)
        return await super().analyze(e, regime)


async def test_pending_symbols_keep_previous_instruction_during_round(
    tmp_path: Path, now: datetime
) -> None:
    eng = _SlowBB(["AAUSDT", "BBUSDT"])
    c, _, clock = make(tmp_path, eng, now, symbol_min_interval=timedelta(minutes=30))
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "启动")
    clock[0] = now + timedelta(minutes=115)
    eng.bb_delay = 10
    task = asyncio.create_task(c.refresh(p, "到期前"))
    await asyncio.sleep(0.3)
    mid = c.current(clock[0])
    assert mid is not None
    assert {s.symbol for s in mid.symbols} == {"AAUSDT", "BBUSDT"}  # BB 还在开会，旧指令不能消失
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_held_symbol_stays_on_card_watch_only_when_failed_or_outside_pool(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT", "CCUSDT"])
    c, _, clock = make(tmp_path, eng, now, symbol_min_interval=timedelta(minutes=30))
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT")), "启动")
    c.set_held({"AAUSDT": Side.LONG, "BBUSDT": Side.LONG})
    clock[0] = now + timedelta(minutes=125)  # 旧指令到期了
    eng.picks, eng.fail = ["AAUSDT", "CCUSDT"], {"AAUSDT"}  # AA 开会失败，BB 已不在候选池里
    card = await c.refresh(pool(clock[0], entry("AAUSDT"), entry("CCUSDT")), "到期前")
    by = {s.symbol: s for s in card.symbols}
    for sym in ("AAUSDT", "BBUSDT"):
        assert by[sym].bias is Bias.LONG and by[sym].allowed_signals == []  # 不开新仓，方向不动
    assert by["CCUSDT"].bias is Bias.LONG and by["CCUSDT"].allowed_signals
    # 持仓管家不能因为这张卡平掉它们
    for sym in ("AAUSDT", "BBUSDT"):
        pos = PositionRecord.model_construct(symbol=sym, side=Side.LONG)
        assert JevKeeper._card_rule(pos, card, clock[0]) is None


async def test_held_symbol_is_forced_into_the_round(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT"])  # 军师只挑了 AA，但 BB 手上有仓
    c, _, _ = make(tmp_path, eng, now)
    c.set_held({"BBUSDT": Side.SHORT})
    card = await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动")
    assert set(eng.analyzed) == {"AAUSDT", "BBUSDT"}
    assert {s.symbol for s in card.symbols} == {"AAUSDT", "BBUSDT"}


async def test_held_symbol_without_any_old_instruction_gets_synthesized_hold_only(
    tmp_path: Path, now: datetime
) -> None:
    """切档重启（旧卡故意丢掉）/ 卡文件丢了 / 旧卡本来没有它：按持仓方向合成一条只盯不开。"""
    eng = FakeEngine(["AAUSDT"], fail={"BBUSDT"})
    c, _, _ = make(tmp_path, eng, now)
    c.set_held({"BBUSDT": Side.SHORT})
    card = await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动")
    bb = card.for_symbol("BBUSDT")
    assert bb is not None and bb.bias is Bias.SHORT and bb.allowed_signals == []
    pos = PositionRecord.model_construct(symbol="BBUSDT", side=Side.SHORT)
    assert JevKeeper._card_rule(pos, card, now) is None
    # 池外的持仓币也一样
    c2, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now)
    c2.set_held({"ZZUSDT": Side.LONG})
    card2 = await c2.refresh(pool(now, entry("AAUSDT")), "启动")
    zz = card2.for_symbol("ZZUSDT")
    assert zz is not None and zz.bias is Bias.LONG and zz.allowed_signals == []


async def test_failed_symbol_without_position_is_dropped(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = make(tmp_path, eng, now, symbol_min_interval=timedelta(minutes=30))
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动")
    clock[0] = now + timedelta(minutes=125)  # 旧指令到期了
    eng.fail = {"AAUSDT"}
    card = await c.refresh(pool(clock[0], entry("AAUSDT"), entry("BBUSDT")), "到期前")
    assert {s.symbol for s in card.symbols} == {"BBUSDT"}


async def test_saved_card_not_restored_when_stop_scale_changed(
    tmp_path: Path, now: datetime
) -> None:
    from jiaoyi.slow.commander import StopScale

    path = tmp_path / "battle_card.json"
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, card_path=path)
    await c.refresh(pool(now, entry("AAUSDT")), "启动")
    c2 = BattleCardCommander(
        FakeEngine([]),
        RISK,
        JsonlDecisionLog(tmp_path),
        clock=lambda: now,
        card_path=path,
        stop_scale=StopScale(period_min=60),
    )
    assert c2.current(now) is None


async def test_heartbeat_reports_progress_while_meeting(tmp_path: Path, now: datetime) -> None:
    eng = _SlowBB(["AAUSDT", "BBUSDT"])
    eng.bb_delay = 10
    c, log, _ = make(tmp_path, eng, now)
    c._beat_s = 0.05
    task = asyncio.create_task(c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动"))
    await asyncio.sleep(0.4)
    st = log.read_status()[Role.COMMANDER.value]
    assert st.headline == "开会中：1/2"  # AA 开完了，BB 还在开
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_card_file_exists_after_every_finished_meeting(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "data" / "battle_card.json"  # 目录一开始不存在
    eng = _SlowBB(["AAUSDT", "BBUSDT"])
    eng.bb_delay = 10
    c, _, _ = make(tmp_path, eng, now, card_path=path)
    task = asyncio.create_task(c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动"))
    await asyncio.sleep(0.3)
    assert path.is_file()  # AA 开完、BB 还在开：文件已经在了
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [s["symbol"] for s in saved["card"]["symbols"]] == ["AAUSDT"]
    assert saved["stop"]["period_min"] == 15
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_save_failure_is_logged_and_does_not_fail_the_meeting(
    tmp_path: Path, now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("我是个文件，目录建不出来", encoding="utf-8")
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, card_path=blocker / "battle_card.json")
    with caplog.at_level("ERROR"):
        card = await c.refresh(pool(now, entry("AAUSDT")), "启动")
    assert [s.symbol for s in card.symbols] == ["AAUSDT"]  # 会照常算成功
    assert "作战卡写不进" in caplog.text


def test_heartbeat_when_idle_and_after_restore(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "battle_card.json"
    eng = FakeEngine(["AAUSDT"])
    c, log, _ = make(tmp_path, eng, now, card_path=path)
    c.heartbeat(now)
    assert log.read_status()[Role.COMMANDER.value].headline == "还没有作战卡，等第一轮会议"
    asyncio.run(c.refresh(pool(now, entry("AAUSDT")), "启动"))
    c.heartbeat(now)
    head = log.read_status()[Role.COMMANDER.value].headline
    assert head.startswith("当前卡 v1") and "下次会议" in head
    # 重启后立刻上报“已恢复卡 vN，下次会议 HH:MM”
    c2, log2, _ = make(tmp_path / "other", FakeEngine([]), now, card_path=path)
    c2.heartbeat(now)
    head2 = log2.read_status()[Role.COMMANDER.value].headline
    assert head2.startswith("已恢复卡 v1") and "下次会议" in head2
    later = now + timedelta(hours=3)
    c2.heartbeat(later)
    assert "过期" in log2.read_status()[Role.COMMANDER.value].headline


async def test_in_flight_meeting_cost_is_reserved_and_saved_before_it_finishes(
    tmp_path: Path, now: datetime
) -> None:
    """强退（重启）打断的会议也要记进当日预算：开会前按估算预记并落盘，正常结束再改成实际值。"""
    bpath = tmp_path / "slow_budget.json"
    eng = _SlowBB(["AAUSDT", "BBUSDT"])
    eng.bb_delay = 10
    c, _, _ = make(tmp_path, eng, now, budget_path=bpath, failed_meeting_cost_usd=0.05)
    task = asyncio.create_task(c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动"))
    await asyncio.sleep(0.3)
    # 选币 0.01 + AA 实际 0.02 + BB 还在开（当时没有完成场次可参考，预记 0.05）
    saved = json.loads(bpath.read_text(encoding="utf-8"))["cost_usd"]
    assert saved == pytest.approx(0.01 + 0.02 + 0.05, abs=1e-6)
    task.cancel()  # 强退：预记的钱不退
    with pytest.raises(asyncio.CancelledError):
        await task
    c2 = BattleCardCommander(
        FakeEngine([]), RISK, JsonlDecisionLog(tmp_path), clock=lambda: now, budget_path=bpath
    )
    assert c2._stats.cost_usd == pytest.approx(0.08, abs=1e-6)  # 重启后日花费没漏


async def test_reservation_is_replaced_by_actual_cost_when_meeting_ends(
    tmp_path: Path, now: datetime
) -> None:
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT"]), now, failed_meeting_cost_usd=0.5)
    card = await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "启动")
    assert c._stats.cost_usd == pytest.approx(card.llm_cost_usd)  # 0.01 + 2×0.02，没有多记估算


def test_timeout_reason_is_not_empty() -> None:
    from jiaoyi.slow.commander import BattleCardCommander

    c = BattleCardCommander.__new__(BattleCardCommander)
    c._symbol_timeout_s = 900.0
    assert "900" in c._why(TimeoutError())
    assert c._why(ValueError("x")) == "x"


async def test_timeout_charges_real_partial_cost_and_aborts(tmp_path: Path, now: datetime) -> None:
    class Eng(FakeEngine):
        aborted: list[str]

        def __init__(self) -> None:
            super().__init__(["AAUSDT"])
            self.aborted = []

        async def analyze(self, e: SymbolPoolEntry, regime: str) -> Analysis:
            await asyncio.sleep(5)  # 超过 symbol_timeout_s
            raise AssertionError("不该走到这里")

        def partial_cost(self, key: str) -> float | None:
            return 0.123 if key == "AAUSDT" else None

        def abort(self, key: str) -> None:
            self.aborted.append(key)

    eng = Eng()
    c, _, _ = make(tmp_path, eng, now, failed_meeting_cost_usd=0.07, symbol_timeout_s=0.05)
    with pytest.raises(CardRefreshError):
        await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    assert "AAUSDT" in eng.aborted
    # 选币 0.01 + 超时会议已发生的真实花费 0.123（不是固定的 0.07）
    assert c._stats.cost_usd == pytest.approx(0.133)


def _tiered_pool(now: datetime) -> SymbolPool:
    return pool(
        now,
        entry("AAUSDT", tier="head"),
        entry("BBUSDT", tier="mid"),
        entry("CCUSDT", tier="mid"),
        entry("DDUSDT", tier="mid"),
        entry("EEUSDT", tier="tail"),
    )


async def test_tiers_mid_daily_pick_size_cap_and_tail_watch_only(
    tmp_path: Path, now: datetime
) -> None:
    rules = TierRules(enabled=True, mid_daily_pick=2, mid_size_max=SizeTier.S)
    path = tmp_path / "mid_tier.json"
    eng = FakeEngine(["AAUSDT", "BBUSDT", "CCUSDT", "DDUSDT", "EEUSDT"])
    c, _, _ = make(tmp_path, eng, now, tiers=rules, tier_path=path)
    card = await c.refresh(_tiered_pool(now), "scheduled")
    # 头部 AA 照常；中间档当天最多挑 2 个（BB、CC），DD 当天不开；尾部 EE 不开会、卡上没有
    assert [s.symbol for s in card.symbols] == ["AAUSDT", "BBUSDT", "CCUSDT"]
    sizes = {s.symbol: s.size_tier_max for s in card.symbols}
    assert sizes == {"AAUSDT": SizeTier.M, "BBUSDT": SizeTier.S, "CCUSDT": SizeTier.S}
    assert eng.analyzed.count("DDUSDT") == 0 and "EEUSDT" not in eng.analyzed
    assert json.loads(path.read_text(encoding="utf-8"))["mid"] == ["BBUSDT", "CCUSDT"]
    # 同一天不再重挑：下一轮没有"新进场"触发，候选里也没有 DD；重启后记录还在
    assert c.refresh_reason(now + timedelta(minutes=10), _tiered_pool(now), {}) is None
    c2, _, _ = make(tmp_path, FakeEngine([]), now, tiers=rules, tier_path=path)
    assert c2.refresh_reason(now, _tiered_pool(now), {}) == "scheduled"
    assert [e.symbol for e in c2._eligible(_tiered_pool(now), now, expand=False)] == [
        "AAUSDT",
        "BBUSDT",
        "CCUSDT",
    ]


async def test_tiers_held_tail_symbol_still_meets_and_disabled_means_everything(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT"])
    c, _, _ = make(tmp_path, eng, now, tiers=TierRules(enabled=True))
    c.set_held({"EEUSDT": Side.LONG})
    card = await c.refresh(_tiered_pool(now), "scheduled")
    assert "EEUSDT" in {s.symbol for s in card.symbols}  # 有持仓的尾部币也要开会 / 留在卡上
    off, _, _ = make(tmp_path / "off", FakeEngine([]), now, tiers=TierRules(enabled=False))
    assert len(off._eligible(_tiered_pool(now), now, expand=False)) == 5


async def test_market_meeting_is_recorded_in_meeting_live(tmp_path: Path, now: datetime) -> None:
    live = tmp_path / "live"
    eng = FakeEngine(["BBUSDT", "CCUSDT"])
    c, _, _ = make(tmp_path, eng, now, live_dir=live)
    c.set_held({"AAUSDT": Side.LONG})
    await c.refresh(
        pool(now, entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT"), entry("DDUSDT")), "x"
    )
    doc = json.loads((live / "_MARKET.json").read_text(encoding="utf-8"))
    assert doc["symbol"] == "_MARKET" and doc["status"] == "done"
    assert [s["stage"] for s in doc["steps"]] == ["input", "pick", "filter"]
    assert "区间震荡" in doc["steps"][1]["text"] and "BBUSDT" in doc["steps"][1]["text"]
    assert "AAUSDT：固定 / 持仓强制开会" in doc["steps"][2]["text"]
    assert "DDUSDT：总盘会这轮没挑中（候选池第" in doc["steps"][2]["text"]
    assert doc["cost_usd"] == pytest.approx(0.01)


async def test_market_meeting_records_rejection_reasons(tmp_path: Path, now: datetime) -> None:
    live = tmp_path / "live"

    class Eng(FakeEngine):
        async def pick(
            self, candidates: Sequence[SymbolPoolEntry], max_n: int, previous: BattleCard | None
        ) -> PickResult:
            return PickResult(
                "区间震荡", [Pick("BBUSDT", "r")], 0.01, [Pick("CCUSDT", "量能萎缩，不值得开会")]
            )

    c, _, _ = make(tmp_path, Eng(["BBUSDT"]), now, live_dir=live)
    await c.refresh(pool(now, entry("BBUSDT"), entry("CCUSDT"), entry("DDUSDT")), "x")
    doc = json.loads((live / "_MARKET.json").read_text(encoding="utf-8"))
    assert doc["rejections"] == [
        {"symbol": "CCUSDT", "reason": "量能萎缩，不值得开会"},
        {"symbol": "DDUSDT", "reason": "军师没挑，也没给理由"},
    ]


async def test_symbol_record_carries_usage(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.slow.commander import Analysis

    out = BattleCardCommander._symbol_output(None, False, {"input_tokens": 1})
    assert out == {"cost_estimated": False, "usage": {"input_tokens": 1}}
    assert Analysis.__dataclass_fields__["usage"].default is None


async def test_saved_card_with_stop_hint_survives_restart(tmp_path: Path, now: datetime) -> None:
    """存盘→读盘→恢复：stop_hint 是 tuple，JSON 读回是 list，不能因此判成「档位变了」。"""
    from jiaoyi.slow.commander import StopScale

    scale = StopScale(15, 0.9, 2.95, 1.0, 3.0, long_period_min=60, stop_hint=(1.15, 1.85))
    path = tmp_path / "battle_card.json"
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, card_path=path, stop_scale=scale)
    card = await c.refresh(pool(now, entry("AAUSDT")), "启动")
    c2 = BattleCardCommander(
        FakeEngine([]),
        RISK,
        JsonlDecisionLog(tmp_path),
        clock=lambda: now,
        card_path=path,
        stop_scale=StopScale(15, 0.9, 2.95, 1.0, 3.0, long_period_min=60, stop_hint=(1.15, 1.85)),
    )
    restored = c2.current(now)
    assert restored is not None and restored.card_id == card.card_id
