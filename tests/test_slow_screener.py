from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Category
from jiaoyi.execution.exchange import LotFilter
from jiaoyi.execution.lot_rules import ExchangeInfoLotRules
from jiaoyi.slow.screener import (
    CodeSymbolScreener,
    ScreenerConfig,
    SymbolStats,
    UserOverrides,
    classify,
)
from tests.fakes import FakeExchange


def stat(sym: str, **kw: float) -> SymbolStats:
    base = {
        "quote_volume_24h_usdt": 100e6,
        "spread_bps": 1.0,
        "depth_usdt": 500e3,
        "listed_days": 100,
        "range_24h_pct": 5.0,
    }
    return SymbolStats(symbol=sym, **{**base, **kw})  # type: ignore[arg-type]


class FakeSource:
    def __init__(self, stats: list[SymbolStats]) -> None:
        self.stats = stats

    async def fetch(self, now: datetime) -> list[SymbolStats]:
        return self.stats


async def test_hard_filters_reject(now: datetime) -> None:
    src = FakeSource(
        [
            stat("OKUSDT"),
            stat("THINUSDT", quote_volume_24h_usdt=1e6),
            stat("WIDEUSDT", spread_bps=20),
            stat("SHALLOWUSDT", depth_usdt=1e3),
            stat("NEWUSDT", listed_days=3),
        ]
    )
    pool = await CodeSymbolScreener(src).screen(now)
    by = {e.symbol: e for e in pool.entries}
    assert [e.symbol for e in pool.in_pool] == ["OKUSDT"]
    assert by["NEWUSDT"].filters.failed_reasons == ["上线不满 7 天"]
    assert not by["THINUSDT"].in_pool and not by["WIDEUSDT"].in_pool


async def test_score_orders_pool_and_size_limit(now: datetime) -> None:
    src = FakeSource([stat(f"C{i}USDT", range_24h_pct=0.1 * (i + 1)) for i in range(10)])
    pool = await CodeSymbolScreener(src, ScreenerConfig(pool_size=3)).screen(now)
    assert [e.symbol for e in pool.in_pool] == ["C9USDT", "C8USDT", "C7USDT"]


async def test_blacklist_beats_pin_and_pool(now: datetime) -> None:
    src = FakeSource([stat("AAUSDT", range_24h_pct=50), stat("BBUSDT")])
    ov = UserOverrides(pinned=frozenset({"AAUSDT"}), blacklist=frozenset({"AAUSDT"}))
    pool = await CodeSymbolScreener(src, overrides=ov).screen(now)
    a = next(e for e in pool.entries if e.symbol == "AAUSDT")
    assert a.blacklisted and not a.in_pool and not a.pinned


async def test_pin_bypasses_category_cap_but_not_hard_filters(now: datetime) -> None:
    src = FakeSource(
        [stat("AAAUSDT", range_24h_pct=9), stat("BBBUSDT", range_24h_pct=8),
         stat("PINUSDT", range_24h_pct=0.1), stat("BADUSDT", listed_days=1)]
    )  # fmt: skip
    ov = UserOverrides(pinned=frozenset({"PINUSDT", "BADUSDT"}), category_caps={Category.ALT: 2})
    pool = await CodeSymbolScreener(src, overrides=ov).screen(now)
    names = {e.symbol for e in pool.in_pool}
    assert names == {"PINUSDT", "AAAUSDT"}  # 置顶占名额但不受上限限制，BBB 被类别上限挡掉
    bad = next(e for e in pool.entries if e.symbol == "BADUSDT")
    assert bad.pinned and not bad.in_pool


async def test_empty_source_fails_closed(now: datetime) -> None:
    with pytest.raises(RuntimeError):
        await CodeSymbolScreener(FakeSource([])).screen(now)


def test_classify() -> None:
    assert classify("BTCUSDT") is Category.MAJOR
    assert classify("TSLAUSDT") is Category.US_TECH
    assert classify("COINUSDT") is Category.CRYPTO_STOCK
    assert classify("XAUUSDT") is Category.PRECIOUS_METAL
    assert classify("PEPEUSDT") is Category.ALT
    assert classify("PAXGUSDT") is Category.PRECIOUS_METAL  # 链上黄金按金属算
    assert classify("INTCUSDT", "tradifi_perpetual equity") is Category.US_TECH  # 名单外按类型识别
    assert classify("XPDUSDT", "") is Category.PRECIOUS_METAL
    assert classify("WTIUSDT", "tradifi_perpetual commodity") is Category.PRECIOUS_METAL
    assert classify("NEWUSDT", "tradifi_perpetual") is Category.ALT  # 认不出类型：不当美股
    assert classify("0700USDT", "tradifi_perpetual hk_equity") is Category.ALT  # 港股不套美股时段


