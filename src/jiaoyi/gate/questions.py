"""拍板人和持仓管家问决策模型的问题，以及压缩后的状态快照。

问题写法借鉴 jarrodwatts/jev-trader 的 ``src/model.ts``（MIT，提交 d6254bf）：把“目标、
时间、该看哪些输入”写进问题说明，模型判断更准。问题用英文写（模型对英文更稳），
注释和日志用中文。快照结构借鉴 buberlo/jev-trader 的 ``state/snapshot.py``（MIT，
提交 15d089e）：只发紧凑的数字，控制在约 400 token（2400 字符）以内，token 就是钱。

问题编号和选项固定在契约（``ENTRY_CHOICES`` / ``HOLD_CHOICES``）；这里只管措辞。
措辞一改，校准过的门槛就可能失效，所以模板有版本号（``TEMPLATE_VERSION``）和内容指纹
（``template_fingerprint()``，问题措辞 + 选项说明 + 快照格式的哈希）。改了任何一个字，
指纹就变，``tests/test_jev_templates.py`` 会失败，提醒你升版本号并在 ``KNOWN_TEMPLATES``
里登记新指纹。每次调用 Jev 的决策日志都带版本号和指纹（``jev_log_fields``），M1 之后
按版本分组复盘、校准（``jiaoyi.gate.review``）。
"""

from __future__ import annotations

import hashlib
import json
import math
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
    JevRequest,
    JevResponse,
    PositionRecord,
    Side,
)

TEMPLATE_VERSION = "q3"
PROMPT_VERSION = TEMPLATE_VERSION  # 旧名字，保留兼容
SNAPSHOT_SCHEMA = "s2"
"""快照字段（``market_block`` / ``entry_snapshot`` / ``hold_snapshot`` 的键）变了也要升模板版本。"""
MAX_SNAPSHOT_CHARS = 2400

# 每个模板版本登记一次指纹，历史版本不删，方便对照旧日志
KNOWN_TEMPLATES: dict[str, str] = {
    "q1": "f79b89bdad4e",
    "q2": "41997c5bfdef",  # 说明 atr_bps 是 stop_atr 的单位（ATR 改成 15 分钟口径）
    # 时间按最长持仓档（快照 hold_min）问，不再写死 5–30 / 15 分钟；atr_bps = 止损 ATR（√短×长）
    "q3": "cc0b4b385bae",
}

_ENTRY_GOAL = (
    "Goal: decide whether to open a short-term directional position on a Binance USDT-M "
    "perpetual right now, in the direction `side` proposed by a rule-based trigger. "
    "Timing: market order now; the position lives up to `hold_min` minutes and is closed by a "
    "stop at `stop_atr` x ATR, a take-profit at `tp_r` x stop distance, or that time limit. "
    "Round-trip taker fees are about 10 bps, so small moves lose money. "
    "Inputs: `sig`/`why` say what fired; `ret*_bps`, `vol_x` (1m volume vs average), "
    "`buy` (taker buy share, 1m), `cvd30_k` (taker buy minus sell, last 30s, kUSDT), "
    "`liq_*_k` (liquidations 1m, kUSDT), `fund_bps`, `atr_bps` (ATR in bps, the unit "
    "of `stop_atr`); `book` has spread, imbalance, "
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
        "Is this trigger a genuine move that will follow through over the holding period "
        "(`hold_min` minutes), or a fake-out that reverses? " + _ENTRY_GOAL
    ),
    EntryQuestion.DIRECTION: (
        "Where is price most likely to be at the end of the holding period (`hold_min` minutes)?"
    ),
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
        "up": "Price higher at the end of the holding period by more than fees",
        "down": "Price lower at the end of the holding period by more than fees",
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


def _stop_atr(f: FeatureSnapshot) -> float:
    """作战卡 ``stop_atr`` 乘的那个 ATR：有长周期就 √(短 × 长)，和保镖一致（不含时段系数）。"""
    if f.atr_long:
        return math.sqrt(f.atr * f.atr_long)
    return f.atr


def market_block(f: FeatureSnapshot) -> dict[str, Any]:
    """特征 → 紧凑字典。键很短，省 token。"""
    v = f.values
    px = f.last_price
    d: dict[str, Any] = {
        "px": px,
        "atr_bps": _r(_stop_atr(f) / px * 1e4) if px > 0 else 0.0,
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


DEFAULT_HOLD_MIN = 120


def entry_snapshot(
    signal: CandidateSignal, card: BattleCard, hold_min: int = DEFAULT_HOLD_MIN
) -> str:
    """``hold_min``：单笔最长持仓（分钟，``fast_loop.time_limit_s`` / 60），问题按它问时间。"""
    f = signal.features
    d: dict[str, Any] = {
        "sym": signal.symbol,
        "side": "long" if signal.side_hint == Side.LONG else "short",
        "sig": signal.kind.value,
        "why": signal.trigger_desc[:120],
        "hold_min": hold_min,
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


# ---------------------------------------------------------------------- 版本和日志


def template_fingerprint() -> str:
    """问题措辞、选项说明和快照格式的哈希（前 12 位）。"""
    payload = {
        "entry": {q.value: ENTRY_PROMPTS[q] for q in EntryQuestion},
        "hold": {q.value: HOLD_PROMPTS[q] for q in HoldQuestion},
        "criteria": CRITERIA,
        "snapshot": SNAPSHOT_SCHEMA,
        "max_chars": MAX_SNAPSHOT_CHARS,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def template_dump() -> dict[str, Any]:
    """当前模板全文，复盘时对照用（可以写成 data/jev_templates/<版本>.json）。"""
    return {
        "version": TEMPLATE_VERSION,
        "fingerprint": template_fingerprint(),
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "entry": {q.value: ENTRY_PROMPTS[q] for q in EntryQuestion},
        "hold": {q.value: HOLD_PROMPTS[q] for q in HoldQuestion},
        "criteria": CRITERIA,
    }


def jev_log_fields(
    request: JevRequest | None, resp: JevResponse
) -> tuple[dict[str, Any], dict[str, Any]]:
    """一次 Jev 调用写进 ``DecisionRecord`` 的 ``input`` / ``output``（拍板人和管家共用同一格式）。

    ``input.jev``：模板版本、指纹、请求号、类型、问题编号、完整快照。
    ``output.jev``：是否成功、错误、模型版本、延迟、花费、每个问题每个选项的概率。
    ``output.usage``：token 用量 ``{input_tokens, output_tokens, reasoning_tokens}``（面板 AI
    费用详情读这里）；没真正调用时为 None。
    没有真正调用（例如没有作战卡被拦下）时 ``request`` 为 None，``input.jev.called`` 为 False。
    """
    inp: dict[str, Any] = {
        "template": TEMPLATE_VERSION,
        "template_fp": template_fingerprint(),
        "called": request is not None,
    }
    if request is not None:
        inp.update(
            request_id=request.request_id,
            kind=request.kind,
            questions=[q.id for q in request.questions],
            snapshot=request.snapshot,
        )
    out: dict[str, Any] = {
        "ok": resp.ok,
        "error": resp.error,
        "model": resp.model_version,
        "latency_ms": round(resp.latency_ms, 1),
        "cost_usd": resp.cost_usd,
        "answers": {a.id: a.probs for a in resp.answers} if resp.ok else {},
    }
    usage = resp.usage.model_dump() if resp.usage is not None else None
    return {"jev": inp}, {"jev": out, "usage": usage}
