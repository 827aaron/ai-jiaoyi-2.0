from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.app import App, read_blocked
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.loop import Runner
from tests import fastloop_helpers as h
from tests.conftest import ROOT, load_example

RISK = ROOT / "config" / "risk.example.toml"
APP = ROOT / "config" / "app.example.toml"


def _app(tmp_path: Path, now: datetime) -> App:
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path)})
    return App(load_risk_config(RISK), cfg, clock=lambda: now, offline=True)


def _position(**over: Any) -> c.PositionRecord:
    return c.PositionRecord.model_validate({**load_example("position_record"), **over})


class FakeExecutor:
    def __init__(self, fail_open: bool = False) -> None:
        self.opened: list[str] = []
        self.closed: list[tuple[str, CloseReason]] = []
        self.fail_open = fail_open
        self.held: list[c.PositionRecord] = []

    async def open(self, intent: c.OrderIntent) -> c.PositionRecord:
        if self.fail_open:
            raise RuntimeError("hummingbot-api 503")
        self.opened.append(intent.intent_id)
        return _position()

    async def close(self, position_id: str, reason: CloseReason) -> None:
        self.closed.append((position_id, reason))

    async def positions(self) -> list[c.PositionRecord]:
        return self.held

    async def account(self) -> c.AccountState:
        return c.AccountState.model_validate(load_example("account_state"))


class FakeRisk:
    def __init__(self, approve: bool) -> None:
        self.approve = approve

    def review(self, *args: object, **kw: object) -> c.RiskVerdict:
        name = "risk_verdict_approved" if self.approve else "risk_verdict_rejected"
        return c.RiskVerdict.model_validate(load_example(name))


class FakeGate:
    def __init__(self) -> None:
        self.asked = 0

    async def evaluate(
        self, signal: c.CandidateSignal, card: c.BattleCard, now: datetime
    ) -> c.JevResponse:
        self.asked += 1
        return c.JevResponse.model_validate(load_example("jev_entry_response"))


class FakeCards:
    def __init__(self, card: c.BattleCard | None, reason: str | None = None) -> None:
        self.card = card
        self.reason = reason
        self.refreshed: list[str] = []

    def current(self, now: datetime) -> c.BattleCard | None:
        return self.card

    def refresh_reason(
        self, now: datetime, pool: c.SymbolPool, prices: Mapping[str, float]
    ) -> str | None:
        return self.reason

    async def refresh(self, pool: c.SymbolPool, reason: str) -> c.BattleCard:
        self.refreshed.append(reason)
        assert self.card is not None
        return self.card


class FakeKeeper:
    def __init__(self, action: str = "hold") -> None:
        self.action = action
        self.reviewed: list[str] = []
        self.forgotten: list[str] = []

    async def review(self, position: c.PositionRecord, now: datetime) -> c.KeeperVerdict:
        self.reviewed.append(position.position_id)
        return c.KeeperVerdict(
            position_id=position.position_id,
            ts=now,
            action="close" if self.action == "close" else "hold",
            p_close=0.9 if self.action == "close" else 0.1,
            consecutive=2,
        )

    def forget(self, position_id: str) -> None:
        self.forgotten.append(position_id)


class FakePatrol:
    def __init__(self, action: c.PatrolAction | None = None) -> None:
        self.action = action
        self.urgent: set[str] = set()

    def check(
        self, event: c.MarketEvent, positions: Sequence[c.PositionRecord]
    ) -> c.PatrolAction | None:
        return self.action


async def test_approved_signal_opens(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, gate = FakeExecutor(), FakeGate()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=gate, risk=FakeRisk(True), cards=FakeCards(h.card(now))
    )
    r = Runner(app)
    await r.handle_signal(h.signal(now))
    assert gate.asked == 1 and ex.opened == ["int-0001"]
    assert [p.position_id for p in r.positions] == ["pos-0001"]


