from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiaoyi.contracts import FastLoopConfig, Kline, Role
from jiaoyi.core.hotconfig import HOT_FAST, patch_inplace
from jiaoyi.loop import Runner
from jiaoyi.market.rest import RateLimited
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.pages import build_team
from jiaoyi.sentinel.regime_flip import Direction
from jiaoyi.slow.commander import BattleCardCommander, EventRules, Pick, PickResult
from jiaoyi.slow.market_log import MarketLog
from tests.fastloop_helpers import features
from tests.test_hot_config import _edit, _running
from tests.test_loop import _app
from tests.test_regime_flip import bar, records
from tests.test_slow_commander import RISK, FakeEngine, entry, pool


class Engine(FakeEngine):
    direction: Direction = "看跌"

    def __init__(self) -> None:
        super().__init__(["AAUSDT", "ABUSDT"])
        self.pick_calls = 0

    async def pick(self, *args: object, **kwargs: object) -> PickResult:
        self.pick_calls += 1
        return PickResult(
            "文字不参与规则",
            [Pick(s, "测试") for s in self.picks],
            0.01,
            regime_direction=self.direction,
        )


async def setup(tmp: Path, now: datetime):
    app = _app(tmp, now)
    clock, px = [now], [100.0]
    app.clock = lambda: clock[0]
    eng = Engine()
    cards = BattleCardCommander(
        eng,
        RISK,
        app.log,
        clock=app.clock,
        card_path=tmp / "battle_card.json",
        market_log=MarketLog(tmp / "market_meeting"),
        events=EventRules(
            enabled=True, triggers=frozenset({"signal_against_card"}), symbol_gap=timedelta(0)
        ),
    )

    class Features:
        def snapshot(self, symbol: str):
            return features(clock[0], price=px[0]) if symbol == "BTCUSDT" else None

    app.m = dataclasses.replace(app.m, cards=cards, features=Features())
    p = pool(now, *(entry(f"A{chr(65 + i)}USDT") for i in range(10)))
    app.pool = p

    async def fetch(sym: str, start: datetime, end: datetime) -> list[Kline]:
        return [bar(sym, end - timedelta(hours=1), 100), bar(sym, end, px[0])]

    r = Runner(app, fetch_regime_klines=fetch)
    r._screen_now = AsyncMock()
    r.sync_watch = AsyncMock()
    await cards.refresh(p, "scheduled")
    r._last_screen = r._last_card_at = now
    return r, cards, eng, clock, px


async def finish(r: Runner) -> None:
    if r._tasks:
        await asyncio.gather(*tuple(r._tasks))


async def test_round_reference_flip_and_unchanged_scheduled_deadline(
    tmp_path: Path, now: datetime
) -> None:
    r, c, eng, clock, px = await setup(tmp_path, now)
    assert c.regime_reference is not None and c.regime_reference.btc_px == 100
    assert c.regime_reference.direction == "看跌"
    deadline = c._scheduled_due
    eng.direction = "看涨"
    px[0] = 102
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()
    assert eng.pick_calls == 1
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()
    assert r._refreshing
    await finish(r)
    assert eng.pick_calls == 2
    assert eng.analyzed == ["AAUSDT", "ABUSDT", "AAUSDT", "ABUSDT"]  # 不沿用旧单币会
    assert c.current(clock[0]).refresh_reason == "regime_flip"
    assert c.regime_reference.direction == "看涨" and c.regime_reference.btc_px == 102
    assert c._scheduled_due == deadline and r._last_card_at == now
    events = records(tmp_path / "regime_flip")
    assert [e["kind"] for e in events] == ["blocked", "confirmed", "rerun_result"]
    assert events[-1]["result"] == "flip_changed"
    assert events[-1]["cost_usd"] == pytest.approx(0.05)
    assert events[-1]["event_id"] == events[-2]["event_id"]
    market = records(tmp_path / "market_meeting")[-1]
    assert market["regime_direction"] == "看涨" and market["refresh_reason"] == "regime_flip"

    # 模拟重启：参照、翻转间隔和原有整轮到期点都恢复。
    restored = BattleCardCommander(
        eng, RISK, r.app.log, clock=r.app.clock, card_path=tmp_path / "battle_card.json"
    )
    assert restored.regime_reference == c.regime_reference
    assert restored.last_regime_flip_at == clock[0]
    assert restored._scheduled_due == deadline and restored._last_refresh == now


