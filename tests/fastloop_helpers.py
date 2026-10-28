"""快环测试共用的构造函数：行情、特征、作战卡、Jev 回答、账户。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
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
    # 示例卡的进场区间（66800–67300）不含测试价 68000：默认去掉，要测区间就显式传 entry_zone
    data["symbols"][0]["entry_zone"] = None
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
        "live_s": 3600.0,
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


class FakeMarketRest:
    """假的币安公开行情 REST（``MarketData`` 的 ``fetch``）：``/fapi/v1/aggTrades`` 和
    ``/fapi/v1/premiumIndex``。``trades[币]`` 是 [(时间, 价格, 名义 U, 主动买)]，按时间排。

    ``fail``：这些币一请求就抛这个异常；``delay``：每次请求等几秒（测并发）；``on_call``：
    每次请求先调一下（测预算节奏时记时间）。"""

    def __init__(
        self,
        trades: Mapping[str, Sequence[tuple[datetime, float, float, bool]]],
        *,
        now: datetime,
        funding: float = 0.0001,
        delay: float = 0.0,
        fail: Mapping[str, Exception] | None = None,
        on_call: Callable[[str, dict[str, Any], int], None] | None = None,
    ) -> None:
        self.trades = trades
        self.now = now
        self.funding = funding
        self.delay = delay
        self.fail = dict(fail or {})
        self.on_call = on_call
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.inflight = 0
        self.max_inflight = 0

    def agg_calls(self, symbol: str | None = None) -> int:
        return sum(
            1
            for p, q, _ in self.calls
            if p == "/fapi/v1/aggTrades" and (symbol is None or q["symbol"] == symbol)
        )

    async def __call__(self, path: str, params: dict[str, Any], weight: int) -> Any:
        self.calls.append((path, dict(params), weight))
        if self.on_call is not None:
            self.on_call(path, params, weight)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            sym = params["symbol"]
            if sym in self.fail:
                raise self.fail[sym]
            rows = self.trades.get(sym, [])
            if path == "/fapi/v1/premiumIndex":
                px = str(rows[-1][1]) if rows else "1.0"
                return {
                    "symbol": sym,
                    "markPrice": px,
                    "indexPrice": px,
                    "lastFundingRate": str(self.funding),
                    "nextFundingTime": int((self.now + timedelta(hours=1)).timestamp() * 1000),
                    "time": int(self.now.timestamp() * 1000),
                }
            out = [
                {"a": i, "p": str(p), "q": str(u / p), "T": int(at.timestamp() * 1000), "m": not b}
                for i, (at, p, u, b) in enumerate(rows)
            ]
            limit = int(params["limit"])
            if "fromId" in params:
                return [r for r in out if r["a"] >= int(params["fromId"])][:limit]
            return out[-limit:]
        finally:
            self.inflight -= 1