async def test_rejected_or_no_card_never_opens(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, gate = FakeExecutor(), FakeGate()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=gate, risk=FakeRisk(False), cards=FakeCards(h.card(now))
    )
    await Runner(app).handle_signal(h.signal(now))
    assert ex.opened == []
    app.m = dataclasses.replace(app.m, risk=FakeRisk(True), cards=FakeCards(None))
    await Runner(app).handle_signal(h.signal(now))
    assert ex.opened == [] and gate.asked == 1  # 没卡连 Jev 都不问


async def test_open_failure_alerts(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    sent: list[c.Alert] = []

    class Alerter:
        async def send(self, alert: c.Alert) -> None:
            sent.append(alert)

    app.m = dataclasses.replace(
        app.m,
        executor=FakeExecutor(fail_open=True),
        gate=FakeGate(),
        risk=FakeRisk(True),
        cards=FakeCards(h.card(now)),
        alerter=Alerter(),
    )
    r = Runner(app)
    await r.handle_signal(h.signal(now))
    assert r.positions == [] and "开仓失败" in sent[0].title


async def test_patrol_action_closes_and_keeper_forgets(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, keeper = FakeExecutor(), FakeKeeper()
    patrol = FakePatrol(c.PatrolAction(ts=now, symbols=["BTCUSDT"], reason="1 分钟急跌"))
    patrol.urgent.add("pos-0001")
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper, patrol=patrol)
    r = Runner(app)
    r.positions = [_position(), _position(position_id="pos-0002", symbol="ETHUSDT")]
    await r.on_event(c.MarkPrice.model_validate(_mark(now)))
    await asyncio.gather(*r._tasks)
    assert ex.closed == [("pos-0001", CloseReason.PATROL)]
    assert keeper.forgotten == ["pos-0001"] and not patrol.urgent
    assert [p.position_id for p in r.positions] == ["pos-0002"]


def _mark(now: datetime) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "ts": now.isoformat(),
        "mark_price": 67000.0,
        "index_price": 67000.0,
        "funding_rate": 0.0001,
    }


async def test_urgent_review_closes_via_keeper(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, keeper, patrol = FakeExecutor(), FakeKeeper("close"), FakePatrol()
    patrol.urgent |= {"pos-0001", "pos-gone"}
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper, patrol=patrol)
    r = Runner(app)
    r.positions = [_position()]
    await r.urgent_tick()
    assert keeper.reviewed == ["pos-0001"]
    assert ex.closed == [("pos-0001", CloseReason.KEEPER)]
    assert not patrol.urgent  # 不在持仓里的也清掉


async def test_keeper_tick_reads_positions_and_holds(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, keeper = FakeExecutor(), FakeKeeper("hold")
    ex.held = [_position(), _position(position_id="pos-c", status="closed")]
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper)
    r = Runner(app)
    await r.keeper_tick()
    assert keeper.reviewed == ["pos-0001"] and ex.closed == []