@pytest.mark.parametrize("block", ["busy", "budget", "backoff", "rate", "missing", "neutral"])
async def test_gates_block_without_duplicate_round(
    tmp_path: Path, now: datetime, block: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    r, c, eng, clock, px = await setup(tmp_path, now)
    px[0] = 102
    patch_inplace(
        r.app.app_cfg.fast_loop, FastLoopConfig(regime_flip_confirm=1), {"regime_flip_confirm"}
    )
    if block == "busy":
        r._refreshing = True
    elif block == "budget":
        c._cost_limit = 0.001
    elif block == "backoff":
        c._retry_at = now + timedelta(hours=1)
    elif block == "rate":
        monkeypatch.setattr("jiaoyi.loop.binance_gate.banned_until", lambda: now.timestamp() + 3600)
    elif block == "missing":
        c.regime_reference = None
    else:
        c.regime_reference = c.regime_reference.model_copy(update={"direction": "中性"})
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()
    await finish(r)
    assert eng.pick_calls == 1
    latest = records(tmp_path / "regime_flip")[-1]
    assert latest["kind"] == "blocked"
    expected = {
        "busy": "正在开会",
        "budget": "日预算",
        "backoff": "失败退避",
        "rate": "限流",
        "missing": "没有可恢复的总盘会参照",
        "neutral": "中性判断",
    }
    assert latest["blocked_reason"] == expected[block]
    assert c.last_regime_flip_at is None


async def test_rate_limit_during_fetch_is_logged_not_raised(tmp_path: Path, now: datetime) -> None:
    r, _, eng, clock, _ = await setup(tmp_path, now)
    r._fetch_regime = AsyncMock(side_effect=RateLimited(429, now.timestamp() + 600))
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()
    assert r._regime.count == 0 and eng.pick_calls == 1
    assert records(tmp_path / "regime_flip")[-1]["blocked_reason"] == "限流"


async def test_busy_single_coin_event_and_flip_do_not_overlap(
    tmp_path: Path, now: datetime
) -> None:
    r, c, eng, clock, px = await setup(tmp_path, now)
    px[0] = 102
    cfg = r.app.app_cfg.fast_loop
    patch_inplace(cfg, cfg.model_copy(update={"regime_flip_confirm": 1}), {"regime_flip_confirm"})
    c.request_event("AAUSDT", "signal_against_card")
    clock[0] += timedelta(minutes=5)
    await asyncio.gather(r.regime_flip_tick(), r.regime_flip_tick())
    assert r._refreshing
    await r.slow_tick()  # 单币事件仍在排队，不能和整轮并发
    assert "AAUSDT" in c._pending
    await finish(r)
    assert eng.pick_calls == 2
    await r.slow_tick()
    await finish(r)
    assert eng.pick_calls == 2 and "AAUSDT" not in c._pending


@pytest.mark.parametrize("failed", [False, True])
async def test_no_change_or_failure_is_logged_and_does_not_refund_cooldown(
    tmp_path: Path, now: datetime, failed: bool
) -> None:
    r, c, eng, clock, px = await setup(tmp_path, now)
    old = c.regime_reference
    if failed:
        eng.fail = set(eng.picks)
    px[0] = 102
    cfg = r.app.app_cfg.fast_loop
    patch_inplace(cfg, cfg.model_copy(update={"regime_flip_confirm": 1}), {"regime_flip_confirm"})
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()
    await finish(r)
    result = records(tmp_path / "regime_flip")[-1]
    assert result["result"] == ("flip_failed" if failed else "flip_no_change")
    assert c.last_regime_flip_at == clock[0]
    assert result["cost_usd"] > 0
    if failed:
        assert c.regime_reference == old


@pytest.mark.parametrize(
    "mode,on,live,period,enabled",
    [
        ("paper", False, True, 5, False),
        ("testnet", True, False, 5, False),
        ("live", True, False, 5, False),
        ("live", True, True, 5, True),
        ("paper", True, False, 0, False),
    ],
)
async def test_switches_have_no_network_when_off(
    tmp_path: Path, now: datetime, mode: str, on: bool, live: bool, period: float, enabled: bool
) -> None:
    r, _, _, _, _ = await setup(tmp_path, now)
    cfg = r.app.app_cfg
    object.__setattr__(cfg, "mode", mode)
    fl = cfg.fast_loop
    patch_inplace(
        fl,
        fl.model_copy(
            update={
                "regime_flip_on": on,
                "regime_flip_live": live,
                "regime_flip_check_min": period,
            }
        ),
        HOT_FAST,
    )
    r._fetch_regime = AsyncMock(return_value=[])
    await r.regime_flip_tick()
    assert bool(r._fetch_regime.await_count) is enabled


def test_all_new_settings_hot_update_in_running_app(tmp_path: Path) -> None:
    app, paths, data = _running(tmp_path)
    keys = {k for k in FastLoopConfig.model_fields if k.startswith("regime_flip_")}
    assert keys <= HOT_FAST
    changes = {k: False if k in ("regime_flip_on", "regime_flip_live") else 0 for k in keys}
    changes["regime_flip_min_gap_min"] = 100000.125
    _edit(paths, data, "fast_loop", changes)
    app.reload_config()
    for key, value in changes.items():
        assert getattr(app.app_cfg.fast_loop, key) == value


async def test_check_period_is_hot_and_zero_stops_it(tmp_path: Path, now: datetime) -> None:
    r, _, _, clock, _ = await setup(tmp_path, now)
    r.regime_flip_tick = AsyncMock()
    await r._regime_periodic_tick()
    clock[0] += timedelta(minutes=5)
    await r._regime_periodic_tick()
    assert r.regime_flip_tick.await_count == 1
    cfg = r.app.app_cfg.fast_loop
    patch_inplace(cfg, cfg.model_copy(update={"regime_flip_check_min": 0.25}), HOT_FAST)
    await r._regime_periodic_tick()
    clock[0] += timedelta(seconds=15)
    await r._regime_periodic_tick()
    assert r.regime_flip_tick.await_count == 2
    patch_inplace(cfg, cfg.model_copy(update={"regime_flip_check_min": 0}), HOT_FAST)
    clock[0] += timedelta(hours=1)
    await r._regime_periodic_tick()
    assert r.regime_flip_tick.await_count == 2


async def test_panel_sentinel_logs_and_trigger_edge_payload(tmp_path: Path, now: datetime) -> None:
    r, _, _, clock, px = await setup(tmp_path, now)
    px[0] = 100.3
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()
    page = build_team(PanelFiles(tmp_path, "UTC"), clock[0], Role.SENTINEL)
    assert page.regime_flip is None  # 疑似没有虚线
    assert "疑似" in page.records[0].text
    px[0] = 102
    for _ in range(2):
        clock[0] += timedelta(minutes=5)
        await r.regime_flip_tick()
    await finish(r)
    page = build_team(PanelFiles(tmp_path, "UTC"), clock[0], Role.SENTINEL)
    assert page.regime_flip is not None and "提前开整轮会" in page.regime_flip.text
    assert {item.output["regime_flip"]["kind"] for item in page.records} >= {
        "suspect_breadth",
        "blocked",
        "confirmed",
        "rerun_result",
    }
    assert (
        build_team(PanelFiles(tmp_path, "UTC"), clock[0], Role.GATE).regime_flip == page.regime_flip
    )


async def test_bad_or_legacy_reference_never_guesses_direction(
    tmp_path: Path, now: datetime
) -> None:
    r, c, eng, _, _ = await setup(tmp_path, now)
    path = tmp_path / "battle_card.json"
    saved = json.loads(path.read_text())
    saved["regime_reference"] = {"ts": "bad", "direction": "看涨", "btc_px": 0}
    path.write_text(json.dumps(saved))
    new = BattleCardCommander(eng, RISK, r.app.log, clock=r.app.clock, card_path=path)
    assert new.current(now) is not None and new.regime_reference is None
    del saved["regime_reference"]
    path.write_text(json.dumps(saved))
    new = BattleCardCommander(eng, RISK, r.app.log, clock=r.app.clock, card_path=path)
    assert new.current(now) is not None and new.regime_reference is None
    assert c.regime_reference is not None


async def test_expired_card_still_restores_arbitrary_flip_gap(
    tmp_path: Path, now: datetime
) -> None:
    r, c, eng, clock, _ = await setup(tmp_path, now)
    c.mark_regime_flip(now)
    clock[0] += timedelta(days=2)
    restored = BattleCardCommander(
        eng, RISK, r.app.log, clock=r.app.clock, card_path=tmp_path / "battle_card.json"
    )
    assert restored.last_regime_flip_at == now
    assert restored.regime_reference is None  # 旧卡不能成为当前方向参照


async def test_reference_price_is_pick_time_not_last_symbol_finish(
    tmp_path: Path,
    now: datetime,
) -> None:
    r, c, eng, clock, px = await setup(tmp_path, now)
    analyze = eng.analyze

    async def delayed(*args, **kw):
        px[0] = 120
        clock[0] += timedelta(minutes=7)
        return await analyze(*args, **kw)

    eng.analyze = delayed
    clock[0] += timedelta(minutes=5)
    pick_at = clock[0]
    px[0] = 102
    await c.refresh(r.app.pool, "regime_flip")
    assert c.regime_reference.ts == pick_at
    assert c.regime_reference.btc_px == 102
    assert clock[0] > pick_at and px[0] == 120


async def test_settings_change_while_rescreening_aborts_and_logs(
    tmp_path: Path,
    now: datetime,
) -> None:
    r, _, eng, clock, px = await setup(tmp_path, now)
    px[0] = 102
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()

    async def rescreen(_):
        cfg = r.app.app_cfg.fast_loop
        patch_inplace(cfg, cfg.model_copy(update={"regime_flip_confirm": 3}), HOT_FAST)

    r._screen_now = rescreen
    clock[0] += timedelta(minutes=5)
    await r.regime_flip_tick()
    await finish(r)
    assert eng.pick_calls == 1 and not r._refreshing
    assert "阈值已更新" in records(tmp_path / "regime_flip")[-1]["blocked_reason"]


async def test_reference_price_freshness_can_be_disabled(tmp_path: Path, now: datetime) -> None:
    r, _, _, _, _ = await setup(tmp_path, now)

    class StaleFeatures:
        def snapshot(self, symbol):
            return features(now - timedelta(minutes=20), price=100)

    r.m = dataclasses.replace(r.m, features=StaleFeatures())
    assert r._regime_btc_price() is None
    cfg = r.app.app_cfg.fast_loop
    patch_inplace(cfg, cfg.model_copy(update={"regime_flip_price_max_age_min": 0}), HOT_FAST)
    assert r._regime_btc_price() == 100