async def test_screener_reports_its_own_status(now: datetime, tmp_path: Path) -> None:
    from jiaoyi.contracts import Health, Role
    from jiaoyi.core.recorder import RoleRecorder
    from jiaoyi.core.store import JsonlDecisionLog

    log = JsonlDecisionLog(tmp_path)
    src = FakeSource([])
    scr = CodeSymbolScreener(src, recorder=RoleRecorder(Role.SCREENER, log), tz="Asia/Bangkok")
    scr.heartbeat(now)  # 还没跑过
    st = log.read_status()["screener"]
    assert st.health is Health.STARTING and st.headline == "等待首次选币" and st.last_run_at is None

    with pytest.raises(RuntimeError):  # 数据源空：选币失败
        await scr.screen(now)
    st = log.read_status()["screener"]
    assert st.health is Health.DEGRADED and st.error and "沿用旧候选池" in st.headline
    assert st.today.errors == 1

    src.stats = [stat("OKUSDT")]
    await scr.screen(now)
    st = log.read_status()["screener"]
    assert st.health is Health.OK and st.error is None and st.symbol_count == 1
    assert st.last_run_at == now and st.next_run_at == now + timedelta(hours=1)
    assert "选出 1 个币" in st.headline and "下次" in st.headline


def test_screener_section_defaults_equal_the_old_hardcoded_values() -> None:
    from jiaoyi.slow.settings import SlowConfig

    cfg = SlowConfig().screener_config(
        pool_size=40, risk_max_spread_bps=5.0, round_trip_fee_bps=10.0
    )
    # [screener] 不写 = 默认值；唯一的区别是设置里默认每轮进总盘会的 BTC / ETH
    assert cfg == ScreenerConfig(always_candidates=frozenset({"BTC", "ETH"}))


def test_screener_section_validation_and_cautions() -> None:
    from pydantic import ValidationError

    from jiaoyi.slow.settings import ScreenerSection

    with pytest.raises(ValidationError, match="权重"):
        ScreenerSection(
            weight_volatility=0,
            weight_volume_surge=0,
            weight_funding=0,
            weight_oi=0,
            weight_liquidations=0,
        )
    with pytest.raises(ValidationError, match="下限"):
        ScreenerSection(min_range_24h_pct=10, max_range_24h_pct=5)
    assert ScreenerSection().cautions(5.0) == []
    notes = ScreenerSection(min_quote_volume_usdt=1e6, max_spread_bps=9).cautions(5.0)
    assert len(notes) == 2 and "风控员" in notes[1]


def test_range_filters_and_full_score_points(now: datetime) -> None:
    from jiaoyi.slow.screener import score

    src = FakeSource([stat("CALMUSDT", range_24h_pct=0.5), stat("WILDUSDT", range_24h_pct=60)])
    cfg = ScreenerConfig(min_range_24h_pct=1.5, max_range_24h_pct=30)
    import asyncio

    pool = asyncio.run(CodeSymbolScreener(src, cfg).screen(now))
    by = {e.symbol: e for e in pool.entries}
    assert by["CALMUSDT"].filters.failed_reasons == ["24 小时波动太小"]
    assert by["WILDUSDT"].filters.failed_reasons == ["24 小时波动过大"]
    # 满分点可配：funding 0.5% 封顶时 0.25% 只有一半分
    _, parts, _ = score(stat("XUSDT", **{}), ScreenerConfig(funding_full=0.005))
    assert parts["funding_extreme"] == 0.0


