"""拍板人和持仓管家问决策模型的问题，以及压缩后的状态快照。

问题写法借鉴 jarrodwatts/jev-trader 的 ``src/model.ts``（MIT，提交 d6254bf）：把“目标、
时间、该看哪些输入”写进问题说明，模型判断更准。问题用英文写（模型对英文更稳），
注释和日志用中文。快照结构借鉴 buberlo/jev-trader 的 ``state/snapshot.py``（MIT，
提交 15d089e）：只发紧凑的数字，控制在约 400 token（2400 字符）以内，token 就是钱。

问题编号和选项固定在契约（``ENTRY_CHOICES`` / ``HOLD_CHOICES``）；这里只管措辞。
措辞一改，校准过的门槛就可能失效，所以改动要记版本号（``PROMPT_VERSION``）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from jiaoyi.contracts import (
    ENTRY_CHOICES,
    HOLD_CHOICES,
    BattleCard,
    CandidateSignal,
    EntryQuestion,
    FeatureSnapshot,
    HoldQuestion,
    JevQuestion,
    PositionRecord,
    Side,
)

PROMPT_VERSION = "q1"
MAX_SNAPSHOT_CHARS = 2400

_ENTRY_GOAL = (
    "Goal: decide whether to open a short-term directional position on a Binance USDT-M "
    "perpetual right now, in the direction `side` proposed by a rule-based trigger. "
    "Timing: market order now; the position lives minutes to a few hours and is closed by a "
    "stop at `stop_atr` x ATR, a take-profit at `tp_r` x stop distance, or a time limit. "
    "Round-trip taker fees are about 10 bps, so small moves lose money. "
    "Inputs: `sig`/`why` say what fired; `ret*_bps`, `vol_x` (1m volume vs average), "
    "`buy` (taker buy share, 1m), `cvd30_k` (taker buy minus sell, last 30s, kUSDT), "
    "`liq_*_k` (liquidations 1m, kUSDT), `fund_bps`; `book` has spread, imbalance, "
    "order-flow imbalance and depth; `card` is the strategist's plan for today "
    "(bias, key levels as bps from price); `kronos` is an optional price-range forecast."
)

_HOLD_GOAL = (
    "Goal: we hold an open position (`pos`). Decide whether to keep holding or close it now. "
    "You can only hold or close the whole position; the stop is never widened and size is "
    "never added. A wrong close costs fees and the remaining move; a wrong hold can cost up "
    "to the stop. Inputs: `pnl_bps`, `age_min`, distance to stop and take-profit in bps, "
    "recent returns, taker flow (`buy`, `cvd30_k`), liquidations, funding, the book, and "
    "whether today's strategist card still supports the trade (`card`)."
)

ENTRY_PROMPTS: dict[EntryQuestion, str] = {
    EntryQuestion.GENUINE: (
        "Is this trigger a genuine move that will follow through over the next 5-30 minutes, "
        "or a fake-out that reverses? " + _ENTRY_GOAL
    ),
    EntryQuestion.DIRECTION: "Where is price most likely to go over the next 15 minutes?",
    EntryQuestion.TOXICITY: (
        "Is the counterparty flow toxic for a trade in direction `side`, i.e. informed traders "
        "or a liquidity trap that will run us over right after we enter?"
    ),
    EntryQuestion.TAKE: (
        "Considering everything, should we take this trade in direction `side` now, after fees?"
    ),
    EntryQuestion.STOP_WIDTH: (
        "How wide should the stop be within the card's `stop_atr` range, given current noise?"
    ),
    EntryQuestion.SIZE: "How large should the position be within today's allowed sizes?",
}

HOLD_PROMPTS: dict[HoldQuestion, str] = {
    HoldQuestion.THESIS_INTACT: "Is the reason we entered this position still valid? " + _HOLD_GOAL,
    HoldQuestion.ADVERSE_RISK: (
        "How likely is a sharp move against the position in the next few minutes?"
    ),
    HoldQuestion.ACTION: "Should we keep holding the whole position or close all of it now?",
}

# 每个选项的含义，决策模型 SDK 支持就一并发过去
CRITERIA: dict[str, dict[str, str]] = {
    "genuine": {
        "real": "Genuine: the move is backed by flow and likely to continue",
        "fake": "Fake-out: likely to fade or reverse soon",
    },
    "direction": {
        "up": "Price higher in 15 minutes by more than fees",
        "down": "Price lower in 15 minutes by more than fees",
        "flat": "No meaningful move either way",
    },
    "toxicity": {
        "low": "Flow is benign; entering now is not adversely selected",
        "high": "Flow is informed or a trap; entering now is likely adversely selected",
    },
    "take": {"take": "Open the position now", "skip": "Do not trade this signal"},
    "stop_width": {
        "narrow": "Low end of the stop range: clean setup, low noise",
        "mid": "Middle of the stop range",
        "wide": "High end of the stop range: noisy, needs room",
    },
    "size": {
        "S": "Small: uncertain or noisy setup",
        "M": "Medium: decent setup",
        "L": "Large: clean, high-conviction setup",
    },
    "thesis_intact": {"yes": "The entry reason still holds", "no": "The entry reason is gone"},
    "adverse_risk": {
        "low": "A sharp adverse move is unlikely in the next few minutes",
        "high": "A sharp adverse move is likely in the next few minutes",
    },
    "action": {"hold": "Keep the whole position", "close": "Close the whole position now"},
}


def entry_questions() -> list[JevQuestion]:
    return [
        JevQuestion(id=q.value, prompt=ENTRY_PROMPTS[q], choices=ENTRY_CHOICES[q])
        for q in EntryQuestion
    ]


def hold_questions() -> list[JevQuestion]:
    return [
        JevQuestion(id=q.value, prompt=HOLD_PROMPTS[q], choices=HOLD_CHOICES[q])
        for q in HoldQuestion
    ]


# ---------------------------------------------------------------------- 快照


def _r(x: float, nd: int = 1) -> float:
    return round(x, nd)


def _bps(x: float, ref: float) -> float:
    return _r((x / ref - 1.0) * 1e4) if ref > 0 else 0.0


def market_block(f: FeatureSnapshot) -> dict[str, Any]:
    """特征 → 紧凑字典。键很短，省 token。"""
    v = f.values
    px = f.last_price
    d: dict[str, Any] = {
        "px": px,
        "atr_bps": _r(f.atr / px * 1e4) if px > 0 else 0.0,
        "ret1m_bps": _r(v.get("ret_1m", 0.0) * 1e4),
        "ret5m_bps": _r(v.get("ret_5m", 0.0) * 1e4),
        "vol_x": _r(v.get("vol_ratio_1m", 0.0), 2),
        "buy": _r(v.get("taker_buy_ratio_1m", 0.5), 2),
        "cvd30_k": _r(v.get("cvd_30s_usdt", 0.0) / 1e3),
        "rv5m_bps": _r(v.get("realized_vol_5m", 0.0) * 1e4, 2),
        "liq_long_k": _r(v.get("liq_long_usdt_1m", 0.0) / 1e3),
        "liq_short_k": _r(v.get("liq_short_usdt_1m", 0.0) / 1e3),
    }
    if "funding_rate" in v:
        d["fund_bps"] = _r(v["funding_rate"] * 1e4, 2)
    if f.book is not None:
        b = f.book
        d["book"] = {
            "spread_bps": _r(b.spread_bps, 2),
            "imb": _r(b.imbalance, 2),
            "micro_bps": _bps(b.microprice, b.mid),
            "ofi_k": _r(b.ofi / 1e3),
            "depth_k": _r(b.depth_usdt / 1e3),
        }
    return d


def _card_block(card: BattleCard | None, symbol: str, px: float) -> dict[str, Any]:
    if card is None:
        return {"status": "none"}
    cs = card.for_symbol(symbol)
    if cs is None:
        return {"status": "symbol_not_in_card", "regime": card.regime[:120]}
    return {
        "bias": cs.bias.value,
        "levels_bps": [_bps(lv.price, px) for lv in cs.key_levels][:6],
        "stop_atr": [cs.stop_atr.min, cs.stop_atr.max],
        "tp_r": [cs.take_profit_r.min, cs.take_profit_r.max],
        "size_max": cs.size_tier_max.value,
        "regime": card.regime[:120],
    }


def _dump(d: dict[str, Any]) -> str:
    s = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    if len(s) > MAX_SNAPSHOT_CHARS and isinstance(d.get("card"), dict):
        d["card"].pop("regime", None)
        d.pop("why", None)
        s = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    return s[:MAX_SNAPSHOT_CHARS]


def entry_snapshot(signal: CandidateSignal, card: BattleCard) -> str:
    f = signal.features
    d: dict[str, Any] = {
        "sym": signal.symbol,
        "side": "long" if signal.side_hint == Side.LONG else "short",
        "sig": signal.kind.value,
        "why": signal.trigger_desc[:120],
        **market_block(f),
        "card": _card_block(card, signal.symbol, f.last_price),
    }
    fc = signal.forecast
    if fc is not None and fc.stale_after > signal.ts:
        d["kronos"] = {
            "h_min": fc.horizon_min,
            "p_up": _r(fc.p_up, 2),
            "lo_bps": _bps(fc.price_low, f.last_price),
            "hi_bps": _bps(fc.price_high, f.last_price),
        }
    return _dump(d)


def hold_snapshot(
    pos: PositionRecord, f: FeatureSnapshot, card: BattleCard | None, now: datetime
) -> str:
    px = f.last_price
    entry = pos.entry_price or px
    sign = 1.0 if pos.side == Side.LONG else -1.0
    d: dict[str, Any] = {
        "sym": pos.symbol,
        "pos": {
            "side": pos.side.value,
            "pnl_bps": _r(sign * (px / entry - 1.0) * 1e4),
            "age_min": _r((now - pos.opened_at).total_seconds() / 60.0),
            "to_stop_bps": _r(abs(px / pos.stop_loss_price - 1.0) * 1e4)
            if pos.stop_loss_price
            else None,
            "to_tp_bps": (
                _r(abs(pos.take_profit_price / px - 1.0) * 1e4) if pos.take_profit_price else None
            ),
            "time_left_min": _r((pos.time_limit_s - (now - pos.opened_at).total_seconds()) / 60.0),
        },
        **market_block(f),
        "card": _card_block(card, pos.symbol, px),
    }
    return _dump(d)