async def test_slow_tick_schedules_meeting_only_with_real_cards(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    app.pool = c.SymbolPool.model_validate(load_example("symbol_pool"))
    cards = FakeCards(h.card(now), reason="card_expiring")
    app.m = dataclasses.replace(app.m, cards=cards)
    r = Runner(app)
    r._last_screen = now
    await r.slow_tick()
    await asyncio.sleep(0)
    assert cards.refreshed == ["card_expiring"]

    # 没理由、离上次开会也不够久 → 不开
    cards.reason = None
    await r.slow_tick()
    await asyncio.sleep(0)
    assert cards.refreshed == ["card_expiring"]

    # 到了 slow_loop_interval_min → 定时开会
    r._last_card_at = now - timedelta(minutes=app.app_cfg.slow_loop_interval_min)
    await r.slow_tick()
    await asyncio.sleep(0)
    assert cards.refreshed == ["card_expiring", "scheduled"]


async def test_backfill_skipped_for_stub_feed_and_runs_for_real(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    calls: list[str] = []

    async def fetch(sym: str) -> list[c.Kline]:
        calls.append(sym)
        if sym == "ETHUSDT":
            raise RuntimeError("超时")
        return []

    r = Runner(app, fetch_klines=fetch)
    await r.backfill(["BTCUSDT"])
    assert calls == []  # 行情是桩

    class Feed:
        async def subscribe(self, symbols: Sequence[str]) -> None: ...

        async def events(self) -> AsyncIterator[c.MarketEvent]:
            if False:
                yield  # pragma: no cover

        async def close(self) -> None: ...

    five: list[str] = []

    async def fetch5(sym: str) -> list[c.Kline]:
        five.append(sym)
        return []

    app.m = dataclasses.replace(app.m, feed=Feed())
    hist: list[str] = []

    async def fetch_hist(sym: str) -> list[c.Kline]:
        hist.append(sym)
        raise RuntimeError("超时")  # 7 天 ATR 历史拉不到：没有下限，不影响别的

    await Runner(
        app, fetch_klines=fetch, fetch_predictor_klines=fetch5, fetch_atr_history=fetch_hist
    ).backfill(["BTCUSDT", "ETHUSDT"])
    assert sorted(calls) == ["BTCUSDT", "ETHUSDT"]  # 一个失败不影响别的
    assert sorted(five) == ["BTCUSDT", "ETHUSDT"]  # 预测员的 5 分钟线也补
    assert sorted(hist) == ["BTCUSDT", "ETHUSDT"]  # 止损 ATR 下限的 7 天历史也补


class FakePredictor:
    enabled = True

    def __init__(self) -> None:
        self.wanted: set[str] = set()
        self.filled: dict[str, int] = {}
        self.klines = 0

    async def on_kline(self, kline: c.Kline) -> None:
        self.klines += 1

    def latest(self, symbol: str, now: datetime) -> c.Forecast | None:
        return None

    def set_wanted(self, symbols: set[str] | frozenset[str]) -> None:
        self.wanted = set(symbols)

    def request(self, symbol: str) -> None: ...

    def set_priority(self, symbols: set[str] | frozenset[str]) -> None:
        self.priority = set(symbols)

    async def backfill(self, symbol: str, bars: list[c.Kline]) -> None:
        self.filled[symbol] = len(bars)


async def test_predictor_wants_card_and_held_symbols(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    pred = FakePredictor()
    card = h.card(now)
    app.m = dataclasses.replace(app.m, predictor=pred, cards=FakeCards(card))
    r = Runner(app)
    r.positions = [_position(symbol="XRPUSDT")]
    k = c.Kline(
        symbol="BTCUSDT", interval=c.Interval.M1, open_time=now - timedelta(minutes=1),
        close_time=now, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
    )  # fmt: skip
    await r.on_event(k)
    assert pred.klines == 1
    assert pred.wanted == {s.symbol for s in card.symbols} | {"XRPUSDT"}


class WatchFeed:
    def __init__(self) -> None:
        self.subs: list[list[str]] = []
        self.watch_note = ""

    async def subscribe(self, symbols: Sequence[str]) -> None:
        self.subs.append(list(symbols))

    async def events(self) -> AsyncIterator[c.MarketEvent]:
        if False:
            yield  # pragma: no cover

    async def close(self) -> None: ...


async def test_watch_set_is_meeting_card_and_held_not_pool(tmp_path: Path, now: datetime) -> None:
    """盘口员只订总盘会挑中 + 作战卡 + 持仓的币；新币回填，卡过期 / 平仓后退订。"""
    app = _app(tmp_path, now)
    pred, feed, cards = FakePredictor(), WatchFeed(), FakeCards(None)
    filled: list[str] = []

    async def fetch(sym: str) -> list[c.Kline]:
        filled.append(sym)
        return []

    app.m = dataclasses.replace(app.m, predictor=pred, feed=feed, cards=cards)
    r = Runner(app, fetch_klines=fetch, fetch_predictor_klines=fetch, fetch_atr_history=fetch)
    await r.sync_watch()
    assert feed.subs == [[]]  # 没有要盯的也订一次（全市场强平连接）
    await r.sync_watch()
    assert len(feed.subs) == 1  # 没变不重订

    r.set_meeting_picks(["SOLUSDT", "ETHUSDT"])  # 总盘会刚挑完，卡还没出
    await asyncio.sleep(0)
    await r.sync_watch()
    assert feed.subs[-1] == ["ETHUSDT", "SOLUSDT"]
    assert pred.priority == {"SOLUSDT", "ETHUSDT"}
    assert sorted(filled) == ["ETHUSDT"] * 3 + ["SOLUSDT"] * 3  # 1m + 5m + 7 天 ATR 各回填一次

    card = h.card(now)
    on_card = {s.symbol for s in card.symbols}
    cards.card = card
    r.positions = [_position(symbol="XRPUSDT")]
    await r._refresh(c.SymbolPool.model_validate(load_example("symbol_pool")), "scheduled")
    assert pred.priority == set()  # 挑中的币已上卡
    assert set(feed.subs[-1]) == on_card | {"XRPUSDT"}
    n = len(on_card - {"XRPUSDT"})
    assert feed.watch_note == f"盯着 {len(on_card | {'XRPUSDT'})} 个币（总盘会 {n} 持仓 1）"

    cards.card, r.positions = None, []  # 卡过期、平仓
    await r.sync_watch()
    assert feed.subs[-1] == []


async def test_backfill_feeds_recent_minutes_to_predictor(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    pred = FakePredictor()

    class Feed:
        async def subscribe(self, symbols: Sequence[str]) -> None: ...

        async def events(self) -> AsyncIterator[c.MarketEvent]:
            if False:
                yield  # pragma: no cover

        async def close(self) -> None: ...

    app.m = dataclasses.replace(app.m, predictor=pred, feed=Feed())
    k = c.Kline(
        symbol="BTCUSDT", interval=c.Interval.M1, open_time=now - timedelta(minutes=1),
        close_time=now, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
    )  # fmt: skip

    async def fetch1(sym: str) -> list[c.Kline]:
        return [k, k]

    async def fetch5(sym: str) -> list[c.Kline]:
        return [k]

    await Runner(
        app, fetch_klines=fetch1, fetch_predictor_klines=fetch5, fetch_atr_history=fetch5
    ).backfill(["BTCUSDT"])
    assert pred.filled == {"BTCUSDT": 1}
    assert pred.klines == 2  # 1 分钟线也交给预测员接上当前这根 5 分钟线


async def test_run_until_cancelled_then_stops(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    r = Runner(app)
    task = asyncio.create_task(r.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if app.started:
            break
    assert app.started
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not app.started


async def test_concurrent_signals_respect_max_open_positions(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)

    class SlowExecutor(FakeExecutor):
        async def open(self, intent: c.OrderIntent) -> c.PositionRecord:
            await asyncio.sleep(0.01)  # 开仓要等成交，期间另一个信号到达
            return await super().open(intent)

        async def account(self) -> c.AccountState:
            acc = await super().account()
            return acc.model_copy(update={"open_positions": len(self.opened)})

    class OneSlot:
        """max_open_positions=1 / 敞口只够一笔：已有持仓就拒。"""

        def review(self, *args: object, **kw: object) -> c.RiskVerdict:
            account = args[3]
            assert isinstance(account, c.AccountState)
            ok = account.open_positions == 0
            name = "risk_verdict_approved" if ok else "risk_verdict_rejected"
            return c.RiskVerdict.model_validate(load_example(name))

    ex = SlowExecutor()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=FakeGate(), risk=OneSlot(), cards=FakeCards(h.card(now))
    )
    r = Runner(app)
    await asyncio.gather(r.handle_signal(h.signal(now)), r.handle_signal(h.signal(now)))
    assert len(ex.opened) == 1


async def test_stuck_close_does_not_block_market_events(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    release = asyncio.Event()

    class StuckExecutor(FakeExecutor):
        async def close(self, position_id: str, reason: CloseReason) -> None:
            self.closed.append((position_id, reason))
            await release.wait()

    ex = StuckExecutor()
    patrol = FakePatrol(c.PatrolAction(ts=now, symbols=["BTCUSDT"], reason="1 分钟急跌"))
    app.m = dataclasses.replace(app.m, executor=ex, keeper=FakeKeeper(), patrol=patrol)
    r = Runner(app)
    r.positions = [_position(), _position(position_id="pos-0002")]
    ev = c.MarkPrice.model_validate(_mark(now))
    await asyncio.wait_for(r.on_event(ev), 0.5)  # 平仓卡住，行情照常处理
    await asyncio.sleep(0)
    await asyncio.wait_for(r.on_event(ev), 0.5)  # 同一笔不重复提交
    await asyncio.sleep(0)
    assert sorted(pid for pid, _ in ex.closed) == ["pos-0001", "pos-0002"]  # 两笔并行
    release.set()
    await asyncio.gather(*r._tasks)
    assert r.positions == []


class FakeHub:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def poll_and_notify(
        self,
        symbols: Sequence[str],
        notify: Callable[[str], None],
        block: Callable[[str, str], None] | None = None,
    ) -> list[object]:
        self.calls.append(list(symbols))
        notify("BTCUSDT")
        assert block is not None
        block("ETHUSDT", "币安公告：下架 ETHUSDT")
        return []


class NewsCards(FakeCards):
    def __init__(self) -> None:
        super().__init__(None)
        self.news: list[str] = []
        self.blocked: dict[str, str] = {}

    def notify_news(self, symbol: str) -> None:
        self.news.append(symbol)

    def block_symbol(self, symbol: str, reason: str) -> None:
        self.blocked[symbol] = reason

    @property
    def blocked_symbols(self) -> dict[str, str]:
        return dict(self.blocked)

    def unblock_symbol(self, symbol: str) -> None:
        self.blocked.pop(symbol, None)


async def test_news_tick_notifies_and_blocks(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    app.pool = c.SymbolPool.model_validate(load_example("symbol_pool"))
    hub, cards = FakeHub(), NewsCards()
    sent: list[c.Alert] = []

    class Alerter:
        async def send(self, alert: c.Alert) -> None:
            sent.append(alert)

    app.news = hub  # type: ignore[assignment]
    app.m = dataclasses.replace(app.m, cards=cards, alerter=Alerter())
    r = Runner(app)
    r.positions = [_position(symbol="DOGEUSDT")]
    await r.news_tick()
    await r.news_tick()  # 已封的不重复推
    assert [a.title for a in sent] == ["ETHUSDT 禁止新开仓"]
    pool_syms = {e.symbol for e in app.pool.in_pool}
    assert set(hub.calls[0]) == pool_syms | {"DOGEUSDT"}  # 候选池 + 持仓
    assert cards.news == ["BTCUSDT", "BTCUSDT"] and "ETHUSDT" in cards.blocked
    # 封禁名单落盘给 jiaoyi status / 面板读；人工解封后文件同步
    assert set(read_blocked(tmp_path)) == {"ETHUSDT"}
    assert app.unblock_symbol("ETHUSDT") and not app.unblock_symbol("ETHUSDT")
    assert read_blocked(tmp_path) == {}


async def test_news_tick_skipped_without_hub_or_real_cards(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    app.pool = c.SymbolPool.model_validate(load_example("symbol_pool"))
    hub = FakeHub()
    app.news = hub  # type: ignore[assignment]
    await Runner(app).news_tick()  # 军师团是桩：不拉
    assert hub.calls == []
    app.news = None
    app.m = dataclasses.replace(app.m, cards=NewsCards())
    await Runner(app).news_tick()  # 没有新闻源：什么都不做


async def test_rescreen_writes_pool_file_for_panel(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.app import POOL_FILE

    app = _app(tmp_path, now)
    pool = c.SymbolPool.model_validate(load_example("symbol_pool"))

    class Screener:
        async def screen(self, now: datetime) -> c.SymbolPool:
            return pool

    app.m = dataclasses.replace(app.m, screener=Screener())
    await app.start()  # 启动选币就写一次
    saved = c.SymbolPool.model_validate_json((tmp_path / POOL_FILE).read_text(encoding="utf-8"))
    assert saved == pool
    (tmp_path / POOL_FILE).unlink()
    await Runner(app)._rescreen(now)  # 每小时重选也写
    assert (tmp_path / POOL_FILE).is_file()
    await app.stop()


async def test_app_exposes_predictor_against_card_watch(tmp_path: Path, now: datetime) -> None:
    """慢环通过 App.against 订阅「预测员连续看反作战卡」。"""
    app = _app(tmp_path, now)
    assert app.against is not None and app.against.current(now) == {}


def test_every_atr_period_has_a_history_interval() -> None:
    """配置允许的每个 ATR 周期都要有拉 7 天历史用的 K 线周期，漏了止损下限会静默失效。"""
    from typing import get_args

    from jiaoyi.loop import ATR_INTERVAL

    periods = get_args(c.FastLoopConfig.model_fields["atr_period_min"].annotation)
    assert set(periods) == set(ATR_INTERVAL)
    for p, iv in ATR_INTERVAL.items():
        assert iv.value == (f"{p}m" if p < 60 else f"{p // 60}h")


async def test_limit_take_profit_falls_back_to_market(tmp_path: Path, now: datetime) -> None:
    t = [now]
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path)})
    app = App(load_risk_config(RISK), cfg, clock=lambda: t[0], offline=True)
    assert app.app_cfg.limit_take_profit_on  # paper 默认开
    ex = FakeExecutor()
    app.m = dataclasses.replace(app.m, executor=ex, patrol=FakePatrol())
    r = Runner(app)
    pos = _position()  # 做多，止盈 68384.2
    r.positions = [pos]

    def trade(px: float) -> c.Trade:
        return c.Trade(symbol="BTCUSDT", ts=t[0], price=px, qty=0.01, taker_side=c.Side.LONG)

    await r.on_event(trade(68390.0))  # 越过止盈价，开始计时
    t[0] = now + timedelta(seconds=5)
    await r.on_event(trade(68380.0))  # 回到止盈价里面：重新计时
    t[0] = now + timedelta(seconds=12)
    await r.on_event(trade(68390.0))
    await asyncio.gather(*r._tasks)
    assert ex.closed == []
    t[0] = now + timedelta(seconds=23)
    await r.on_event(trade(68395.0))  # 越过满 10 秒还在：改市价平
    await asyncio.gather(*r._tasks)
    assert ex.closed == [("pos-0001", CloseReason.TAKE_PROFIT)]


async def test_keeper_tick_reviews_positions_concurrently(tmp_path: Path, now: datetime) -> None:
    from jiaoyi import loop

    app = _app(tmp_path, now)
    ex = FakeExecutor()
    ex.held = [_position(position_id=f"pos-{i}", symbol=f"S{i}USDT") for i in range(7)]
    running = peak = 0

    class SlowKeeper(FakeKeeper):
        async def review(self, position: c.PositionRecord, at: datetime) -> c.KeeperVerdict:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return await super().review(position, at)

    keeper = SlowKeeper("hold")
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper)
    await Runner(app).keeper_tick()
    assert sorted(keeper.reviewed) == sorted(p.position_id for p in ex.held)
    assert peak == loop.KEEPER_CONCURRENCY  # 并发问，但不超过上限
