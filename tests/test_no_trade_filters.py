"""保镖「不做」过滤（新信号研究 10-24）：要下架、刚上线、自动减仓风险高、封币理由写全。"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.app import App
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.execution.exchange import _ms_time
from jiaoyi.loop import Runner
from jiaoyi.market.binance_ws import ContractInfo, parse_contract_info
from jiaoyi.market.derivs import DerivsFeed
from jiaoyi.risk.guard import RiskGuardImpl
from tests.fastloop_helpers import BTC_LOT, FAST, RISK, Lots, account, card, jev, signal

ROOT = Path(__file__).resolve().parents[1]


def _review(now: datetime, *, lot: Any = BTC_LOT, fast: c.FastLoopConfig = FAST, **kw: Any):  # type: ignore[no-untyped-def]
    g = RiskGuardImpl(RISK, fast, Lots(lot), **kw)
    return g.review(signal(now), jev(), card(now), account(now), now, market=signal(now).features)


def test_baseline_passes(now: datetime) -> None:
    assert _review(now).approved


def test_delisting_within_7_days_rejected(now: datetime) -> None:
    soon = dataclasses.replace(BTC_LOT, delist_at=now + timedelta(days=3))
    v = _review(now, lot=soon)
    assert not v.approved and any("下架" in r for r in v.reasons), v.reasons
    later = dataclasses.replace(BTC_LOT, delist_at=now + timedelta(days=30))
    assert _review(now, lot=later).approved


def test_new_listing_rejected_unless_zero(now: datetime) -> None:
    fresh = dataclasses.replace(BTC_LOT, onboard_at=now - timedelta(hours=10))
    v = _review(now, lot=fresh)
    assert not v.approved and any("上线不到 24 小时" in r for r in v.reasons), v.reasons
    assert _review(now, lot=fresh, fast=FAST.model_copy(update={"new_listing_block_h": 0})).approved
    old = dataclasses.replace(BTC_LOT, onboard_at=now - timedelta(days=3))
    assert _review(now, lot=old).approved


def test_adl_high_rejected_fail_open(now: datetime) -> None:
    v = _review(now, adl_risk=lambda s: "high")
    assert not v.approved and any("自动减仓" in r for r in v.reasons), v.reasons
    assert _review(now, adl_risk=lambda s: "medium").approved
    assert _review(now, adl_risk=lambda s: None).approved  # 查不到不挡
    off = FAST.model_copy(update={"adl_high_block": False})
    assert _review(now, fast=off, adl_risk=lambda s: "high").approved


def test_blocked_reason_written_in_full(now: datetime) -> None:
    g = RiskGuardImpl(
        RISK, FAST, Lots(), blocked=lambda: {"BTCUSDT": "币安公告要下架 BTCUSDT（10-24 发布）"}
    )
    blocked_card = card(now, bias="no_trade", allowed_signals=[])
    v = g.review(signal(now), jev(), blocked_card, account(now), now, market=signal(now).features)
    assert not v.approved
    assert "币安公告要下架 BTCUSDT（10-24 发布），不开新仓" in v.reasons
    assert "作战卡：这个标的只看不做" not in v.reasons


def test_exchange_info_times() -> None:
    assert _ms_time("4133404800000") is None  # 永续不交割 = 2100 年
    assert _ms_time(None) is None and _ms_time("x") is None
    assert _ms_time(1569398400000) == datetime(2019, 9, 25, 8, 0, tzinfo=UTC)


def test_parse_contract_info() -> None:
    msg = {"e": "contractInfo", "s": "ABCUSDT", "cs": "TRADING", "dt": 1793000000000}
    info = parse_contract_info(msg)
    assert info is not None and info.status == "TRADING" and info.delist_at is not None
    far = parse_contract_info({**msg, "dt": 4133404800000})
    assert far is not None and far.delist_at is None
    assert parse_contract_info({"e": "aggTrade"}) is None


async def test_adl_poll_and_stale() -> None:
    t = [1000.0]

    async def fetch(path: str, params: dict[str, Any], weight: int) -> Any:
        assert path == "/fapi/v1/symbolAdlRisk"
        return [{"symbol": "ABCUSDT", "adlRisk": "HIGH"}, {"symbol": "BTCUSDT", "adlRisk": "low"}]

    feed = DerivsFeed(fetch=fetch, clock=lambda: t[0])
    assert feed.adl_risk("ABCUSDT") is None
    await feed.poll_adl()
    assert feed.adl_risk("ABCUSDT") == "high" and feed.adl_risk("BTCUSDT") == "low"
    t[0] += 3 * 3600
    assert feed.adl_risk("ABCUSDT") is None  # 太旧当没有，不挡


class BlockCards:
    def __init__(self, card: c.BattleCard) -> None:
        self.card = card
        self.blocked: dict[str, str] = {}

    def current(self, now: datetime) -> c.BattleCard | None:
        return self.card

    def notify_news(self, symbol: str) -> None: ...

    def block_symbol(self, symbol: str, reason: str) -> None:
        self.blocked[symbol] = reason

    @property
    def blocked_symbols(self) -> dict[str, str]:
        return dict(self.blocked)


async def test_contract_info_push_blocks_watched_symbol(tmp_path: Path, now: datetime) -> None:
    cfg = load_app_config(ROOT / "config" / "app.example.toml").model_copy(
        update={"data_dir": str(tmp_path)}
    )
    app = App(
        load_risk_config(ROOT / "config" / "risk.example.toml"),
        cfg,
        clock=lambda: now,
        offline=True,
    )
    cards = BlockCards(card(now))
    app.m = dataclasses.replace(app.m, cards=cards)
    r = Runner(app)
    r._watched = {"BTCUSDT"}
    r.on_contract_info(ContractInfo("ETHUSDT", "SETTLING", None))  # 不是我们的币
    r.on_contract_info(ContractInfo("BTCUSDT", "TRADING", now + timedelta(days=60)))  # 还远
    assert cards.blocked == {}
    r.on_contract_info(ContractInfo("BTCUSDT", "TRADING", now + timedelta(days=2)))
    assert "下架" in cards.blocked["BTCUSDT"]
    await asyncio.sleep(0)
    assert (tmp_path / "blocked_symbols.json").is_file()