async def test_category_off_and_cap_precedence(now: datetime) -> None:
    from jiaoyi.slow.screener import ScreenerConfig

    src = FakeSource(
        [stat("AAAUSDT", range_24h_pct=9), stat("BBBUSDT", range_24h_pct=8),
         stat("TSLAUSDT", range_24h_pct=7), stat("PINUSDT", range_24h_pct=0.1)]
    )  # fmt: skip
    cfg = ScreenerConfig(
        category_enabled={Category.US_TECH: False}, category_caps={Category.ALT: 1}
    )
    pool = await CodeSymbolScreener(src, cfg).screen(now)
    assert {e.symbol for e in pool.in_pool} == {"AAAUSDT"}  # toml：美股关、山寨只要 1 个
    tsla = next(e for e in pool.entries if e.symbol == "TSLAUSDT")
    assert "这一类已关闭" in tsla.reasons
    # 选币页优先于 toml：重新打开美股、山寨放宽到 3；固定的币在关闭的类别里仍进池
    ov = UserOverrides(
        category_enabled={Category.US_TECH: True},
        category_caps={Category.ALT: 3},
    )
    pool = await CodeSymbolScreener(src, cfg, ov).screen(now)
    assert {e.symbol for e in pool.in_pool} == {"AAAUSDT", "BBBUSDT", "PINUSDT", "TSLAUSDT"}
    off = UserOverrides(category_enabled={Category.ALT: False}, pinned=frozenset({"PINUSDT"}))
    pool = await CodeSymbolScreener(src, ScreenerConfig(), off).screen(now)
    assert {e.symbol for e in pool.in_pool} == {"TSLAUSDT", "PINUSDT"}


