"""快环测试共用的构造函数：行情、特征、作战卡、Jev 回答、账户。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.core.config import load_risk_config
from jiaoyi.risk.guard import LotRules
from tests.conftest import ROOT, load_example

# 示例里价差 / 滑点固定 bp 默认不写（只按止损比例）；这里写上，顺带测「写了就和比例取小」
RISK = load_risk_config(ROOT / "config" / "risk.example.toml").model_copy(
    update={"max_spread_bps": 5.0, "max_slippage_bps": 10.0}
)
FAST = c.FastLoopConfig()


def card(now: datetime, **sym_over: Any) -> c.BattleCard:
    data = load_example("battle_card")
    data["created_at"] = (now - timedelta(minutes=10)).isoformat()
    data["expires_at"] = (now + timedelta(hours=1)).isoformat()
    data["symbols"][0].update(sym_over)
    return c.BattleCard.model_validate(data)


def book(price: float = 68000.0, spread: float = 0.5) -> c.BookMetrics:
    return c.BookMetrics(
        mid=price,
        microprice=price + 0.1,
        spread_bps=spread / price * 1e4,
        depth_usdt=2_000_000.0,
        imbalance=0.3,
        ofi=10_000.0,
        cvd=50_000.0,
        taker_buy_ratio=0.65,
    )


def features(
    now: datetime, price: float = 68000.0, atr: float = 80.0, **values: float
) -> c.FeatureSnapshot:
    base = {
        "ret_1m": 0.002,
        "ret_5m": 0.004,
        "vol_ratio_1m": 4.0,
        "taker_buy_ratio_1m": 0.65,
        "cvd_30s_usdt": 30_000.0,
        "realized_vol_5m": 0.0004,
    }
    base.update(values)
    return c.FeatureSnapshot(
        symbol="BTCUSDT", ts=now, last_price=price, atr=atr, values=base, book=book(price)
    )


def signal(
    now: datetime,
    side: c.Side = c.Side.LONG,
    kind: c.SignalKind = c.SignalKind.VOLUME_BREAKOUT,
    f: c.FeatureSnapshot | None = None,
    card_id: str = "card-20261022-0600",
    card_version: int = 1,
) -> c.CandidateSignal:
    f = f or features(now)
    return c.CandidateSignal(
        signal_id="sig-test-1",
        symbol="BTCUSDT",
        kind=kind,
        side_hint=side,
        ts=now,
        trigger_price=f.last_price,
        trigger_desc="测试",
        dedup_key=f"BTCUSDT:{kind.value}:{side.value}",
        card_id=card_id,
        card_version=card_version,
        features=f,
    )


GOOD_LONG: dict[str, dict[str, float]] = {
    "genuine": {"real": 0.8, "fake": 0.2},
    "direction": {"up": 0.6, "down": 0.2, "flat": 0.2},
    "toxicity": {"low": 0.8, "high": 0.2},
    "take": {"take": 0.75, "skip": 0.25},
    "stop_width": {"narrow": 0.2, "mid": 0.6, "wide": 0.2},
    "size": {"S": 0.2, "M": 0.6, "L": 0.2},
}


def jev(
    probs: Mapping[str, Mapping[str, float]] | None = None, ok: bool = True, **over: Any
) -> c.JevResponse:
    if not ok:
        return c.JevResponse(
            request_id="r1", ok=False, error="超时", model_version="jev-test", latency_ms=1000.0
        )
    p = {k: dict(v) for k, v in (probs or GOOD_LONG).items()}
    p.update(over)
    return c.JevResponse(
        request_id="r1",
        ok=True,
        model_version="jev-test",
        latency_ms=300.0,
        answers=[c.JevAnswer(id=k, probs=v) for k, v in p.items()],
    )


def account(now: datetime, equity: float = 10_000.0, **over: Any) -> c.AccountState:
    data: dict[str, Any] = {
        "ts": now,
        "equity_usdt": equity,
        "available_usdt": equity,
        "day_start_equity_usdt": equity,
        "open_notional_usdt": 0.0,
        "open_positions": 0,
    }
    data.update(over)
    return c.AccountState.model_validate(data)


BTC_LOT = LotRules(step_size=0.001, min_qty=0.001, min_notional=5.0)


class Lots:
    def __init__(self, rules: LotRules | None = BTC_LOT) -> None:
        self.rules = rules

    def lot_rules(self, symbol: str) -> LotRules | None:
        return self.rules


def answer_with(probs: Mapping[str, Mapping[str, float]]) -> Any:
    def f(state: str, questions: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        return {q.id: probs[q.id] for q in questions}

    return f
