from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
        self.briefs: dict[str, tuple[str, str]] = {}  # 币 → (总盘会判断, 挑币理由)
        self.running = self.peak = 0

    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        return PickResult("区间震荡", [Pick(s, "r") for s in self.picks][:max_n], 0.01)

    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        self.analyzed.append(e.symbol)
        self.briefs[e.symbol] = (regime, pick_reason)
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
    assert len(recs) == 5  # 两个标的 + 每个币开完一条更新记录 + 整轮一张卡
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


async def test_symbol_meetings_get_the_regime_and_why_the_coin_was_picked(
    tmp_path: Path, now: datetime
) -> None:
    """总盘会的判断和挑币理由要进单币会；固定币不是挑的，理由留空（不提持仓）。"""
    eng = FakeEngine(["BBUSDT"])
    c, _, _ = make(tmp_path, eng, now)
    await c.refresh(pool(now, entry("AAUSDT", pinned=True), entry("BBUSDT")), "scheduled")
    assert eng.briefs["BBUSDT"] == ("区间震荡", "r")
    assert eng.briefs["AAUSDT"] == ("区间震荡", "")


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
    assert c.current(clock[0]).model_dump(exclude={"current_market"}) == old.model_dump(
        exclude={"current_market"}
    )


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
        async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
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
    # 只按权益倍数时慢环不夹名义价值（不知道实时权益），风控员下单时封顶
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
    assert out.no_trade_from is Bias.LONG  # 会议方向留着，管家不把同方向持仓当中性
    plain = clamp_to_risk(card_symbol("AAUSDT", bias=Bias.NO_TRADE, allowed_signals=[]), RISK)
    assert plain.no_trade_from is None
    kept = card_symbol("AAUSDT", bias=Bias.NO_TRADE, allowed_signals=[], no_trade_from=Bias.SHORT)
    assert clamp_to_risk(kept, RISK).no_trade_from is Bias.SHORT


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
        async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
            return Analysis(card_symbol(e.symbol, category=Category.MAJOR), 0.0)

    c, _, _ = make(tmp_path, LyingEngine(["AAUSDT", "BBUSDT"]), now)
    with pytest.raises(CardRefreshError):  # 全部被拒 = 不出卡
        await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")

    # 其中一个正确、一个说谎：卡里只有正确的，且分组是候选池的
    class HalfLying(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
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
        async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
            if e.symbol == "AAUSDT":
                raise MeetingError("结构化输出失败", cost_usd=0.03)  # 真实 usage
            raise RuntimeError("超时")  # 拿不到 usage → 估算

    eng = Eng(["AAUSDT", "BBUSDT"])
    c, log, _ = make(tmp_path, eng, now, failed_meeting_cost_usd=0.07)
    with pytest.raises(CardRefreshError):
        await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    # 选币 0.01（所有标的都失败也要计）+ 真实 0.03 + 估算 0.07
    assert log.read_status()["commander"].today.cost_usd == pytest.approx(0.11)


async def test_credit_errors_cost_nothing_and_raise_the_alarm(
    tmp_path: Path, now: datetime
) -> None:
    class Eng(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
            raise RuntimeError("Error code: 402 - in_flight_budget_exhausted, Retry-After 120")

    c, log, _ = make(tmp_path, Eng(["AAUSDT"]), now, failed_meeting_cost_usd=0.07)
    alarms: list[int] = []
    c.bind_credit_alarm(lambda: alarms.append(1))
    with pytest.raises(CardRefreshError):
        await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    assert alarms  # 额度不足：告警
    # 只有选币 0.01；被 402 拒的会议没烧 token，不按估算 0.07 计
    assert log.read_status()["commander"].today.cost_usd == pytest.approx(0.01)


async def test_invalid_conclusion_still_charges_its_cost(tmp_path: Path, now: datetime) -> None:
    class Eng(FakeEngine):
        async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
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
    assert tight.stop_atr.min >= 0.5  # 不会被风控员的 min_stop_atr 整张拒掉
    wide = clamp_to_risk(card_symbol("AAUSDT", stop_atr=Range(min=3, max=9)), RISK, band)
    assert wide.stop_atr.max == 4.0
    ok = clamp_to_risk(card_symbol("AAUSDT", stop_atr=Range(min=1, max=2)), RISK, band)
    assert (ok.stop_atr.min, ok.stop_atr.max) == (1, 2)


def test_clamp_raises_too_low_take_profit_into_guard_band() -> None:
    from jiaoyi.contracts import Range
    from jiaoyi.slow.commander import StopScale

    band = StopScale(15, 0.5, 4.0, 1.0, 6.0)
    low = clamp_to_risk(card_symbol("AAUSDT", take_profit_r=Range(min=0.3, max=0.8)), RISK, band)
    assert low.take_profit_r.min >= 1.0  # 不会被风控员的 min_tp_r 整张拒掉
    high = clamp_to_risk(card_symbol("AAUSDT", take_profit_r=Range(min=3, max=12)), RISK, band)
    assert high.take_profit_r.max == 6.0
    ok = clamp_to_risk(card_symbol("AAUSDT", take_profit_r=Range(min=1.5, max=3)), RISK, band)
    assert (ok.take_profit_r.min, ok.take_profit_r.max) == (1.5, 3)


class _SlowSecond(FakeEngine):
    """第二个币开会时被打断（模拟重启）：此时第一个币的会早该落盘。"""

    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
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

    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
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

        async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
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
            self,
            candidates: Sequence[SymbolPoolEntry],
            max_n: int,
            previous: BattleCard | None,
            pool: Sequence[SymbolPoolEntry] = (),
        ) -> PickResult:
            return PickResult(
                "区间震荡", [Pick("BBUSDT", "r")], 0.01, [Pick("CCUSDT", "量能萎缩，不值得开会")]
            )

    c, _, _ = make(tmp_path, Eng(["BBUSDT"]), now, live_dir=live)
    await c.refresh(pool(now, entry("BBUSDT"), entry("CCUSDT"), entry("DDUSDT")), "x")
    doc = json.loads((live / "_MARKET.json").read_text(encoding="utf-8"))
    assert doc["rejections"] == [
        {"symbol": "CCUSDT", "reason": "量能萎缩，不值得开会"},
        {"symbol": "DDUSDT", "reason": "AI 没写理由"},
    ]
    filt = doc["steps"][2]["text"]
    assert (
        "CCUSDT：总盘会这轮没挑中" in filt and "量能萎缩，不值得开会" in filt
    )  # 步骤文字也写军师的理由
    assert "分）" not in filt and "选币分 0." in filt  # 0–1 的分数不能四舍五入成 0


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


async def test_card_record_keeps_historical_direction_when_latest_market_changed(
    tmp_path: Path, now: datetime
) -> None:
    from jiaoyi.contracts import MarketView

    engine = FakeEngine(["AAUSDT"])
    commander, log, _ = make(tmp_path, engine, now)
    card = await commander.refresh(pool(now, entry("AAUSDT")), "scheduled")
    historical = card.model_copy(update={"regime": "旧看跌", "regime_direction": "看跌"})
    commander.latest_market_view = MarketView(
        market_view_id="latest",
        version=2,
        observed_at=now,
        regime="新看涨",
        regime_direction="看涨",
    )
    commander.last_regime_direction = "看涨"
    commander._record_card(historical, 0.0)
    record = log.read_day(now.date().isoformat())[-1]
    assert record.output["regime"] == "旧看跌" and record.output["regime_direction"] == "看跌"
    assert record.output["market_view"]["regime_direction"] == "看涨"


async def test_untradeable_symbol_goes_watch_only_except_when_held(
    tmp_path: Path, now: datetime
) -> None:
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT", "CCUSDT"]), now)
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT")), "scheduled")
    c.set_held({"CCUSDT": Side.LONG})
    c.set_untradeable({"AAUSDT": "SETTLING", "CCUSDT": "SETTLING"})
    card = c.current(now)
    assert card is not None
    aa, bb, cc = (card.for_symbol(x) for x in ("AAUSDT", "BBUSDT", "CCUSDT"))
    assert aa is not None and aa.allowed_signals == [] and aa.bias is Bias.LONG
    assert bb is not None and bb.allowed_signals == [SignalKind.VOLUME_BREAKOUT]
    assert cc is not None and cc.allowed_signals == [SignalKind.VOLUME_BREAKOUT]
    c.set_untradeable({})
    again = c.current(now)
    assert again is not None
    assert again.for_symbol("AAUSDT").allowed_signals == [SignalKind.VOLUME_BREAKOUT]  # type: ignore[union-attr]


# 2026-10-28: the 03:48 card expired 06:08, but XRP's 03:43:24 event conclusion was carried over
# with the old card's 04:07:17 expiry, leaving no tradable entry until the 05:48 round
R1 = datetime.fromisoformat("2026-10-28T01:47:17+00:00")
XRP_EVENT = datetime.fromisoformat("2026-10-28T03:43:24+00:00")
R2 = datetime.fromisoformat("2026-10-28T03:48:00+00:00")


class _Directed(FakeEngine):
    def __init__(self, picks: list[str]) -> None:
        super().__init__(picks)
        self.direction = "中性"

    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        got = await super().pick(candidates, max_n, previous, pool)
        return dataclasses.replace(got, regime_direction=self.direction)


async def _xrp_case(
    tmp_path: Path, on: bool, *, picked: bool = True, flip: bool = False
) -> tuple[BattleCardCommander, BattleCard]:
    from jiaoyi.slow.commander import EventRules

    eng = _Directed(["XRPUSDT", "LINKUSDT"])
    eng.direction = "看涨"
    c, _, clock = make(
        tmp_path,
        eng,
        R1,
        ttl=timedelta(minutes=140),
        expiry_lead=timedelta(minutes=20),
        symbol_min_interval=timedelta(minutes=115),
        events=EventRules(enabled=True, triggers=frozenset({"key_level_break"})),
        reuse_extend_expiry=on,
    )
    p = pool(R1, entry("XRPUSDT"), entry("LINKUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = XRP_EVENT
    assert await c.refresh_symbol(p, "XRPUSDT", "key_level_break") is not None
    clock[0] = R2
    if not picked:
        eng.picks = ["LINKUSDT"]
    if flip:
        eng.direction = "看跌"
    card = await c.refresh(pool(R2, entry("XRPUSDT"), entry("LINKUSDT")), "scheduled")
    assert eng.analyzed == ["XRPUSDT", "LINKUSDT", "XRPUSDT", "LINKUSDT"]
    return c, card


@pytest.mark.parametrize("picked", [True, False])
@pytest.mark.parametrize("on", [False, True])
async def test_commander_refresh_reused_entry_keeps_or_extends_expiry(
    tmp_path: Path, on: bool, picked: bool
) -> None:
    c, card = await _xrp_case(tmp_path, on, picked=picked)
    assert card.expires_at == datetime.fromisoformat("2026-10-28T06:08:00+00:00")
    xrp = card.for_symbol("XRPUSDT")
    assert xrp is not None and xrp.issued_at == XRP_EVENT
    seen = c.current(datetime.fromisoformat("2026-10-28T04:30:00+00:00"))
    syms = {s.symbol for s in seen.symbols} if seen is not None else set()
    if on:
        # capped at issued_at + ttl (06:03:24), not stretched to the round's 06:08
        assert xrp.expires_at == datetime.fromisoformat("2026-10-28T06:03:24+00:00")
        assert syms == {"XRPUSDT", "LINKUSDT"}
    else:
        assert xrp.expires_at == datetime.fromisoformat("2026-10-28T04:07:17+00:00")
        assert syms == {"LINKUSDT"}


@pytest.mark.parametrize("picked", [True, False])
async def test_commander_refresh_does_not_extend_entry_from_a_flipped_regime(
    tmp_path: Path, picked: bool
) -> None:
    _c, card = await _xrp_case(tmp_path, True, picked=picked, flip=True)
    assert card.regime_direction == "看跌"
    xrp = card.for_symbol("XRPUSDT")
    assert xrp is not None
    assert xrp.expires_at == datetime.fromisoformat("2026-10-28T04:07:17+00:00")


async def test_commander_refresh_reuse_extension_is_capped_by_conclusion_age(
    tmp_path: Path,
) -> None:
    eng = FakeEngine(["AAUSDT"])
    c, _, clock = make(
        tmp_path,
        eng,
        R1,
        ttl=timedelta(minutes=140),
        symbol_min_interval=timedelta(minutes=150),
        reuse_extend_expiry=True,
    )
    p = pool(R1, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = R2
    card = await c.refresh(p, "scheduled")
    assert eng.analyzed == ["AAUSDT"]
    aa = card.for_symbol("AAUSDT")
    assert aa is not None and aa.expires_at == datetime.fromisoformat("2026-10-28T04:07:17+00:00")


class _EventDuringPick(FakeEngine):
    """BB's event meeting finishes while the round is still picking."""

    def __init__(self) -> None:
        super().__init__(["AAUSDT", "BBUSDT"])
        self.release = asyncio.Event()
        self.merged = asyncio.Event()
        self.hold = False

    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        if e.symbol == "BBUSDT" and self.hold:
            await self.release.wait()
        return await super().analyze(e, regime, pick_reason)

    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        if self.hold:
            self.release.set()
            await asyncio.wait_for(self.merged.wait(), 5)
        return await super().pick(candidates, max_n, previous, pool)


@pytest.mark.parametrize("on", [False, True])
async def test_commander_refresh_extends_event_merged_while_picking(
    tmp_path: Path, on: bool
) -> None:
    from jiaoyi.slow.commander import EventRules

    eng = _EventDuringPick()
    c, _, clock = make(
        tmp_path,
        eng,
        R1,
        ttl=timedelta(minutes=140),
        events=EventRules(enabled=True, triggers=frozenset({"key_level_break"})),
        reuse_extend_expiry=on,
    )
    p = pool(R1, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    eng.picks, eng.hold = ["AAUSDT"], True
    clock[0] = XRP_EVENT
    event = asyncio.create_task(c.refresh_symbol(p, "BBUSDT", "key_level_break"))
    await asyncio.sleep(0)
    clock[0] = R2

    async def merged() -> None:
        await event
        eng.merged.set()

    watcher = asyncio.create_task(merged())
    card = await asyncio.wait_for(c.refresh(p, "scheduled"), 5)
    await asyncio.wait_for(watcher, 5)
    bb = card.for_symbol("BBUSDT")
    assert bb is not None and bb.issued_at == XRP_EVENT
    expected = "2026-10-28T06:03:24+00:00" if on else "2026-10-28T04:07:17+00:00"
    assert bb.expires_at == datetime.fromisoformat(expected)


class _WithOutlook(FakeEngine):
    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        from jiaoyi.contracts import CardOutlook

        res = await super().analyze(e, regime, pick_reason)
        cs = res.card_symbol.model_copy(update={"outlook": CardOutlook(market_dir="flat")})
        return dataclasses.replace(res, card_symbol=cs)


async def _xrp_event_card(
    tmp_path: Path, eng: FakeEngine
) -> tuple[BattleCardCommander, list[datetime], dict[str, object]]:
    from jiaoyi.slow.commander import EventRules

    kw: dict[str, object] = {
        "ttl": timedelta(minutes=140),
        "symbol_min_interval": timedelta(minutes=115),
        "events": EventRules(enabled=True, triggers=frozenset({"key_level_break"})),
        "reuse_extend_expiry": True,
        "card_path": tmp_path / "battle_card.json",
    }
    c, _, clock = make(tmp_path, eng, R1, **kw)
    p = pool(R1, entry("XRPUSDT"), entry("LINKUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = XRP_EVENT
    assert await c.refresh_symbol(p, "XRPUSDT", "key_level_break") is not None
    eng.picks = ["LINKUSDT"]  # XRP is carried over unpicked
    return c, clock, kw


async def _second_round(c: BattleCardCommander) -> CardSymbol:
    card = await c.refresh(pool(R2, entry("XRPUSDT"), entry("LINKUSDT")), "scheduled")
    assert card.regime_direction == "中性"
    xrp = card.for_symbol("XRPUSDT")
    assert xrp is not None and xrp.issued_at == XRP_EVENT
    return xrp


def _edit_json(path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("flip", [False, True])
async def test_commander_refresh_after_restart_restores_view_directions(
    tmp_path: Path, flip: bool
) -> None:
    eng = _Directed(["XRPUSDT", "LINKUSDT"])
    _c, _clock, kw = await _xrp_event_card(tmp_path, eng)
    saved = json.loads((tmp_path / "market_view.json").read_text(encoding="utf-8"))
    assert set(saved["view_dirs"].values()) == {"中性"}
    c2, _, _ = make(tmp_path, eng, R2, **kw)  # restart (every update restarts the process)
    eng.direction = "看跌" if flip else "中性"
    card = await c2.refresh(pool(R2, entry("XRPUSDT"), entry("LINKUSDT")), "scheduled")
    xrp = card.for_symbol("XRPUSDT")
    assert xrp is not None and xrp.issued_at == XRP_EVENT
    expected = "2026-10-28T04:07:17+00:00" if flip else "2026-10-28T06:03:24+00:00"
    assert xrp.expires_at == datetime.fromisoformat(expected)


@pytest.mark.parametrize("missing", ["view_dirs", "source_market_view_id"])
async def test_commander_refresh_after_restart_keeps_expiry_of_unknown_direction(
    tmp_path: Path, missing: str
) -> None:
    """Files written before the view directions were saved, or a card from before entries named
    their market view: the direction is unknown and the entry keeps its expiry."""
    eng = FakeEngine(["XRPUSDT", "LINKUSDT"])
    _c, _clock, kw = await _xrp_event_card(tmp_path, eng)
    if missing == "view_dirs":
        _edit_json(tmp_path / "market_view.json", lambda d: d.pop("view_dirs"))
    else:

        def drop(d: dict[str, Any]) -> None:
            for s in d["card"]["symbols"]:
                s["source_market_view_id"] = None

        _edit_json(tmp_path / "battle_card.json", drop)
    c2, _, _ = make(tmp_path, eng, R2, **kw)
    restored = c2.current(R2)
    xrp_before = restored.for_symbol("XRPUSDT") if restored is not None else None
    assert xrp_before is not None
    assert (xrp_before.source_market_view_id is None) is (missing == "source_market_view_id")
    xrp = await _second_round(c2)
    assert xrp.expires_at == datetime.fromisoformat("2026-10-28T04:07:17+00:00")


@pytest.mark.parametrize("outlook", [False, True])
async def test_commander_refresh_reads_direction_from_outlook_when_history_is_empty(
    tmp_path: Path, outlook: bool
) -> None:
    """The battle card file drops outlook, so this fallback only serves a process whose view
    history no longer holds the entry's view (evicted past VIEW_DIRS_MAX); simulated by emptying
    it."""
    eng = _WithOutlook(["XRPUSDT", "LINKUSDT"]) if outlook else FakeEngine(["XRPUSDT", "LINKUSDT"])
    c, clock, _kw = await _xrp_event_card(tmp_path, eng)
    c._view_dirs.clear()
    clock[0] = R2
    xrp = await _second_round(c)
    expected = "2026-10-28T06:03:24+00:00" if outlook else "2026-10-28T04:07:17+00:00"
    assert xrp.expires_at == datetime.fromisoformat(expected)


def test_commander_restore_keeps_last_512_valid_view_directions(tmp_path: Path) -> None:
    dirs: dict[str, object] = {f"v{i}": "看涨" for i in range(600)}
    dirs["bad"] = "up"
    (tmp_path / "market_view.json").write_text(
        json.dumps({"latest_market_view": None, "view_dirs": dirs}), encoding="utf-8"
    )
    c, _, _ = make(tmp_path, FakeEngine([]), R1, card_path=tmp_path / "battle_card.json")
    assert list(c._view_dirs) == [f"v{i}" for i in range(88, 600)]


class _Directed2(FakeEngine):
    """Each meeting of a symbol returns the next bias of its script."""

    def __init__(self, script: dict[str, list[Bias]]) -> None:
        super().__init__(list(script))
        self.script = {k: list(v) for k, v in script.items()}

    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        await super().analyze(e, regime, pick_reason)
        bias = self.script[e.symbol].pop(0)
        signals = [] if bias is Bias.NO_TRADE else [SignalKind.VOLUME_BREAKOUT]
        return Analysis(
            card_symbol(e.symbol, bias=bias, allowed_signals=signals, reason=f"会议{bias.value}"),
            0.02,
        )


_OPP = {Bias.LONG: Bias.SHORT, Bias.SHORT: Bias.LONG}
_SIDE = {Bias.LONG: Side.LONG, Bias.SHORT: Side.SHORT}


async def _flip_rounds(
    tmp_path: Path, now: datetime, script: list[Bias], **kw: object
) -> tuple[BattleCardCommander, JsonlDecisionLog, list[datetime], list[BattleCard]]:
    from jiaoyi.slow.review import CardJournal

    eng = _Directed2({"AAUSDT": script})
    c, log, clock = make(
        tmp_path,
        eng,
        now,
        symbol_min_interval=timedelta(minutes=5),
        card_path=tmp_path / "battle_card.json",
        journal=CardJournal(tmp_path),
        **kw,
    )
    cards = []
    for i in range(len(script)):
        clock[0] = now + timedelta(minutes=60 * i)
        cards.append(await c.refresh(pool(clock[0], entry("AAUSDT")), "scheduled"))
    return c, log, clock, cards


def _flip_records(log: JsonlDecisionLog, now: datetime) -> list[str]:
    return [
        str(r.output["card_flip"])
        for r in log.read_day(now.date().isoformat())
        if r.output and "card_flip" in r.output
    ]


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_waits_for_second_agreeing_meeting(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    from jiaoyi.slow.review import CardJournal

    opp = _OPP[start]
    c, log, clock, cards = await _flip_rounds(tmp_path, now, [start, opp], flip_confirm=2)
    pos = PositionRecord.model_construct(symbol="AAUSDT", side=_SIDE[start])
    first = cards[0].for_symbol("AAUSDT")
    held = cards[1].for_symbol("AAUSDT")
    assert first is not None and held is not None
    assert held.bias is start and held.allowed_signals == []
    assert (held.issued_at, held.meeting_id) == (first.issued_at, first.meeting_id)
    assert held.reason.startswith("待确认翻转（1/2）")
    assert held.reason.endswith(f"会议{start.value}")
    assert JevKeeper._card_rule(pos, cards[1], clock[0]) is None
    assert [r.bias for r in CardJournal(tmp_path).read()] == [start.value, opp.value]
    issued = [
        r
        for r in log.read_day(now.date().isoformat())
        if r.card_id and r.outcome is Outcome.ACTION and not r.symbol
    ]
    note = issued[-1].output["pool"]["AAUSDT"]["flip_pending"]
    assert (note["confirmed"], note["candidate"], note["count"], note["need"]) == (
        start.value,
        opp.value,
        1,
        2,
    )
    # the confirming meeting is an event meeting: both kinds count
    c._engine.script["AAUSDT"].append(opp)  # type: ignore[attr-defined]
    clock[0] += timedelta(minutes=30)
    card = await c.refresh_symbol(pool(clock[0], entry("AAUSDT")), "AAUSDT", "key_level_break")
    assert card is not None
    flipped = card.for_symbol("AAUSDT")
    assert flipped is not None and flipped.bias is opp and flipped.allowed_signals
    assert JevKeeper._card_rule(pos, card, clock[0]) == f"新作战卡方向改成 {opp.value}"
    assert _flip_records(log, now) == ["pending", "confirmed"]
    assert json.loads((tmp_path / "card_flip.json").read_text())["flips"] == {}


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_pending_hands_over_the_candidate_and_confirm_flip_applies_it(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    opp = _OPP[start]
    eng = _Directed2({"AAUSDT": [start, opp]})
    c, log, clock = make(
        tmp_path,
        eng,
        now,
        symbol_min_interval=timedelta(minutes=5),
        card_path=tmp_path / "battle_card.json",
        flip_confirm=2,
    )
    pending: list[tuple[str, Any]] = []
    c.set_on_flip_pending(lambda sym, cand: pending.append((sym, cand)))
    await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    assert pending == []
    clock[0] = now + timedelta(minutes=60)
    held = (await c.refresh(pool(clock[0], entry("AAUSDT")), "scheduled")).for_symbol("AAUSDT")
    assert held is not None and held.bias is start and held.allowed_signals == []
    [(sym, cand)] = pending
    assert (sym, cand.candidate, cand.count) == ("AAUSDT", opp.value, 1)
    assert not c.confirm_flip("AAUSDT", replace(cand, count=2), "x", {})
    assert not c.confirm_flip("AAUSDT", replace(cand, held="other"), "x", {})
    live = c._card
    assert live is not None
    swapped = [
        x.model_copy(update={"meeting_id": "other"}) if x.symbol == "AAUSDT" else x
        for x in live.symbols
    ]
    c._card = live.model_copy(update={"symbols": swapped})  # the directive changed under us
    assert not c.confirm_flip("AAUSDT", cand, "x", {})
    c._card = live
    assert c.confirm_flip("AAUSDT", cand, "两个模型都答是", {})
    card = c.current(clock[0])
    assert card is not None
    flipped = card.for_symbol("AAUSDT")
    assert flipped is not None and flipped.bias is opp and flipped.allowed_signals
    pos = PositionRecord.model_construct(symbol="AAUSDT", side=_SIDE[start])
    assert JevKeeper._card_rule(pos, card, clock[0]) == f"新作战卡方向改成 {opp.value}"
    assert _flip_records(log, now) == ["pending", "confirmed"]
    assert not c.confirm_flip("AAUSDT", cand, "x", {})


async def test_commander_confirm_flip_refuses_a_candidate_that_moved_on(
    tmp_path: Path, now: datetime
) -> None:
    eng = _Directed2({"AAUSDT": [Bias.LONG, Bias.SHORT, Bias.SHORT]})
    c, _log, clock = make(
        tmp_path,
        eng,
        now,
        symbol_min_interval=timedelta(minutes=5),
        card_path=tmp_path / "battle_card.json",
        flip_confirm=3,
    )
    pending: list[Any] = []
    c.set_on_flip_pending(lambda sym, cand: pending.append(cand))
    for i in range(3):
        clock[0] = now + timedelta(minutes=60 * i)
        await c.refresh(pool(clock[0], entry("AAUSDT")), "scheduled")
    first, second = pending
    assert (first.count, second.count) == (1, 2)
    assert not c.confirm_flip("AAUSDT", first, "旧的那次", {})
    card = c.current(clock[0])
    assert card is not None and card.for_symbol("AAUSDT").bias is Bias.LONG  # type: ignore[union-attr]


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_back_cancels_candidate(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    c, log, _clock, cards = await _flip_rounds(
        tmp_path, now, [start, _OPP[start], start], flip_confirm=2
    )
    last = cards[2].for_symbol("AAUSDT")
    assert last is not None and last.bias is start and last.allowed_signals
    assert last.reason == f"会议{start.value}"
    assert _flip_records(log, now) == ["pending", "cancelled"]
    assert c._flips.items() == {}


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_to_neutral_needs_confirmation_too(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    _c, log, _clock, cards = await _flip_rounds(
        tmp_path, now, [start, Bias.NO_TRADE, Bias.NO_TRADE], flip_confirm=2
    )
    held = cards[1].for_symbol("AAUSDT")
    assert held is not None and held.bias is start and held.allowed_signals == []
    final = cards[2].for_symbol("AAUSDT")
    assert final is not None and final.bias is Bias.NO_TRADE
    assert _flip_records(log, now) == ["pending", "confirmed"]


class _ByAssistant:
    """A commander whose flips only the keeper assistant may confirm, one meeting at a time."""

    def __init__(self, tmp_path: Path, now: datetime, script: list[Bias]) -> None:
        self.mode = ["assistant"]
        self.now = now
        self.c, self.log, self.clock = make(
            tmp_path,
            _Directed2({"AAUSDT": list(script)}),
            now,
            symbol_min_interval=timedelta(minutes=5),
            card_path=tmp_path / "battle_card.json",
            flip_confirm=2,
        )
        self.c.set_flip_by_assistant(lambda: self.mode[0] == "assistant")
        self.n = 0

    async def meet(self) -> CardSymbol:
        self.clock[0] = self.now + timedelta(minutes=60 * self.n)
        self.n += 1
        card = await self.c.refresh(pool(self.clock[0], entry("AAUSDT")), "scheduled")
        cs = card.for_symbol("AAUSDT")
        assert cs is not None
        return cs


@pytest.mark.parametrize("new", [Bias.SHORT, Bias.NO_TRADE])
async def test_commander_flip_by_assistant_meetings_only_propose_and_resend(
    tmp_path: Path, now: datetime, new: Bias
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, new, new, new])
    sent: list[Any] = []
    b.c.set_on_flip_pending(lambda sym, cand: sent.append(cand))
    for _ in range(4):
        held = await b.meet()
    assert held.bias is Bias.LONG and held.allowed_signals == []
    assert held.reason.startswith("待确认翻转（第 3 场改判，等管家助手确认）")
    assert [x.count for x in sent] == [1, 2, 3]
    assert _flip_records(b.log, now) == ["pending", "pending", "pending"]
    note = b.c._flip_note("AAUSDT")["flip_pending"]
    assert note["by"] == "assistant"


async def test_commander_flip_by_assistant_meeting_back_to_original_voids_it(
    tmp_path: Path, now: datetime
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, Bias.SHORT, Bias.LONG])
    for _ in range(3):
        last = await b.meet()
    assert last.bias is Bias.LONG and last.allowed_signals
    assert _flip_records(b.log, now) == ["pending", "unasked", "cancelled"]


async def test_commander_flip_by_assistant_back_to_meeting_confirms_by_count_again(
    tmp_path: Path, now: datetime
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, Bias.SHORT, Bias.SHORT, Bias.SHORT])
    for _ in range(3):
        held = await b.meet()
    assert held.bias is Bias.LONG
    b.mode[0] = "meeting"
    final = await b.meet()
    assert final.bias is Bias.SHORT and final.allowed_signals
    # no assistant wired here: each assistant-mode proposal says nobody will confirm it
    assert _flip_records(b.log, now) == ["pending", "unasked", "pending", "unasked", "confirmed"]


@pytest.mark.parametrize(
    ("new", "p_yes", "agree"),
    [
        (Bias.SHORT, 0.9, True),
        (Bias.SHORT, 0.3, False),
        (Bias.NO_TRADE, 0.1, True),
        (Bias.NO_TRADE, 0.5, False),
    ],
)
async def test_commander_flip_by_assistant_confirmed_only_when_the_judges_agree(
    tmp_path: Path, now: datetime, new: Bias, p_yes: float, agree: bool
) -> None:
    from tests.test_keeper_assistant import Harness, Scorer

    b = _ByAssistant(tmp_path, now, [Bias.LONG, new, new])
    a = Scorer(p_yes)
    h = Harness(
        tmp_path / "ka",
        now,
        a,
        Scorer(p_yes),
        cards=b.c,  # type: ignore[arg-type]
        keeper_assistant_flip_neutral=True,
        keeper_assistant_gap_s=0,
        card_flip_confirm_by="assistant",
    )
    b.c.set_on_flip_pending(h.a.on_flip_pending)
    await b.meet()
    await b.meet()
    await h.settle()
    cs = b.c.current(b.clock[0]).for_symbol("AAUSDT")  # type: ignore[union-attr]
    assert cs is not None
    if agree:
        assert cs.bias is new
        assert _flip_records(b.log, now) == ["pending", "confirmed"]
        return
    assert cs.bias is Bias.LONG and cs.allowed_signals == []
    await b.meet()  # the same conclusion again: asked again, still not confirmed
    await h.settle()
    assert len(a.calls) == 2
    cs = b.c.current(b.clock[0]).for_symbol("AAUSDT")  # type: ignore[union-attr]
    assert cs is not None and cs.bias is Bias.LONG
    assert _flip_records(b.log, now) == ["pending", "pending"]


async def _flip_restart(tmp_path: Path, now: datetime, script: list[Bias]) -> _ByAssistant:
    """Two meetings under ``flip`` / ``meeting``, then a fresh commander on the same files."""
    first = _ByAssistant(tmp_path, now, script)
    first.mode[0] = "meeting"
    await first.meet()
    await first.meet()
    return _ByAssistant(tmp_path, first.clock[0], script)


@pytest.mark.parametrize("new", [Bias.NO_TRADE, Bias.SHORT])
async def test_commander_sweep_flips_after_restart_confirms_neutral_and_waits_on_directional(
    tmp_path: Path, now: datetime, new: Bias
) -> None:
    from tests.test_keeper_assistant import Harness, Scorer

    b = await _flip_restart(tmp_path, now, [Bias.LONG, new])
    assert b.c._flips.get("AAUSDT") is not None and b.c._flip_fresh == {}
    a = Scorer(0.1)
    h = Harness(
        tmp_path / "ka",
        now,
        a,
        Scorer(0.1),
        cards=b.c,  # type: ignore[arg-type]
        keeper_assistant_flip_neutral=True,
        card_flip_confirm_by="assistant",
    )
    b.c.set_on_flip_pending(h.a.on_flip_pending)
    h.a.tick(now)
    h.a.tick(now)  # swept once per switch, not on every heartbeat
    await h.settle()
    cs = b.c.current(b.clock[0]).for_symbol("AAUSDT")  # type: ignore[union-attr]
    assert cs is not None
    if new is Bias.NO_TRADE:
        assert len(a.calls) == 1 and "会上涨吗" in a.calls[0][1]
        assert cs.bias is Bias.NO_TRADE and cs.allowed_signals == []
        assert cs.reason.startswith("管家助手确认转中性")
        assert b.c._flips.get("AAUSDT") is None
        return
    assert a.calls == []
    assert cs.bias is Bias.LONG and cs.allowed_signals == []
    unasked = [
        r.output
        for r in b.log.read_day(now.date().isoformat())
        if r.output and r.output.get("card_flip") == "unasked"
    ]
    assert [u["why"] for u in unasked] == ["需要下一场会的卡面"]


@pytest.mark.parametrize("need", [1, 2])
async def test_commander_neutral_flip_ignore_keeps_the_direction_tradable(
    tmp_path: Path, now: datetime, need: int
) -> None:
    eng = _Directed2({"AAUSDT": [Bias.LONG, Bias.NO_TRADE]})
    c, log, clock = make(
        tmp_path,
        eng,
        now,
        symbol_min_interval=timedelta(minutes=5),
        card_path=tmp_path / "battle_card.json",
        flip_confirm=need,
    )
    c.set_neutral_flip_ignored(lambda: True)
    first = (await c.refresh(pool(now, entry("AAUSDT")), "scheduled")).for_symbol("AAUSDT")
    clock[0] = now + timedelta(minutes=60)
    card = await c.refresh(pool(clock[0], entry("AAUSDT")), "scheduled")
    kept = card.for_symbol("AAUSDT")
    assert first is not None and kept is not None
    assert kept.bias is Bias.LONG and kept.allowed_signals == first.allowed_signals
    assert kept.meeting_id == first.meeting_id
    assert kept.expires_at is not None and first.expires_at is not None
    assert kept.expires_at > first.expires_at
    assert c._flips.items() == {}
    [rec] = [
        r for r in log.read_day(now.date().isoformat()) if r.output and "card_flip" in r.output
    ]
    assert rec.output["card_flip"] == "ignored"
    assert rec.summary == "AAUSDT 会议判不做，按设置不翻转，保留原方向（做多）"


async def test_commander_neutral_flip_ignore_leaves_directional_flips_alone(
    tmp_path: Path, now: datetime
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, Bias.SHORT, Bias.NO_TRADE])
    b.mode[0] = "meeting"
    b.c.set_neutral_flip_ignored(lambda: True)
    await b.meet()
    await b.meet()
    cand = b.c._flips.get("AAUSDT")
    assert cand is not None and cand.candidate == "short"
    held = await b.meet()  # a neutral meeting neither counts nor cancels it
    assert held.bias is Bias.LONG and held.allowed_signals == []
    assert b.c._flips.get("AAUSDT") == cand and "AAUSDT" in b.c._flip_fresh
    b.c.clear_neutral_flips()
    assert b.c._flips.get("AAUSDT") == cand


@pytest.mark.parametrize("legacy", [False, True])
async def test_commander_clear_neutral_flips_restores_the_tradable_direction(
    tmp_path: Path, now: datetime, legacy: bool
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, Bias.NO_TRADE])
    b.mode[0] = "meeting"
    ignore = [False]
    b.c.set_neutral_flip_ignored(lambda: ignore[0])
    first = await b.meet()
    held = await b.meet()
    assert held.allowed_signals == [] and first.allowed_signals
    cand = b.c._flips.get("AAUSDT")
    assert cand is not None and cand.signals == tuple(s.value for s in first.allowed_signals)
    if legacy:  # written before candidates kept the signals: found in the meeting's record
        b.c._flips._flips["AAUSDT"] = replace(cand, signals=None)
    b.c.clear_neutral_flips()
    assert b.c._flips.get("AAUSDT") is not None  # still "flip"
    ignore[0] = True
    b.c.clear_neutral_flips()
    cs = b.c.current(b.clock[0]).for_symbol("AAUSDT")  # type: ignore[union-attr]
    assert cs is not None
    assert cs.bias is Bias.LONG and cs.allowed_signals == first.allowed_signals
    assert cs.reason == first.reason
    assert b.c._flips.get("AAUSDT") is None
    assert json.loads((tmp_path / "card_flip.json").read_text())["flips"] == {}


def _ignoring_book(cap_min: float = 0.0) -> Any:
    from jiaoyi.slow.card_flip import FlipBook

    book = FlipBook(2)
    book.neutral_ignored = lambda: True
    book.ignore_max_age = lambda: timedelta(minutes=cap_min)
    return book


@pytest.mark.parametrize(
    "fresh",
    [
        {"bias": Bias.NO_TRADE, "allowed_signals": [], "reason": "stale_on_arrival：现价已走远"},
        {"bias": Bias.LONG, "allowed_signals": [], "reason": "stale_on_arrival：现价已走远"},
        {"bias": Bias.NO_TRADE, "allowed_signals": [], "no_trade_from": Bias.SHORT},
    ],
    ids=["stale-no-trade", "stale-watch", "gated-opposite"],
)
def test_FlipBook_gate_ignore_mode_keeps_only_genuine_no_trade(
    now: datetime, fresh: dict[str, Any]
) -> None:
    book = _ignoring_book()
    held = card_symbol("AAUSDT", issued_at=now - timedelta(hours=1), meeting_id="m1")
    out = book.gate("AAUSDT", card_symbol("AAUSDT", **fresh), held, now)
    assert out.event == "pending" and out.card_symbol.allowed_signals == []
    genuine = card_symbol("BBUSDT", bias=Bias.NO_TRADE, allowed_signals=[], reason="会议观望")
    kept = book.gate("BBUSDT", genuine, held.model_copy(update={"symbol": "BBUSDT"}), now)
    assert kept.event == "ignored" and kept.card_symbol.allowed_signals == held.allowed_signals


@pytest.mark.parametrize(("cap_min", "event"), [(0.0, "ignored"), (120.0, "pending")])
def test_FlipBook_gate_ignore_mode_stops_keeping_an_instruction_older_than_the_cap(
    now: datetime, cap_min: float, event: str
) -> None:
    book = _ignoring_book(cap_min)
    held = card_symbol("AAUSDT", issued_at=now - timedelta(minutes=121), meeting_id="m1")
    neutral = card_symbol("AAUSDT", bias=Bias.NO_TRADE, allowed_signals=[], reason="会议观望")
    assert book.gate("AAUSDT", neutral, held, now).event == event


async def test_commander_clear_neutral_flips_leaves_a_replaced_instruction_alone(
    tmp_path: Path, now: datetime
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, Bias.NO_TRADE])
    b.mode[0] = "meeting"
    b.c.set_neutral_flip_ignored(lambda: True)
    b.c._flips.neutral_ignored = lambda: False  # create the candidate under "flip"
    await b.meet()
    await b.meet()
    b.c._flips.neutral_ignored = lambda: True
    live = b.c._card
    assert live is not None
    swapped = [
        x.model_copy(update={"meeting_id": "other"}) if x.symbol == "AAUSDT" else x
        for x in live.symbols
    ]
    b.c._card = live.model_copy(update={"symbols": swapped})
    b.c.clear_neutral_flips()
    cs = b.c._card.for_symbol("AAUSDT")
    assert cs is not None and cs.allowed_signals == []
    assert b.c._flips.get("AAUSDT") is not None


def _shift_meeting_records(log: JsonlDecisionLog, day: str, by: timedelta) -> None:
    path = log._log_dir / f"{day}.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    for r in rows:
        if r.get("symbol") == "AAUSDT" and "allowed_signals" in (r.get("output") or {}):
            r["ts"] = (datetime.fromisoformat(r["ts"]) + by).isoformat()
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


async def test_commander_neutral_candidate_without_signals_is_kept_then_retried_on_next_no_trade(
    tmp_path: Path, now: datetime
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, Bias.NO_TRADE, Bias.NO_TRADE])
    b.mode[0] = "meeting"
    ignore = [False]
    b.c.set_neutral_flip_ignored(lambda: ignore[0])
    first = await b.meet()
    await b.meet()
    cand = b.c._flips.get("AAUSDT")
    assert cand is not None
    b.c._flips._flips["AAUSDT"] = replace(cand, signals=None)
    _shift_meeting_records(b.log, now.date().isoformat(), timedelta(hours=3))
    ignore[0] = True
    b.c.clear_neutral_flips()  # a meeting three hours away is not this instruction's
    assert b.c._flips.get("AAUSDT") is not None
    assert b.c._card.for_symbol("AAUSDT").allowed_signals == []  # type: ignore[union-attr]
    _shift_meeting_records(b.log, now.date().isoformat(), timedelta(hours=-3))
    again = await b.meet()
    assert again.bias is Bias.LONG and again.allowed_signals == first.allowed_signals
    assert b.c._flips.get("AAUSDT") is None


@pytest.mark.parametrize("answered", [False, True])
async def test_commander_flip_by_assistant_falls_back_to_meetings_when_never_answered(
    tmp_path: Path, now: datetime, answered: bool
) -> None:
    b = _ByAssistant(tmp_path, now, [Bias.LONG, Bias.SHORT, Bias.SHORT, Bias.SHORT])
    b.c.set_flip_limits(lambda: timedelta(0), lambda: timedelta(minutes=120))
    await b.meet()
    await b.meet()
    cand = b.c._flips.get("AAUSDT")
    assert cand is not None
    if answered:  # the judges answered and disagreed: no fallback
        b.c.flip_answered("AAUSDT", cand)
    held = await b.meet()  # 60 minutes in: too early either way
    assert held.bias is Bias.LONG
    assert b.c.stuck_held_flips(b.clock[0], timedelta(minutes=59)) == []  # not held
    b.c.set_held({"AAUSDT": Side.LONG})
    assert b.c.stuck_held_flips(b.clock[0], timedelta(minutes=59)) == [
        ("AAUSDT", b.c._flips.get("AAUSDT"))
    ]
    assert b.c.stuck_held_flips(b.clock[0], timedelta(minutes=61)) == []
    b.c.set_held({})
    final = await b.meet()  # 120 minutes in
    if answered:
        assert final.bias is Bias.LONG and final.allowed_signals == []
        return
    assert final.bias is Bias.SHORT and final.allowed_signals
    [rec] = [
        r
        for r in b.log.read_day(now.date().isoformat())
        if r.output and r.output.get("card_flip") == "confirmed"
    ]
    assert "管家助手问不了超过 120 分钟，按会议确认" in rec.summary


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_alternating_opposite_and_neutral_still_confirms(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    opp = _OPP[start]
    _c, log, _clock, cards = await _flip_rounds(
        tmp_path, now, [start, opp, Bias.NO_TRADE, opp], flip_confirm=3
    )
    held = [cards[i].for_symbol("AAUSDT") for i in (1, 2)]
    assert all(h is not None and h.bias is start and h.allowed_signals == [] for h in held)
    assert [h.reason[:10] for h in held if h is not None] == [
        "待确认翻转（1/3）",
        "待确认翻转（2/3）",
    ]
    final = cards[3].for_symbol("AAUSDT")
    assert final is not None and final.bias is opp and final.allowed_signals
    assert _flip_records(log, now) == ["pending", "pending", "confirmed"]


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_event_flip_ignores_expired_unheld_instruction(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    opp = _OPP[start]
    eng = _Directed2({"AAUSDT": [start, opp], "BBUSDT": [Bias.LONG, Bias.LONG]})
    c, log, clock = make(
        tmp_path,
        eng,
        now,
        ttl=timedelta(minutes=140),
        symbol_min_interval=timedelta(minutes=5),
        flip_confirm=2,
    )
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    clock[0] = now + timedelta(minutes=120)
    eng.picks = ["BBUSDT"]
    await c.refresh(pool(clock[0], entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    clock[0] = now + timedelta(minutes=150)  # AA's instruction expired, no position
    seen = c.current(clock[0])
    assert seen is not None and seen.for_symbol("AAUSDT") is None
    assert c._card is not None and c._card.for_symbol("AAUSDT") is not None
    card = await c.refresh_symbol(pool(clock[0], entry("AAUSDT")), "AAUSDT", "x")
    assert card is not None
    cs = card.for_symbol("AAUSDT")
    assert cs is not None and cs.bias is opp and cs.allowed_signals
    assert _flip_records(log, now) == []


async def test_commander_flip_lapses_with_expired_stand_in_and_logs_dropped(
    tmp_path: Path, now: datetime
) -> None:
    eng = _Directed2({"AAUSDT": [Bias.LONG, Bias.SHORT, Bias.SHORT], "BBUSDT": [Bias.LONG] * 3})
    c, log, clock = make(
        tmp_path,
        eng,
        now,
        ttl=timedelta(minutes=140),
        symbol_min_interval=timedelta(minutes=5),
        flip_confirm=2,
    )
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    clock[0] = now + timedelta(minutes=60)
    await c.refresh(pool(clock[0], entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    assert c._flips.get("AAUSDT") is not None
    clock[0] = now + timedelta(minutes=190)
    eng.picks = ["BBUSDT"]
    await c.refresh(pool(clock[0], entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    clock[0] = now + timedelta(minutes=210)  # the stand-in (to 60 + 140 min) expired, AA unheld
    card = await c.refresh_symbol(pool(clock[0], entry("AAUSDT")), "AAUSDT", "x")
    assert card is not None
    cs = card.for_symbol("AAUSDT")
    assert cs is not None and cs.bias is Bias.SHORT and cs.allowed_signals
    assert _flip_records(log, now) == ["pending", "dropped"]


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_out_of_neutral_applies_at_once(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    _c, log, _clock, cards = await _flip_rounds(
        tmp_path, now, [Bias.NO_TRADE, start], flip_confirm=3
    )
    cs = cards[1].for_symbol("AAUSDT")
    assert cs is not None and cs.bias is start and cs.allowed_signals
    assert _flip_records(log, now) == []


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_default_applies_new_direction_immediately(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    _c, log, _clock, cards = await _flip_rounds(tmp_path, now, [start, _OPP[start]])
    cs = cards[1].for_symbol("AAUSDT")
    assert cs is not None and cs.bias is _OPP[start] and cs.allowed_signals
    assert _flip_records(log, now) == []
    assert not (tmp_path / "card_flip.json").exists()


async def test_commander_flip_respects_minimum_candidate_age(tmp_path: Path, now: datetime) -> None:
    _c, log, _clock, cards = await _flip_rounds(
        tmp_path,
        now,
        [Bias.LONG, Bias.SHORT, Bias.SHORT, Bias.SHORT],
        flip_confirm=2,
        flip_confirm_min=timedelta(minutes=90),
    )
    biases = [c.for_symbol("AAUSDT").bias for c in cards]  # type: ignore[union-attr]
    assert biases == [Bias.LONG, Bias.LONG, Bias.LONG, Bias.SHORT]
    held = cards[2].for_symbol("AAUSDT")
    assert held is not None and held.reason.startswith("待确认翻转（2/2，等满 90 分钟）")
    assert _flip_records(log, now) == ["pending", "pending", "confirmed"]


@pytest.mark.parametrize("start", [Bias.LONG, Bias.SHORT])
async def test_commander_flip_candidate_survives_restart(
    tmp_path: Path, now: datetime, start: Bias
) -> None:
    opp = _OPP[start]
    _c, _log, clock, _cards = await _flip_rounds(tmp_path, now, [start, opp], flip_confirm=2)
    c2, _, clock2 = make(
        tmp_path / "after",
        _Directed2({"AAUSDT": [opp]}),
        clock[0] + timedelta(minutes=30),
        symbol_min_interval=timedelta(minutes=5),
        card_path=tmp_path / "battle_card.json",
        flip_confirm=2,
    )
    got = c2._flips.get("AAUSDT")
    assert got is not None
    assert (got.confirmed, got.candidate, got.count) == (start.value, opp.value, 1)
    card = await c2.refresh(pool(clock2[0], entry("AAUSDT")), "scheduled")
    cs = card.for_symbol("AAUSDT")
    assert cs is not None and cs.bias is opp and cs.allowed_signals


async def test_commander_flip_restore_drops_candidate_whose_held_instruction_changed(
    tmp_path: Path, now: datetime
) -> None:
    _c, _log, clock, _cards = await _flip_rounds(
        tmp_path, now, [Bias.LONG, Bias.SHORT], flip_confirm=2
    )
    path = tmp_path / "card_flip.json"
    raw = json.loads(path.read_text())
    raw["flips"]["AAUSDT"]["held"] = "other-meeting|2026-01-01T00:00:00+00:00"
    path.write_text(json.dumps(raw))
    c2, log2, _ = make(
        tmp_path / "after",
        FakeEngine([]),
        clock[0],
        card_path=tmp_path / "battle_card.json",
        flip_confirm=2,
    )
    assert c2._flips.items() == {}
    assert json.loads(path.read_text())["flips"] == {}
    assert _flip_records(log2, now) == ["dropped"]


async def test_commander_flip_progress_stays_capped_while_waiting_for_minimum_age(
    tmp_path: Path, now: datetime
) -> None:
    _c, log, _clock, cards = await _flip_rounds(
        tmp_path,
        now,
        [Bias.LONG, Bias.SHORT, Bias.SHORT, Bias.SHORT, Bias.SHORT],
        flip_confirm=2,
        flip_confirm_min=timedelta(minutes=150),
    )
    held = cards[3].for_symbol("AAUSDT")
    assert held is not None and held.bias is Bias.LONG
    assert held.reason.startswith("待确认翻转（2/2，等满 150 分钟）")
    pending = [
        r
        for r in log.read_day(now.date().isoformat())
        if r.output and r.output.get("card_flip") == "pending"
    ]
    assert "待确认翻转 2/2，等满 150 分钟" in pending[-1].summary
    issued = [
        r
        for r in log.read_day(now.date().isoformat())
        if r.card_id and r.outcome is Outcome.ACTION and not r.symbol
    ]
    note = [r.output["pool"]["AAUSDT"].get("flip_pending") for r in issued]
    assert [n["progress"] for n in note if n is not None][-1] == "2/2，等满 150 分钟"
    final = cards[4].for_symbol("AAUSDT")
    assert final is not None and final.bias is Bias.SHORT


async def test_commander_flip_candidate_survives_restart_that_gates_category(
    tmp_path: Path, now: datetime
) -> None:
    _c, _log, clock, _cards = await _flip_rounds(
        tmp_path, now, [Bias.LONG, Bias.SHORT], flip_confirm=2
    )
    closed = RISK.model_copy(
        update={"category_exposure_x": {**RISK.category_exposure_x, Category.ALT: 0.0}}
    )
    c2 = BattleCardCommander(
        FakeEngine([]),
        closed,
        JsonlDecisionLog(tmp_path / "after"),
        clock=lambda: clock[0],
        card_path=tmp_path / "battle_card.json",
        flip_confirm=2,
    )
    back = c2.current(clock[0])
    held = back.for_symbol("AAUSDT") if back is not None else None
    assert held is not None and held.bias is Bias.NO_TRADE and held.no_trade_from is Bias.LONG
    got = c2._flips.get("AAUSDT")
    assert got is not None and (got.confirmed, got.candidate) == ("long", "short")


async def test_commander_flip_restore_with_setting_back_to_one_says_so(
    tmp_path: Path, now: datetime
) -> None:
    _c, _log, clock, _cards = await _flip_rounds(
        tmp_path, now, [Bias.LONG, Bias.SHORT], flip_confirm=2
    )
    c2, log2, _ = make(
        tmp_path / "after", FakeEngine([]), clock[0], card_path=tmp_path / "battle_card.json"
    )
    assert c2._flips.items() == {}
    dropped = [
        r
        for r in log2.read_day(now.date().isoformat())
        if r.output and r.output.get("card_flip") == "dropped"
    ]
    assert len(dropped) == 1 and "设置改回 1" in dropped[0].summary


_R0 = datetime(2026, 10, 30, 4, 25, tzinfo=UTC)  # the round whose card expires at 06:45


async def _event_expiry_case(
    tmp_path: Path, script: dict[str, list[Bias]], **kw: object
) -> tuple[BattleCardCommander, list[datetime]]:
    eng = _Directed2(script)
    c, _log, clock = make(
        tmp_path,
        eng,
        _R0,
        ttl=timedelta(minutes=140),
        symbol_min_interval=timedelta(minutes=5),
        card_path=tmp_path / "battle_card.json",
        **kw,
    )
    card = await c.refresh(pool(_R0, *(entry(s) for s in script)), "scheduled")
    assert card.expires_at == datetime(2026, 10, 30, 6, 45, tzinfo=UTC)
    return c, clock


async def _event(
    c: BattleCardCommander, clock: list[datetime], sym: str, at: datetime
) -> CardSymbol:
    clock[0] = at
    card = await c.refresh_symbol(pool(at, entry(sym)), sym, "key_level_break")
    assert card is not None
    cs = card.for_symbol(sym)
    assert cs is not None
    return cs


@pytest.mark.parametrize(
    ("sym", "start", "flip", "at", "expires"),
    [
        ("ETHUSDT", Bias.SHORT, Bias.LONG, (5, 11), (7, 31)),
        ("STRKUSDT", Bias.LONG, Bias.SHORT, (5, 42), (8, 2)),
        ("QNTUSDT", Bias.NO_TRADE, Bias.LONG, (6, 12), (8, 32)),
    ],
)
async def test_commander_refresh_symbol_flip_gets_full_validity_from_its_meeting(
    tmp_path: Path,
    sym: str,
    start: Bias,
    flip: Bias,
    at: tuple[int, int],
    expires: tuple[int, int],
) -> None:
    c, clock = await _event_expiry_case(tmp_path, {sym: [start, flip]})
    t = datetime(2026, 10, 30, *at, tzinfo=UTC)
    cs = await _event(c, clock, sym, t)
    assert (cs.bias, cs.issued_at) == (flip, t)
    assert cs.expires_at == datetime(2026, 10, 30, *expires, tzinfo=UTC)
    assert c._card is not None
    assert c._card.expires_at == datetime(2026, 10, 30, 6, 45, tzinfo=UTC)


async def test_commander_refresh_symbol_new_symbol_gets_full_validity_from_its_meeting(
    tmp_path: Path,
) -> None:
    eng = _Directed2({"SANDUSDT": [Bias.LONG], "QNTUSDT": [Bias.LONG]})
    eng.picks = ["SANDUSDT"]
    c, _log, clock = make(
        tmp_path,
        eng,
        _R0,
        ttl=timedelta(minutes=140),
        symbol_min_interval=timedelta(minutes=5),
    )
    await c.refresh(pool(_R0, entry("SANDUSDT"), entry("QNTUSDT")), "scheduled")
    assert c._card is not None and c._card.for_symbol("QNTUSDT") is None
    cs = await _event(c, clock, "QNTUSDT", datetime(2026, 10, 30, 6, 12, tzinfo=UTC))
    assert cs.expires_at == datetime(2026, 10, 30, 8, 32, tzinfo=UTC)


async def test_commander_refresh_symbol_same_direction_keeps_card_expiry_and_never_shortens(
    tmp_path: Path,
) -> None:
    c, clock = await _event_expiry_case(
        tmp_path, {"ETHUSDT": [Bias.SHORT, Bias.LONG, Bias.LONG], "SANDUSDT": [Bias.LONG] * 2}
    )
    sand = await _event(c, clock, "SANDUSDT", datetime(2026, 10, 30, 5, 0, tzinfo=UTC))
    assert sand.expires_at == datetime(2026, 10, 30, 6, 45, tzinfo=UTC)
    await _event(c, clock, "ETHUSDT", datetime(2026, 10, 30, 5, 11, tzinfo=UTC))
    again = await _event(c, clock, "ETHUSDT", datetime(2026, 10, 30, 5, 40, tzinfo=UTC))
    assert again.issued_at == datetime(2026, 10, 30, 5, 40, tzinfo=UTC)
    assert again.expires_at == datetime(2026, 10, 30, 7, 31, tzinfo=UTC)


async def test_commander_refresh_symbol_neutral_kept_by_setting_does_not_reset_expiry(
    tmp_path: Path,
) -> None:
    c, clock = await _event_expiry_case(tmp_path, {"ETHUSDT": [Bias.LONG, Bias.NO_TRADE]})
    c.set_neutral_flip_ignored(lambda: True)
    kept = await _event(c, clock, "ETHUSDT", datetime(2026, 10, 30, 5, 11, tzinfo=UTC))
    assert (kept.bias, kept.issued_at) == (Bias.LONG, _R0)
    assert kept.expires_at == datetime(2026, 10, 30, 6, 45, tzinfo=UTC)


async def test_commander_flip_pending_keeps_expiry_and_confirm_flip_resets_it(
    tmp_path: Path,
) -> None:
    c, clock = await _event_expiry_case(
        tmp_path, {"ETHUSDT": [Bias.SHORT, Bias.LONG]}, flip_confirm=2
    )
    pending: list[tuple[str, Any]] = []
    c.set_on_flip_pending(lambda sym, cand: pending.append((sym, cand)))
    held = await _event(c, clock, "ETHUSDT", datetime(2026, 10, 30, 5, 11, tzinfo=UTC))
    assert (held.bias, held.issued_at) == (Bias.SHORT, _R0)
    assert held.expires_at == datetime(2026, 10, 30, 6, 45, tzinfo=UTC)
    [(_sym, cand)] = pending
    clock[0] = datetime(2026, 10, 30, 5, 20, tzinfo=UTC)
    assert c.confirm_flip("ETHUSDT", cand, "两个模型都答是", {})
    assert c._card is not None
    cs = c._card.for_symbol("ETHUSDT")
    assert cs is not None and cs.bias is Bias.LONG
    assert cs.expires_at == datetime(2026, 10, 30, 7, 31, tzinfo=UTC)


async def test_commander_current_keeps_a_flipped_symbol_past_the_card_expiry(
    tmp_path: Path,
) -> None:
    c, clock = await _event_expiry_case(
        tmp_path, {"ETHUSDT": [Bias.SHORT, Bias.LONG], "SANDUSDT": [Bias.LONG]}
    )
    await _event(c, clock, "ETHUSDT", datetime(2026, 10, 30, 5, 11, tzinfo=UTC))
    late = datetime(2026, 10, 30, 6, 50, tzinfo=UTC)
    clock[0] = late
    view = c.current(late)
    assert view is not None and view.is_valid(late)
    assert [s.symbol for s in view.symbols] == ["ETHUSDT"]
    eth = view.for_symbol("ETHUSDT")
    assert eth is not None and eth.allowed_signals == [SignalKind.VOLUME_BREAKOUT]
    pos = PositionRecord.model_construct(symbol="ETHUSDT", side=Side.SHORT)
    assert JevKeeper._card_rule(pos, view, late) == "新作战卡方向改成 long"
    assert c.refresh_reason(late, pool(late, entry("ETHUSDT")), {}) == "scheduled"
    c2, _, _ = make(tmp_path, FakeEngine([]), late, card_path=tmp_path / "battle_card.json")
    restored = c2.current(late)
    assert restored is not None and [s.symbol for s in restored.symbols] == ["ETHUSDT"]
    gone = datetime(2026, 10, 30, 7, 31, tzinfo=UTC)
    assert c.current(gone) is None
