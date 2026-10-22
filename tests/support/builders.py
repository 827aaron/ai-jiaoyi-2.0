"""契约对象的构造器：给出能通过校验的默认值，测试里只改关心的字段。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jiaoyi.contracts import (
    AccountState,
    AppConfig,
    BattleCard,
    Bias,
    BookMetrics,
    CandidateSignal,
    CardSymbol,
    Category,
    FeatureSnapshot,
    JevAnswer,
    JevQuestion,
    JevRequest,
    JevResponse,
    PositionRecord,
    Range,
    RiskConfig,
    Side,
    SignalKind,
    SizeTier,
)
from jiaoyi.contracts.jev import ENTRY_CHOICES, EntryQuestion
from jiaoyi.contracts.position import PositionStatus

T0 = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)

RISK_DEFAULTS: dict[str, Any] = {
    "risk_per_trade_pct": 1.0,
    "max_leverage": 5,
    "max_total_exposure_x": 3.0,
    "category_exposure_x": {
        "major": 2.0,
        "alt": 1.0,
        "crypto_stock": 1.0,
        "us_tech": 1.0,
        "precious_metal": 1.0,
        "other": 0.5,
    },
    "daily_loss_halt_pct": 3.0,
    "max_notional_per_trade_usdt": 2000.0,
    "max_open_positions": 5,
    "lifeboat_stop_multiplier": 1.5,
    "max_spread_bps": 5.0,
    "max_slippage_bps": 10.0,
    "taker_fee_bps": 5.0,
    "min_tp_fee_multiple": 2.0,
    "day_reset_tz": "Asia/Bangkok",
}

APP_DEFAULTS: dict[str, Any] = {
    "mode": "paper",
    "predictor_enabled": False,
    "signal_dedup_s": 30,
    "jev_timeout_ms": 1500,
    "keeper_interval_s": 20,
    "reconcile_interval_s": 60,
    "slow_loop_interval_min": 60,
    "pool_size": 40,
    "card_pick_max": 15,
    "data_dir": "data",
}


def risk_config(**over: Any) -> RiskConfig:
    return RiskConfig.model_validate({**RISK_DEFAULTS, **over})


def app_config(**over: Any) -> AppConfig:
    return AppConfig.model_validate({**APP_DEFAULTS, **over})


def card(
    symbols: list[CardSymbol] | None = None,
    *,
    now: datetime = T0,
    ttl: timedelta = timedelta(hours=2),
    card_id: str = "card-test",
) -> BattleCard:
    return BattleCard(
        card_id=card_id,
        version=1,
        created_at=now - timedelta(minutes=1),
        expires_at=now + ttl,
        source="replay",
        regime="测试用",
        symbols=symbols or [card_symbol()],
    )


def card_symbol(symbol: str = "BTCUSDT", **over: Any) -> CardSymbol:
    base: dict[str, Any] = {
        "symbol": symbol,
        "category": Category.MAJOR,
        "bias": Bias.BOTH,
        "allowed_signals": list(SignalKind),
        "size_tier_max": SizeTier.L,
        "notional_usdt": Range(min=100.0, max=2000.0),
        "leverage": Range(min=1.0, max=3.0),
        "stop_atr": Range(min=1.0, max=2.0),
        "take_profit_r": Range(min=1.5, max=3.0),
        "reason": "测试",
    }
    return CardSymbol.model_validate({**base, **over})


def features(
    symbol: str = "BTCUSDT",
    *,
    price: float = 68_000.0,
    atr: float = 100.0,
    spread_bps: float = 1.0,
    now: datetime = T0,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        ts=now,
        last_price=price,
        atr=atr,
        values={"vol_ratio_1m": 4.0},
        book=BookMetrics(
            mid=price,
            microprice=price,
            spread_bps=spread_bps,
            depth_usdt=500_000.0,
            imbalance=0.2,
            ofi=10.0,
            cvd=5.0,
            taker_buy_ratio=0.6,
        ),
    )


def signal(
    symbol: str = "BTCUSDT",
    *,
    side: Side = Side.LONG,
    kind: SignalKind = SignalKind.VOLUME_BREAKOUT,
    price: float = 68_000.0,
    atr: float = 100.0,
    spread_bps: float = 1.0,
    now: datetime = T0,
    card_id: str | None = "card-test",
    signal_id: str = "sig-1",
) -> CandidateSignal:
    return CandidateSignal(
        signal_id=signal_id,
        symbol=symbol,
        kind=kind,
        side_hint=side,
        ts=now,
        trigger_price=price,
        trigger_desc="测试触发",
        dedup_key=f"{symbol}:{kind.value}:{side.value}",
        card_id=card_id,
        features=features(symbol, price=price, atr=atr, spread_bps=spread_bps, now=now),
    )


def account(
    *,
    equity: float = 10_000.0,
    day_start: float | None = None,
    open_notional: float = 0.0,
    by_category: dict[Category, float] | None = None,
    positions: int = 0,
    paused: bool = False,
    now: datetime = T0,
) -> AccountState:
    return AccountState(
        ts=now,
        equity_usdt=equity,
        available_usdt=equity,
        day_start_equity_usdt=day_start if day_start is not None else equity,
        open_notional_usdt=open_notional,
        open_notional_by_category=by_category or {},
        open_positions=positions,
        paused=paused,
    )


DEFAULT_ANSWERS: dict[str, str] = {
    "genuine": "real",
    "direction": "up",
    "toxicity": "low",
    "take": "take",
    "stop_width": "mid",
    "size": "M",
}


def entry_answers(
    choices: dict[str, str] | None = None, confidence: float = 0.8
) -> list[JevAnswer]:
    """每题给选中项 ``confidence``，其余平分。"""
    chosen = {**DEFAULT_ANSWERS, **(choices or {})}
    out: list[JevAnswer] = []
    for q in EntryQuestion:
        opts = ENTRY_CHOICES[q]
        rest = (1.0 - confidence) / (len(opts) - 1)
        out.append(
            JevAnswer(
                id=q.value, probs={o: (confidence if o == chosen[q.value] else rest) for o in opts}
            )
        )
    return out


def jev_ok(
    choices: dict[str, str] | None = None,
    *,
    confidence: float = 0.8,
    latency_ms: float = 350.0,
    request_id: str = "req-1",
) -> JevResponse:
    return JevResponse(
        request_id=request_id,
        ok=True,
        model_version="test-model-1",
        latency_ms=latency_ms,
        cost_usd=0.001,
        answers=entry_answers(choices, confidence),
    )


def jev_failed(error: str = "超时", request_id: str = "req-1") -> JevResponse:
    return JevResponse(
        request_id=request_id,
        ok=False,
        error=error,
        model_version="test-model-1",
        latency_ms=1500.0,
    )


def entry_request(sig: CandidateSignal, now: datetime | None = None) -> JevRequest:
    """开仓 6 问的标准请求。措辞是测试用的，真实措辞归拍板人模块。"""
    return JevRequest(
        request_id=f"req-{sig.signal_id}",
        kind="entry",
        symbol=sig.symbol,
        ts=now or sig.ts,
        signal_id=sig.signal_id,
        snapshot=f"{sig.symbol} {sig.kind.value} {sig.side_hint.value} @ {sig.trigger_price}",
        questions=[
            JevQuestion(id=q.value, prompt=f"问题：{q.value}", choices=ENTRY_CHOICES[q])
            for q in EntryQuestion
        ],
    )


def position(
    symbol: str = "BTCUSDT",
    *,
    side: Side = Side.LONG,
    entry: float = 68_000.0,
    now: datetime = T0,
    position_id: str = "pos-1",
) -> PositionRecord:
    return PositionRecord(
        position_id=position_id,
        group_id="grp-1",
        intent_id="int-1",
        signal_id="sig-1",
        card_id="card-test",
        symbol=symbol,
        category=Category.MAJOR,
        side=side,
        status=PositionStatus.OPEN,
        quantity=0.01,
        leverage=3,
        entry_price=entry,
        stop_loss_price=entry * (0.998 if side is Side.LONG else 1.002),
        time_limit_s=300,
        opened_at=now,
    )