async def test_tiers_and_us_events(now: datetime) -> None:
    import httpx

    from jiaoyi.slow.screener import ScreenerConfig
    from jiaoyi.slow.us_market import EarningsCalendar

    open_now = datetime(2026, 10, 23, 14, 0, tzinfo=now.tzinfo)  # 周五 10:00 美东：开盘中

    def handler(req: httpx.Request) -> httpx.Response:
        rows = [{"symbol": "TSLA", "time": "time-after-hours"}]
        return httpx.Response(200, json={"data": {"rows": rows if "10-23" in str(req.url) else []}})

    cal = EarningsCalendar(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    src = FakeSource(
        [stat("AAAUSDT", range_24h_pct=9), stat("BBBUSDT", range_24h_pct=8),
         stat("CCCUSDT", range_24h_pct=7), stat("DDDUSDT", range_24h_pct=6),
         stat("PINUSDT", range_24h_pct=0.1), stat("TSLAUSDT", range_24h_pct=1.0)]
    )  # fmt: skip
    cfg = ScreenerConfig(tier_head=2, tier_mid=2)
    ov = UserOverrides(pinned=frozenset({"PINUSDT"}))
    scr = CodeSymbolScreener(src, cfg, ov, earnings=cal)
    pool = await scr.screen(open_now)
    tiers = {e.symbol: e.tier for e in pool.in_pool}
    # 固定的币算头部并占名额：头部 = PIN + 分最高的 1 个，接着 2 个中间，其余尾部
    assert tiers["PINUSDT"] == "head" and sum(t == "head" for t in tiers.values()) == 2
    assert sum(t == "mid" for t in tiers.values()) == 2 and "tail" in tiers.values()
    tsla = next(e for e in pool.entries if e.symbol == "TSLAUSDT")
    assert "美股开盘中" in tsla.reasons and "财报：10-23 盘后（今天）" in tsla.reasons
    assert tsla.score_breakdown["us_open"] == cfg.us_open_bonus
    assert scr.us_tickers() == {"TSLA"}
    # 休市时不加分；关掉分档 / 美股开关各自生效
    closed = datetime(2026, 10, 24, 14, 0, tzinfo=now.tzinfo)
    pool = await CodeSymbolScreener(src, ScreenerConfig(tiers=False, us_events=False)).screen(
        closed
    )
    assert all(e.tier is None for e in pool.entries)
    assert "us_open" not in next(e for e in pool.entries if e.symbol == "TSLAUSDT").score_breakdown


async def test_tradfi_perps_stay_out_until_allowed(now: datetime) -> None:
    from jiaoyi.slow.screener import ScreenerConfig

    src = FakeSource([stat("AAAUSDT"), stat("INTCUSDT", kind="tradifi_perpetual equity")])
    pool = await CodeSymbolScreener(src, ScreenerConfig()).screen(now)
    assert {e.symbol for e in pool.in_pool} == {"AAAUSDT"}
    intc = next(e for e in pool.entries if e.symbol == "INTCUSDT")
    assert (
        intc.category is Category.US_TECH and "传统资产永续暂未开放" in intc.filters.failed_reasons
    )
    pool = await CodeSymbolScreener(src, ScreenerConfig(allow_tradfi=True)).screen(now)
    assert {e.symbol for e in pool.in_pool} == {"AAAUSDT", "INTCUSDT"}


async def test_allowed_tradfi_still_rejects_non_us_types(now: datetime) -> None:
    from jiaoyi.slow.screener import ScreenerConfig

    kinds = {
        "INTCUSDT": "tradifi_perpetual equity",
        "WTIUSDT": "tradifi_perpetual commodity",
        "0700USDT": "tradifi_perpetual hk_equity",
        "SAMSUSDT": "tradifi_perpetual kr_equity",
        "EURUSDT": "tradifi_perpetual fx",
        "PREUSDT": "tradifi_perpetual premarket",
        "MYSTUSDT": "tradifi_perpetual",
    }
    src = FakeSource([stat(sym, kind=k) for sym, k in kinds.items()])
    pool = await CodeSymbolScreener(src, ScreenerConfig(allow_tradfi=True)).screen(now)
    assert {e.symbol for e in pool.in_pool} == {"INTCUSDT", "WTIUSDT"}
    hk = next(e for e in pool.entries if e.symbol == "0700USDT")
    assert any(
        "暂不支持的传统资产类型" in r and "hk_equity" in r for r in hk.filters.failed_reasons
    )


class FakeVenue:
    def __init__(self, gone: dict[str, str] | None) -> None:
        self.gone = gone

    def unavailable(self, symbols: object) -> dict[str, str] | None:
        return (
            None if self.gone is None else {s: v for s, v in self.gone.items() if s in set(symbols)}
        )  # type: ignore[call-overload]


async def test_untradeable_venue_symbols_are_excluded_and_reported(
    now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    src = FakeSource(
        [stat("AAAUSDT"), stat("PUMPUSDT"), stat("THINUSDT", quote_volume_24h_usdt=1e6)]
    )
    venue = FakeVenue({"PUMPUSDT": "SETTLING", "THINUSDT": "SETTLING"})
    scr = CodeSymbolScreener(src, ScreenerConfig(), UserOverrides(pinned=frozenset({"PUMPUSDT"})))
    scr.set_venue(venue, "Demo")
    with caplog.at_level("INFO"):
        pool = await scr.screen(now)
    assert [e.symbol for e in pool.in_pool] == ["AAAUSDT"]
    pump = next(e for e in pool.entries if e.symbol == "PUMPUSDT")
    assert pump.filters.failed_reasons == ["Demo不可交易（SETTLING）"]
    assert pool.venue_excluded == {"PUMPUSDT": "SETTLING"}  # 本来就过不了门槛的不报
    assert "Demo 不可交易，已剔除：PUMPUSDT(SETTLING)" in caplog.text


async def test_untradeable_exclusion_off_switch_and_stale_list(now: datetime) -> None:
    src = FakeSource([stat("AAAUSDT"), stat("PUMPUSDT")])
    off = CodeSymbolScreener(src, ScreenerConfig(exclude_untradeable=False))
    off.set_venue(FakeVenue({"PUMPUSDT": "SETTLING"}))
    pool = await off.screen(now)
    assert {e.symbol for e in pool.in_pool} == {"AAAUSDT", "PUMPUSDT"}
    stale = CodeSymbolScreener(src, ScreenerConfig())
    stale.set_venue(FakeVenue(None))
    pool = await stale.screen(now)
    assert {e.symbol for e in pool.in_pool} == {"AAAUSDT", "PUMPUSDT"}
    assert pool.venue_excluded == {}


def _venue_lots(now: datetime, tradable: list[str]) -> tuple[FakeExchange, ExchangeInfoLotRules]:
    ex = FakeExchange()
    ex.filters = {sym: LotFilter(0.001, 0.001, 5.0) for sym in tradable}
    return ex, ExchangeInfoLotRules(ex, max_age_s=3600, clock=lambda: now)


async def test_screen_drops_symbol_without_venue_lot_rules_and_fills_its_slot(
    now: datetime,
) -> None:
    src = FakeSource([stat("PUMPUSDT", range_24h_pct=9), stat("AAAUSDT"), stat("BBBUSDT")])
    _, lots = _venue_lots(now, ["AAAUSDT", "BBBUSDT"])
    assert await lots.refresh()
    scr = CodeSymbolScreener(src, ScreenerConfig(pool_size=2))
    scr.set_venue(lots, "Demo")
    pool = await scr.screen(now)
    assert sorted(e.symbol for e in pool.in_pool) == ["AAAUSDT", "BBBUSDT"]
    pump = next(e for e in pool.entries if e.symbol == "PUMPUSDT")
    assert pump.filters.failed_reasons == ["Demo不可交易（无下单精度）"]
    assert pool.venue_excluded == {"PUMPUSDT": "无下单精度"}


async def test_screen_keeps_pool_and_warns_when_venue_lot_rules_unavailable(
    now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    src = FakeSource([stat("PUMPUSDT"), stat("AAAUSDT")])
    ex, lots = _venue_lots(now, ["AAAUSDT"])
    ex.filters_down = True
    assert not await lots.refresh()
    scr = CodeSymbolScreener(src, ScreenerConfig())
    scr.set_venue(lots, "Demo")
    with caplog.at_level("WARNING"):
        pool = await scr.screen(now)
    assert sorted(e.symbol for e in pool.in_pool) == ["AAAUSDT", "PUMPUSDT"]
    assert pool.venue_excluded == {}
    assert "Demo下单精度没拉到或已过期，这轮选币不按交易所可交易名单剔币" in caplog.text


async def test_screen_ignores_venue_list_missing_most_passing_symbols(
    now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    syms = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    src = FakeSource([stat(x) for x in [*syms, "PUMPUSDT"]])
    ex, partial = _venue_lots(now, ["AAAUSDT"])
    ex.untradeable_status = {"PUMPUSDT": "SETTLING"}  # type: ignore[attr-defined]
    assert await partial.refresh()
    scr = CodeSymbolScreener(src, ScreenerConfig())
    scr.set_venue(partial, "Demo")
    with caplog.at_level("WARNING"):
        pool = await scr.screen(now)
    assert sorted(e.symbol for e in pool.in_pool) == syms
    assert pool.venue_excluded == {"PUMPUSDT": "SETTLING"}
    assert "Demo下单精度里没有 3 / 5 个币，像是没拉全，这轮选币只按交易所状态剔币" in caplog.text
    _, most = _venue_lots(now, ["AAAUSDT", "BBBUSDT", "PUMPUSDT"])
    assert await most.refresh()
    scr.set_venue(most, "Demo")
    pool = await scr.screen(now)
    assert sorted(e.symbol for e in pool.in_pool) == ["AAAUSDT", "BBBUSDT", "PUMPUSDT"]
    assert pool.venue_excluded == {"CCCUSDT": "无下单精度", "DDDUSDT": "无下单精度"}


async def test_screen_still_excludes_at_exactly_half_without_lot_rules(now: datetime) -> None:
    syms = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    _, lots = _venue_lots(now, ["AAAUSDT", "BBBUSDT"])
    assert await lots.refresh()
    scr = CodeSymbolScreener(FakeSource([stat(x) for x in syms]), ScreenerConfig())
    scr.set_venue(lots, "Demo")
    pool = await scr.screen(now)
    assert sorted(e.symbol for e in pool.in_pool) == ["AAAUSDT", "BBBUSDT"]
    assert pool.venue_excluded == {"CCCUSDT": "无下单精度", "DDDUSDT": "无下单精度"}


async def test_screen_venue_ratio_counts_only_hard_filter_passing_symbols(now: datetime) -> None:
    thin = [stat(f"THIN{i}USDT", quote_volume_24h_usdt=1e6) for i in range(3)]
    src = FakeSource([stat("AAAUSDT"), stat("CCCUSDT"), stat("DDDUSDT"), *thin])
    _, lots = _venue_lots(now, ["AAAUSDT", *(s.symbol for s in thin)])
    assert await lots.refresh()
    scr = CodeSymbolScreener(src, ScreenerConfig())
    scr.set_venue(lots, "Demo")
    pool = await scr.screen(now)
    # 2 / 3 passing coins lack lot rules (2 / 6 if the thin ones counted): treated as partial
    assert sorted(e.symbol for e in pool.in_pool) == ["AAAUSDT", "CCCUSDT", "DDDUSDT"]
    assert pool.venue_excluded == {}
