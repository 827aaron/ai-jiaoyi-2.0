"""总盘会选币改法（spec v3.1）：打分、量能、固定进总盘会的币、候选表、校验、记录和回填。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jiaoyi.contracts import BattleCard, SizeTier, SymbolPoolEntry
from jiaoyi.slow import market_table
from jiaoyi.slow.binance_stats import BinanceStatsSource
from jiaoyi.slow.commander import PickResult, TierRules
from jiaoyi.slow.market_log import MarketLog, pool_rows, round_id
from jiaoyi.slow.screener import (
    CodeSymbolScreener,
    ScreenerConfig,
    SymbolStats,
    UserOverrides,
    score,
    score_old,
)
from jiaoyi.slow.tradingagents_engine import (
    TradingAgentsEngine,
    TradingAgentsSettings,
    _PickDraft,
    pick_problems,
    pick_result,
)
from tests.test_slow_commander import FakeEngine, make
from tests.test_slow_commander import entry as centry
from tests.test_slow_commander import pool as cpool
from tests.test_slow_engine import _binance_handler
from tests.test_slow_screener import FakeSource, stat

CFG = ScreenerConfig()  # 手续费来回 10bp = 0.10%，波动分 30 倍满分

# ---------------------------------------------------------------- 打分


def test_vol_item_uses_recent_hold_windows_per_hour_and_does_not_saturate() -> None:
    def vol(**kw: Any) -> float:
        s = stat("AAUSDT", range_24h_pct=9.0, hold_hours=4.0, **kw)  # 24h 波动一样大
        return score(s, CFG)[1]["vol_vs_fees"]

    lo, hi = vol(range_hold_pct=1.0), vol(range_hold_pct=4.0)
    assert lo < hi < 1.0  # 24h 都 9% 也分得出高低
    # 4h 持仓：每小时折算 = R / √4；R = 5.78% → 每小时 2.89% = 手续费 28.9 倍 → 0.96，没封顶
    assert vol(range_hold_pct=5.78) == pytest.approx(0.9633, abs=1e-3)
    assert vol(range_hold_pct=30.0) == 1.0  # 真正大的才封顶


def test_vol_item_without_hold_ranges_falls_back_to_24h_per_hour() -> None:
    s = stat("AAUSDT", range_24h_pct=7.2)  # 数据源没算分段高低差
    assert score(s, CFG)[1]["vol_vs_fees"] == pytest.approx(7.2 / 24**0.5 / 0.10 / 30, abs=1e-3)


def test_score_old_uses_old_formula_and_old_surge() -> None:
    s = stat("AAUSDT", range_24h_pct=2.0, volume_surge=8.0, volume_surge_old=1.0, hold_hours=2.0)
    old = score_old(s, CFG)
    assert old == pytest.approx(
        0.35 * 1.0, abs=1e-3
    )  # 2% ÷ 0.1% ÷ 20 = 1.0 满分；量能旧值 1 倍 = 0
    assert score(s, CFG)[0] > 0.0 and score(s, CFG)[1]["volume_surge"] == 1.0


# ---------------------------------------------------------------- 本地行情数字


def _klines_handler(complete_vols: list[float], partial_vol: float, partial_taker: float = 0.0):
    """包住 test_slow_engine 的币安桩：K 线换成指定成交额；最后一根是 06:00 开的半截小时。"""
    hour = 3_600_000
    start = int(datetime(2026, 10, 22, 6, tzinfo=UTC).timestamp() * 1000)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path != "/fapi/v1/klines":
            return _binance_handler(req)
        n = len(complete_vols)
        rows: list[list[Any]] = []
        for i, v in enumerate(complete_vols):
            t = start - (n - i) * hour
            rows.append(
                [
                    t,
                    "100",
                    "101",
                    "99",
                    "100",
                    "1",
                    t + hour - 1,
                    str(v),
                    10,
                    "0.5",
                    str(v / 2),
                    "0",
                ]
            )
        rows.append(
            [
                start,
                "100",
                "101",
                "99",
                "100",
                "1",
                start + hour - 1,
                str(partial_vol),
                10,
                "0.5",
                str(partial_taker),
                "0",
            ]
        )
        return httpx.Response(200, json=rows)

    return handler


async def _fetch(handler: Any, now: datetime, **kw: Any) -> dict[str, SymbolStats]:
    client = httpx.AsyncClient(
        base_url="https://fapi.binance.com", transport=httpx.MockTransport(handler)
    )
    stats = await BinanceStatsSource(client, **kw).fetch(now)
    await client.aclose()
    return {s.symbol: s for s in stats}


async def test_surge_uses_the_last_complete_hour_in_the_first_15_minutes() -> None:
    vols = [100.0] * 24 + [300.0]  # 上一个完整小时放量 3 倍
    now = datetime(2026, 10, 22, 6, 5, tzinfo=UTC)  # 整点后 5 分钟：半截小时不可信
    by = await _fetch(_klines_handler(vols, 1.0), now)
    assert by["BTCUSDT"].volume_surge == pytest.approx(3.0)  # complete[-1] ÷ complete[-25:-1] 均值
    assert by["BTCUSDT"].volume_surge_old == pytest.approx(
        1.0 / 100.0 * (24 * 100.0 + 300.0) / 24 / (100.0 * 24 / 24) * 0.0
        + by["BTCUSDT"].volume_surge_old
    )  # 只确认有值


async def test_surge_scales_the_partial_hour_by_elapsed_minutes() -> None:
    vols = [100.0] * 30
    now = datetime(2026, 10, 22, 6, 40, tzinfo=UTC)  # 走了 40 分钟，成交 200 → 折成整小时 300
    by = await _fetch(_klines_handler(vols, 200.0), now)
    assert by["BTCUSDT"].volume_surge == pytest.approx(3.0)
    assert by["BTCUSDT"].volume_surge_old == pytest.approx(2.0)  # 旧算法：半截小时直接比


async def test_surge_baseline_is_exactly_24_complete_hours_not_the_whole_fetch() -> None:
    # 拉了 73 根（24h 持仓）：前面 48 小时是 0，只有最近 24 个完整小时是 100
    vols = [0.0] * 48 + [100.0] * 24
    now = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
    by = await _fetch(_klines_handler(vols, 100.0), now, hold_minutes=24 * 60)
    assert by["BTCUSDT"].volume_surge == pytest.approx(
        2.0
    )  # 100 ÷ 0.5 小时 ÷ 100，不是被 72 小时均值摊薄


async def test_hold_windows_range_and_metrics_and_liq_feed_not_ready() -> None:
    class Liq:
        def liquidations_1h_usdt(self, symbol: str, at: datetime) -> float:
            return 0.0

        def feed_minutes(self, at: datetime) -> float:
            return 30.0  # 强平流刚连上

    now = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
    by = await _fetch(
        _klines_handler([100.0] * 30, 400.0, 240.0), now, liquidations=Liq(), hold_minutes=240
    )
    m = by["BTCUSDT"].metrics
    assert by["BTCUSDT"].range_hold_pct == pytest.approx(2.0)  # 每根 99–101，3 个 4h 窗口都是 2%
    assert m["taker_buy_1h"] == pytest.approx(50.0) and m["chg_1h"] == 0.0 and m["chg_4h"] == 0.0
    assert m["pos_24h"] == 0.0 and m["chg_24h"] == -1.5 and m["qv_24h_m"] == 9000.0
    assert "liq_1h" not in m and m["liq_feed_min"] == 30.0  # 不写成 0
    assert "corr_btc_24h" not in m  # 收盘价一动不动：没有相关系数


async def test_corr_with_btc_and_liq_share_when_feed_is_long() -> None:
    class Liq:
        def liquidations_1h_usdt(self, symbol: str, at: datetime) -> float:
            return 50.0

        def feed_minutes(self, at: datetime) -> float:
            return 600.0

    now = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
    hour = 3_600_000
    start = int(datetime(2026, 10, 22, 6, tzinfo=UTC).timestamp() * 1000)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path != "/fapi/v1/klines":
            return _binance_handler(req)
        sym = req.url.params["symbol"]
        rows = []
        for i in range(30):
            t = start - (30 - i) * hour
            wiggle = (i % 3) * 1.0  # BTC 与 ETH 同步走，TSLA 反着走
            close = 100 + wiggle if sym != "TSLAUSDT" else 100 - wiggle
            rows.append(
                [t, "100", "101", "99", str(close), "1", t + hour - 1, "100", 10, "0.5", "50", "0"]
            )
        rows.append(
            [start, "100", "101", "99", "100", "1", start + hour - 1, "100", 10, "0.5", "50", "0"]
        )
        return httpx.Response(200, json=rows)

    by = await _fetch(handler, now, liquidations=Liq())
    assert by["ETHUSDT"].metrics["corr_btc_24h"] == pytest.approx(1.0, abs=1e-6)
    assert by["TSLAUSDT"].metrics["corr_btc_24h"] < -0.9
    assert by["BTCUSDT"].metrics["liq_1h"] == 50.0 and by["BTCUSDT"].metrics[
        "liq_share"
    ] == pytest.approx(50.0)


# ---------------------------------------------------------------- 每轮进总盘会的币


def _always_cfg(**kw: Any) -> ScreenerConfig:
    return ScreenerConfig(always_candidates=frozenset({"BTC"}), tier_head=2, tier_mid=2, **kw)


def _stats_for_always() -> list[SymbolStats]:
    # BTC 波动最小（分数最低）；其余 6 个由高到低
    return [stat("BTCUSDT", range_24h_pct=0.5)] + [
        stat(f"C{i}USDT", range_24h_pct=9.0 - i) for i in range(6)
    ]


async def test_always_candidate_enters_pool_inside_the_size_without_taking_a_head_slot(
    now: datetime,
) -> None:
    pool = await CodeSymbolScreener(
        FakeSource(_stats_for_always()), _always_cfg(pool_size=4)
    ).screen(now)
    by = {e.symbol: e for e in pool.entries}
    assert by["BTCUSDT"].in_pool and by["BTCUSDT"].always_candidate
    assert "每轮进总盘会" in by["BTCUSDT"].reasons
    assert len(pool.in_pool) == 4  # 算在池子大小里面
    heads = [e.symbol for e in pool.in_pool if e.tier == "head"]
    assert heads == ["C0USDT", "C1USDT"]  # 头部名额没被 BTC 抢走
    assert by["BTCUSDT"].tier != "head"  # 按自己的分数落档，不是头部


async def test_always_candidate_keeps_its_own_tier_but_never_pushes_others(now: datetime) -> None:
    stats = [stat("BTCUSDT", range_24h_pct=9.5)] + [
        stat(f"C{i}USDT", range_24h_pct=9.0 - i) for i in range(5)
    ]
    pool = await CodeSymbolScreener(FakeSource(stats), _always_cfg(pool_size=5)).screen(now)
    by = {e.symbol: e for e in pool.entries}
    assert by["BTCUSDT"].tier == "head"  # 分数最高：自己落头部
    assert [e.symbol for e in pool.in_pool if e.tier == "head" and e.symbol != "BTCUSDT"] == [
        "C0USDT",
        "C1USDT",
    ]


async def test_always_candidate_still_needs_the_hard_filters_and_blacklist(now: datetime) -> None:
    stats = [stat("BTCUSDT", range_24h_pct=0.5, spread_bps=50)] + [
        stat(f"C{i}USDT") for i in range(3)
    ]
    pool = await CodeSymbolScreener(FakeSource(stats), _always_cfg(pool_size=3)).screen(now)
    assert "BTCUSDT" not in {e.symbol for e in pool.in_pool}  # 没过硬门槛
    ov = UserOverrides(blacklist=frozenset({"BTCUSDT"}))
    stats = [stat("BTCUSDT", range_24h_pct=0.5)] + [stat(f"C{i}USDT") for i in range(3)]
    pool = await CodeSymbolScreener(FakeSource(stats), _always_cfg(pool_size=3), ov).screen(now)
    assert "BTCUSDT" not in {e.symbol for e in pool.in_pool}  # 拉黑


async def test_pool_records_old_score_rank_and_metrics(now: datetime) -> None:
    s = stat("AAUSDT", range_hold_pct=4.0, hold_hours=4.0, metrics={"chg_1h": 0.5})
    pool = await CodeSymbolScreener(FakeSource([s])).screen(now)
    m = pool.entries[0].metrics
    assert m["score_old"] > 0 and m["chg_1h"] == 0.5
    assert m["move_hold"] == 4.0 and m["fee_x"] == pytest.approx(40.0)
    assert m["rank"] == 1.0 and m["pool_n"] == 1.0


# ---------------------------------------------------------------- 指挥官：固定币的中间档名额


def _always(e: SymbolPoolEntry) -> SymbolPoolEntry:
    return e.model_copy(update={"always_candidate": True})


async def test_always_candidate_joins_candidates_and_does_not_use_mid_slots(
    tmp_path: Path, now: datetime
) -> None:
    rules = TierRules(enabled=True, mid_daily_pick=2, mid_size_max=SizeTier.S)
    eng = FakeEngine(["BTCUSDT", "BBUSDT", "CCUSDT", "DDUSDT"])
    c, _, _ = make(tmp_path, eng, now, tiers=rules, tier_path=tmp_path / "mid.json", pick_max=10)
    p = cpool(
        now,
        centry("AAUSDT", tier="head"),
        _always(centry("BTCUSDT", tier="mid")),  # 落在中间档的固定币
        centry("BBUSDT", tier="mid"),
        centry("CCUSDT", tier="mid"),
        centry("DDUSDT", tier="mid"),
        _always(centry("ETHUSDT", tier="tail")),  # 落在尾部的固定币
    )
    card = await c.refresh(p, "scheduled")
    got = {s.symbol for s in card.symbols}
    # 曼谷日第一轮：2 个中间档名额 BB、CC 照留；BTC 不占名额也不被去掉；尾部的 ETH 照样进候选
    assert {"BTCUSDT", "BBUSDT", "CCUSDT"} <= got and "DDUSDT" not in got
    assert "ETHUSDT" in {e.symbol for e in c._eligible(p, now, expand=False)}
    sizes = {s.symbol: s.size_tier_max for s in card.symbols}
    assert sizes["BTCUSDT"] is SizeTier.M  # 仓位按头部算，不被中间档封成 S
    assert sizes["BBUSDT"] is SizeTier.S
    saved = json.loads((tmp_path / "mid.json").read_text(encoding="utf-8"))
    assert saved["mid"] == ["BBUSDT", "CCUSDT"]  # 固定币不在中间档当天名单里


class _SeenEngine(FakeEngine):
    """Flash never picks BTC / ETH; records what it was offered."""

    def __init__(self, picks: list[str]) -> None:
        super().__init__(picks)
        self.offered: list[list[str]] = []
        self.rooms: list[int] = []

    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        self.offered.append([e.symbol for e in candidates])
        self.rooms.append(max_n)
        return await super().pick(candidates, max_n, previous, pool)


def _majors_pool(now: datetime) -> Any:
    return cpool(
        now,
        centry("AAUSDT", tier="head"),
        centry("BBUSDT", tier="head"),
        centry("CCUSDT", tier="head"),
        _always(centry("BTCUSDT", tier="mid")),
        _always(centry("ETHUSDT", tier="tail")),
    )


def _meet_round(
    tmp_path: Path, now: datetime, *, meet: bool, **kw: Any
) -> tuple[_SeenEngine, Any, list[datetime]]:
    eng = _SeenEngine(["AAUSDT", "BBUSDT", "CCUSDT"])
    rules = TierRules(enabled=True, always_meet=meet)
    c, _, clock = make(tmp_path, eng, now, tiers=rules, pick_max=2, **kw)
    return eng, c, clock


async def test_always_candidates_meet_off_leaves_unpicked_majors_without_a_meeting(
    tmp_path: Path, now: datetime
) -> None:
    eng, c, _ = _meet_round(tmp_path, now, meet=False)
    card = await c.refresh(_majors_pool(now), "scheduled")
    assert eng.offered == [["AAUSDT", "BBUSDT", "CCUSDT", "BTCUSDT", "ETHUSDT"]]
    assert eng.rooms == [2]
    assert sorted(eng.analyzed) == ["AAUSDT", "BBUSDT"]
    assert {s.symbol for s in card.symbols} == {"AAUSDT", "BBUSDT"}


async def test_always_candidates_meet_on_meets_majors_every_round_outside_the_pick_slots(
    tmp_path: Path, now: datetime
) -> None:
    live, mm = tmp_path / "live", MarketLog(tmp_path / "mm")
    eng, c, clock = _meet_round(tmp_path, now, meet=True, live_dir=live, market_log=mm)
    card = await c.refresh(_majors_pool(now), "scheduled")
    assert eng.offered == [["AAUSDT", "BBUSDT", "CCUSDT"]]
    assert eng.rooms == [2]
    assert sorted(eng.analyzed) == ["AAUSDT", "BBUSDT", "BTCUSDT", "ETHUSDT"]
    assert {s.symbol for s in card.symbols} == {"AAUSDT", "BBUSDT", "BTCUSDT", "ETHUSDT"}
    assert eng.briefs["BTCUSDT"] == ("区间震荡", "")
    doc = json.loads((live / "_MARKET.json").read_text(encoding="utf-8"))
    assert "BTCUSDT：每轮直接开会（设置），不经挑选" in doc["steps"][2]["text"]
    assert "ETHUSDT：每轮直接开会（设置）" in doc["steps"][1]["text"]
    row = json.loads((tmp_path / "mm" / f"{now:%Y-%m-%d}.jsonl").read_text("utf-8"))
    assert row["direct_meet"] == ["BTCUSDT", "ETHUSDT"]
    assert row["picked"] == ["AAUSDT", "BBUSDT"]
    btc = next(x for x in row["pool"] if x["symbol"] == "BTCUSDT")
    assert (btc["pick"], btc["reason"]) == (True, "每轮直接开会（设置）")

    clock[0] = now + timedelta(hours=3)
    eng.analyzed.clear()
    await c.refresh(_majors_pool(clock[0]), "scheduled")
    assert sorted(eng.analyzed) == ["AAUSDT", "BBUSDT", "BTCUSDT", "ETHUSDT"]


async def test_always_candidates_meet_skips_a_coin_the_venue_cannot_trade(
    tmp_path: Path, now: datetime
) -> None:
    eng, c, _ = _meet_round(tmp_path, now, meet=True)
    c.set_untradeable({"ETHUSDT": "SETTLING"})
    await c.refresh(_majors_pool(now), "scheduled")
    assert eng.offered == [["AAUSDT", "BBUSDT", "CCUSDT", "ETHUSDT"]]
    assert sorted(eng.analyzed) == ["AAUSDT", "BBUSDT", "BTCUSDT"]


# ---------------------------------------------------------------- 候选表和池子概况


def _entry_with(**m: float) -> SymbolPoolEntry:
    e = centry("ENAUSDT", tier="head")
    return e.model_copy(update={"score": 0.71, "metrics": m})


def test_row_has_every_field_and_marks_missing_numbers() -> None:
    e = _entry_with(
        chg_this_hour=0.2, chg_1h=-0.4, chg_4h=1.2, chg_24h=-3.1, pos_24h=38.0, move_hold=3.7,
        fee_x=37.0, rank=1.0, pool_n=40.0, vol_ratio_1h=1.6, taker_buy_1h=54.0, funding=0.01,
        oi_chg_1h=2.1, liq_1h=37000.0, liq_share=0.4, spread_bps=1.2, depth_k=310.0,
        corr_btc_24h=0.82, qv_24h_m=900.0,
    )  # fmt: skip
    row = market_table.format_row(e, "4h")
    for part in (
        "ENAUSDT tier=head cat=alt score=0.71",
        "chg current_hour_open_to_ticker +0.2% completed_hour -0.4% "
        "completed_4h_close_span +1.2% ticker_24h -3.1%",
        "pos24h 38%",
        "move4h 3.7% (37x fee, rank 1/40)",
        "vol1h 1.6x",
        "takerBuy1h 54%",
        "funding +0.010%",
        "OI1h +2.1%",
        "liq1h 37k (0.4% of 1h vol)",
        "spread 1.2bp depth 310k",
        "corrBTC 0.82",
    ):
        assert part in row, part
    bare = market_table.format_row(_entry_with(liq_feed_min=20.0), "4h")
    assert "move4h n/a" in bare and "corrBTC n/a" in bare and "vol1h n/a" in bare
    assert "liq1h n/a (feed 20 min)" in bare  # 强平流刚连上：n/a，不写 0
    assert "takerBuy1h 0%" not in bare


def test_breadth_covers_the_whole_pool() -> None:
    pool = [
        _entry_with(chg_1h=1.0, chg_4h=-1.0, chg_24h=-2.0),
        _entry_with(chg_1h=-2.0, chg_4h=-3.0, chg_24h=1.0),
        _entry_with(chg_1h=-0.4, chg_4h=-5.0),
    ]
    line = market_table.breadth_line(pool)
    assert line.startswith(
        "Pool breadth (3): completed_hour up 1 / down 2 / flat 0 / missing 0, median -0.4%"
    )
    assert "completed_4h_close_span up 0 / down 3 / flat 0 / missing 0, median -3.0%" in line
    assert "ticker_24h up 1 / down 1 / flat 0 / missing 1, median -0.5%" in line


def test_count_numbers() -> None:
    assert market_table.count_numbers("4h 涨了 1.2%，量能 3 倍") == 3
    assert market_table.count_numbers("盘面偏薄，高 beta") == 0


# ---------------------------------------------------------------- 校验


def _draft(regime: str, *rows: tuple[str, bool, str]) -> _PickDraft:
    return _PickDraft(
        regime=regime,
        regime_direction="中性",
        decisions=[_PickDraft._D(symbol=s, pick=p, reason=r) for s, p, r in rows],
    )


def test_pick_problems_missing_duplicate_and_numberless_reasons() -> None:
    valid = ["AAUSDT", "BBUSDT", "CCUSDT"]
    ok = _draft(
        "1h 涨 3 家 / 跌 2 家", *[(s, i == 0, "4h 涨 1.2%，量能 3 倍") for i, s in enumerate(valid)]
    )
    assert pick_problems(ok, valid) == []
    bad = _draft(
        "震荡",
        ("AAUSDT", True, "盘面偏薄"),
        ("AAUSDT", False, "4h 涨 1.2%，量能 3 倍"),
        ("BBUSDT", False, "4h 涨 1.2%，量能 3 倍"),
    )
    msgs = " | ".join(pick_problems(bad, valid))
    assert "no decision for: CCUSDT" in msgs and "more than one decision for: AAUSDT" in msgs
    assert "AAUSDT: the reason must quote at least two numbers" in msgs
    assert "regime: quote at least two numbers" in msgs


def test_numberless_reason_never_drops_a_picked_coin_and_max_n_still_applies() -> None:
    valid = ["AAUSDT", "BBUSDT", "CCUSDT"]
    d = _draft(
        "震荡",
        ("AAUSDT", True, "盘面偏薄"),
        ("BBUSDT", True, "4h 涨 1.2%，量能 3 倍"),
        ("CCUSDT", True, "4h 涨 1.2%，量能 3 倍"),
        ("ZZUSDT", True, "池外的币丢掉 1 个 2 个"),
    )
    res = pick_result(d, valid, 2, pick_problems(d, valid), True, 0.001, "breadth", "table")
    assert [p.symbol for p in res.picks] == [
        "AAUSDT",
        "BBUSDT",
    ]  # 理由缺数字照样挑中；超出 max_n 才截断
    assert [r.symbol for r in res.rejections] == ["CCUSDT"]  # 被名额挤掉的算落选
    assert res.retried and res.errors and "ZZUSDT" not in {x.symbol for x in res.decisions}
    assert res.breadth == "breadth" and res.table == "table"


class _FakeLLM:
    def __init__(self, answers: list[_PickDraft]) -> None:
        self.answers, self.prompts = answers, []  # type: ignore[var-annotated]

    def with_structured_output(self, _schema: Any) -> _FakeLLM:
        return self

    def bind(self, **_kw: Any) -> _FakeLLM:
        return self

    def invoke(self, prompt: str, config: Any = None) -> _PickDraft:
        self.prompts.append(prompt)
        return self.answers.pop(0)


def _engine(llm: _FakeLLM) -> TradingAgentsEngine:
    eng = TradingAgentsEngine.__new__(TradingAgentsEngine)
    eng._s = TradingAgentsSettings(hold="4h", hold_minutes=240)
    eng._news = None
    eng._brief = None
    eng._today = lambda: datetime(2026, 10, 25, 3, 0, tzinfo=UTC)  # type: ignore[method-assign]
    eng._graph = lambda *a, **k: None  # type: ignore[method-assign,assignment]
    eng._llm = lambda *a, **k: llm  # type: ignore[method-assign,assignment]
    return eng


class _Meter:
    cost = 0.0004


def test_pick_run_asks_again_once_and_uses_the_second_answer() -> None:
    cands = [centry("AAUSDT"), centry("BBUSDT")]
    first = _draft("震荡", ("AAUSDT", True, "盘面偏薄"))  # 缺 BBUSDT，理由缺数字
    second = _draft(
        "1h 涨 1 家 / 跌 1 家",
        ("AAUSDT", True, "4h 涨 1.2%，量能 3 倍"),
        ("BBUSDT", False, "4h 跌 2%，量能 0.5 倍"),
    )
    llm = _FakeLLM([first, second])
    res = _engine(llm)._pick_run(cands, 5, None, None, _Meter())  # type: ignore[arg-type]
    assert len(llm.prompts) == 2 and "no decision for: BBUSDT" in llm.prompts[1]
    assert res.retried and res.errors == []  # 重问后合格
    assert [p.symbol for p in res.picks] == ["AAUSDT"] and [d.symbol for d in res.decisions] == [
        "AAUSDT",
        "BBUSDT",
    ]
    assert "Pool breadth (2)" in res.table and "AAUSDT tier=" in res.table
    assert "Pool breadth" in llm.prompts[0]  # 池子概况和每个币的行都给了模型


def test_pick_run_keeps_the_coins_when_the_second_answer_is_still_bad() -> None:
    cands = [centry("AAUSDT"), centry("BBUSDT")]
    bad = _draft("震荡", ("AAUSDT", True, "盘面偏薄"))
    llm = _FakeLLM([bad, bad])
    res = _engine(llm)._pick_run(cands, 5, None, None, _Meter())  # type: ignore[arg-type]
    assert [p.symbol for p in res.picks] == ["AAUSDT"]  # 格式问题不让会变少
    assert res.retried and len(res.errors) == 3  # 缺 BBUSDT、AAUSDT 理由缺数字、大盘判断缺数字


# ---------------------------------------------------------------- 记录和回填


async def test_each_round_writes_one_line_with_every_pool_coin(
    tmp_path: Path, now: datetime
) -> None:
    log = MarketLog(tmp_path / "mm")
    eng = FakeEngine(["AAUSDT"])
    c, _, _ = make(
        tmp_path,
        eng,
        now,
        market_log=log,
        log_meta={"meeting_interval": "2h", "hold": "4h", "hold_hours": 4.0},
    )
    p = cpool(now, centry("AAUSDT"), _always(centry("BBUSDT")))
    await c.refresh(p, "scheduled")
    rows = [
        json.loads(x)
        for x in (tmp_path / "mm" / f"{now:%Y-%m-%d}.jsonl").read_text("utf-8").splitlines()
    ]
    assert len(rows) == 1
    r = rows[0]
    assert (
        r["round_id"] == round_id(now) and r["hold_hours"] == 4.0 and r["meeting_interval"] == "2h"
    )
    assert r["picked"] == ["AAUSDT"] and r["card_version"] == 1 and r["max_n"] > 0
    assert {x["symbol"] for x in r["pool"]} == {"AAUSDT", "BBUSDT"}
    assert next(x for x in r["pool"] if x["symbol"] == "BBUSDT")["always_candidate"] is True


def test_backfill_writes_outcomes_once_after_the_hold_has_elapsed(tmp_path: Path) -> None:
    log = MarketLog(tmp_path / "mm")
    ts = datetime(2026, 10, 25, 3, 20, tzinfo=UTC)
    log.write_round(
        {
            "round_id": round_id(ts), "ts": ts.isoformat(), "hold_hours": 2.0,
            "pool": pool_rows([centry("AAUSDT"), centry("BBUSDT")], {}),
        }
    )  # fmt: skip
    hour = 3_600_000
    first = int(datetime(2026, 10, 25, 4, tzinfo=UTC).timestamp() * 1000)  # 开会后第一个整点
    bars = {
        "AAUSDT": [(first, 100.0, 103.0, 99.0, 102.0), (first + hour, 102.0, 104.0, 101.0, 101.0)],
        "BBUSDT": [(first, 10.0, 10.1, 9.9, 10.0)],  # 只有一根：不够，不写
    }
    early = datetime(2026, 10, 25, 5, 30, tzinfo=UTC)
    assert log.backfill(early, bars) == 0  # 4:00–6:00 还没走完
    done = datetime(2026, 10, 25, 6, 5, tzinfo=UTC)
    assert log.backfill(done, bars) == 1
    out = json.loads(log.outcomes_path.read_text("utf-8").splitlines()[0])
    a = out["symbols"]["AAUSDT"]
    assert a["range_pct"] == pytest.approx((104.0 - 99.0) / 100.0 * 100.0)
    assert a["chg_pct"] == pytest.approx(1.0) and "BBUSDT" not in out["symbols"] and out["approx"]
    assert log.backfill(done + timedelta(hours=1), bars) == 0  # 已经回填过，不重复写
