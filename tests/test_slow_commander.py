from __future__ import annotations

import asyncio
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
    SignalKind,
    SizeTier,
    SymbolPool,
    SymbolPoolEntry,
)
from jiaoyi.core.config import load_risk_config
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.slow.commander import (
    Analysis,
    BattleCardCommander,
    CardRefreshError,
    Pick,
    PickResult,
    clamp_to_risk,
)
from tests.conftest import ROOT

RISK = load_risk_config(ROOT / "config" / "risk.example.toml")


def entry(sym: str, *, pinned: bool = False, cat: Category = Category.ALT) -> SymbolPoolEntry:
    return SymbolPoolEntry(
        symbol=sym,
        category=cat,
        score=0.5,
        filters=HardFilters(
            quote_volume_24h_usdt=1e8, spread_bps=1, depth_usdt=1e6, listed_days=100, passed=True
        ),
        pinned=pinned,
        in_pool=True,
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
    out = clamp_to_risk(cs, RISK)
    assert out.notional_usdt.max == RISK.max_notional_per_trade_usdt
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


def test_ttl_range_enforced(tmp_path: Path, now: datetime) -> None:
    with pytest.raises(ValueError):
        make(tmp_path, FakeEngine([]), now, ttl=timedelta(minutes=30))


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
