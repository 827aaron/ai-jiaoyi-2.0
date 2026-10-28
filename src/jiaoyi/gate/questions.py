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
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from jiaoyi.contracts import (
    ENTRY_CHOICES,
    ENTRY_ONE,
    HOLD_CHOICES,
    AgainstSignalEvent,
    BattleCard,
    CandidateSignal,
    EntryQuestion,
    FeatureSnapshot,
    Forecast,
    HoldQuestion,
    JevQuestion,
    JevRequest,
    JevResponse,
    MarketView,
    PositionRecord,
    Side,
)
from jiaoyi.gate.context import EntryExtras

TEMPLATE_VERSION = "q9"
PROMPT_VERSION = TEMPLATE_VERSION  # 旧名字，保留兼容
SNAPSHOT_SCHEMA = "s6"
"""快照字段（``market_block`` / ``entry_snapshot`` / ``hold_snapshot`` 的键）变了也要升模板版本。"""
MAX_SNAPSHOT_CHARS = 3200
"""一道题问法加了技术面、成交流、衍生品、完整作战卡，中位约 1750 字，最长约 2600（spec §3e）。"""

# 每个模板版本登记一次指纹，历史版本不删，方便对照旧日志
KNOWN_TEMPLATES: dict[str, str] = {
    "q1": "f79b89bdad4e",
    "q2": "41997c5bfdef",  # 说明 atr_bps 是 stop_atr 的单位（ATR 改成 15 分钟口径）
    # 时间按最长持仓档（快照 hold_min）问，不再写死 5–30 / 15 分钟；atr_bps = 止损 ATR（√短×长）
    "q3": "cc0b4b385bae",
    # 新信号研究 10-24：快照加持仓量、多空比、BTC 5 分钟倍数、10 秒折算量比（拿不到不写）
    "q4": "8422594b5239",
    # 管家方案 E（Aaron 10-24）：持仓快照加进场价 / 最高浮盈 / 回吐 / R / 扣费后 / 历次浮盈、
    # 进场理由、移动止损状态、预测员；第 1、3 题措辞改；进场题目和快照不变
    "q5": "359a65892452",
    # 拍板人一道题（Aaron 10-24）：只问「做不做」，按持仓档问时长（2h 档问 30 分钟）；快照加
    # 技术面 / 成交流 / 衍生品 / 24 小时行情 / 完整作战卡，不带预测员；上限 3200 字
    "q6": "b514926fcf43",
    # 预测员改官方用法（Aaron 10-24）：管家快照的 kronos 改成「平均路径是否同向」+ 全池名次
    "q7": "2937b035dde4",
    # 10-25「没有新单」：一道题改回旧措辞，卡去掉理由 / 区间 / 允许信号 / 卡龄（A/B 0.08 → 0.59）
    "q8": "7309175f24c4",
    # 独立大盘：当前市场背景与每币原研究来源分列，字段结构 s6。
    "q9": "11822a6e9146",
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
    "of `stop_atr`); when present: `vol10_x` (last 10s volume scaled to 1m vs average), "
    "`oi15`/`oi60`/`oi4h` = open interest change %, `top_ls`/`crowd_ls` = top-trader "
    "position and all-account long/short ratios, `div4h` = top minus crowd 4h change %, "
    "`btc5z` = BTC 5-minute move in noise units; `book` has spread, imbalance, "
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
    " New inputs: `pos.entry_px`; `pos.peak_bps` = best unrealized PnL so far, "
    "`pos.giveback_bps` = how much of that peak is gone; `pos.pnl_r`/`pos.peak_r` = the same "
    "in units of the initial stop distance; `pos.be_bps` = PnL after round-trip fees; "
    "`pos.path_bps` = unrealized PnL at earlier checks, oldest first; `entry` = why we entered "
    "(signal type, trigger text, entry-time genuine/take probabilities); `trail` = trailing "
    "stop: on, active, and bps from price to the trailing stop; `kronos.agree` = 1 if the "
    "forecast model's averaged path ends in the position's direction, else 0; `kronos.rank` = "
    "where this coin's forecast return ranks among all coins forecast now (top/mid/bottom); "
    "`against` (only on urgent checks) = a "
    "rule trigger just fired in the OPPOSITE direction of our position: its type, side, volume "
    "multiple, taker buy share, whether it is a strong type, seconds ago."
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

ONE_TAKE_PROMPT = ENTRY_PROMPTS[EntryQuestion.TAKE] + " " + _ENTRY_GOAL
"""拍板人一道题「做不做」的题目说明：用回旧的「Considering everything, should we take this
trade」+ 目标说明。10-25 线上 45 条快照 A/B（/mnt/project-files/gate-*.jsonl）：原措辞
「Take it only if all of these hold together」+ 完整作战卡中位只有 0.08（≥0.55 为 0/90）；
这句 + 精简的卡、技术面等数据块照留，中位 0.59（54/90）。预测员不进快照。"""


HOLD_PROMPTS: dict[HoldQuestion, str] = {
    HoldQuestion.THESIS_INTACT: (
        "Is the reason we entered this position (`entry.sig`, `entry.why`) still valid? "
        + _HOLD_GOAL
    ),
    HoldQuestion.ADVERSE_RISK: (
        "How likely is a sharp move against the position in the next few minutes?"
    ),
    HoldQuestion.ACTION: (
        "Should we keep holding the whole position or close all of it now? If the position is "
        "in profit after fees (`pos.be_bps` > 0), weigh how much of the peak profit "
        "(`pos.peak_bps`, `pos.giveback_bps`) has already been given back and whether the "
        "trailing stop (`trail`) already protects it. A position that is losing but still "
        "inside its stop is not by itself a reason to close: the stop already limits that loss."
    ),
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


GateQuestions = str
"""``"one"``：一道「做不做」（+ 止损档、仓位档两题给风控）；``"six"``：旧的 6 道题（回放对照）。"""


def entry_questions(mode: GateQuestions = "six", hold_min: int = 120) -> list[JevQuestion]:
    """``mode="one"`` 时 ``take`` 用按持仓档的一道题说明，只问 ``ENTRY_ONE``。"""
    if mode == "one":
        return [
            JevQuestion(
                id=q.value,
                prompt=ONE_TAKE_PROMPT if q == EntryQuestion.TAKE else ENTRY_PROMPTS[q],
                choices=ENTRY_CHOICES[q],
            )
            for q in ENTRY_ONE
        ]
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
    """作战卡 ``stop_atr`` 乘的那个 ATR：有长周期就 √(短 × 长)，和风控员一致（不含时段系数）。"""
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
    # 新信号的数据（REST 轮询 / BTC）：拿不到的不写，不写 0
    if "vol_ratio_10s" in v:
        d["vol10_x"] = _r(v["vol_ratio_10s"], 2)
    for key, src, nd in (
        ("oi15", "oi15", 2),
        ("oi60", "oi60", 2),
        ("oi4h", "oi4h", 2),
        ("top_ls", "top_ls", 3),
        ("crowd_ls", "crowd_ls", 3),
        ("div4h", "div4h", 2),
        ("btc5z", "btc5z", 2),
    ):
        if src in v:
            d[key] = _r(v[src], nd)
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
        "regime": (cs.source_regime or card.regime)[:120],
        **({"source_market_view_id": cs.source_market_view_id} if cs.source_market_view_id else {}),
    }


def _current_market(
    card: BattleCard | None, view: MarketView | None = None, intra: str | None = None
) -> dict[str, Any]:
    """``intra``：大盘台最近一轮的「未来 1 小时概率 + 短线现状」（只在持仓复查里给，
    没有就不写这个键）。"""
    view = view or (card.current_market if card else None)
    if view is None:
        return {}
    out: dict[str, Any] = {
        "market_view_id": view.market_view_id,
        "version": view.version,
        "observed_at": view.observed_at.isoformat(),
        "source_ts": view.source_ts.isoformat() if view.source_ts else None,
        "regime": view.regime[:200],
        "regime_direction": view.regime_direction,
    }
    if intra:
        out["within_4h"] = intra[:400]
    return {"current_market": out}


def _js(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def _dump(d: dict[str, Any]) -> str:
    """超长按顺序删：``card.regime`` → ``day`` → ``flow.big_15m`` → ``kronos`` → ``book``
    （spec §3e），再不行删 ``why`` 和 ``card.reason``，免得硬截断成坏 JSON。"""
    s = _js(d)
    card = d.get("card")
    steps: list[Callable[[], object]] = [
        lambda: card.pop("regime", None) if isinstance(card, dict) else None,
        lambda: d.pop("day", None),
        lambda: d["flow"].pop("big_15m", None) if isinstance(d.get("flow"), dict) else None,
        lambda: d.pop("kronos", None),
        lambda: d.pop("book", None),
        lambda: d.pop("why", None),
        lambda: card.pop("reason", None) if isinstance(card, dict) else None,
        lambda: d.pop("deriv", None),  # 正常到不了这里（最长约 2600 字）；兜底别出坏 JSON
        lambda: d.pop("flow", None),
        lambda: d.pop("tech", None),
    ]
    for step in steps:
        if len(s) <= MAX_SNAPSHOT_CHARS:
            break
        if step() is not None:
            s = _js(d)
    return s[:MAX_SNAPSHOT_CHARS]


def _card_full(
    card: BattleCard, symbol: str, px: float, hold_min: int, now: datetime
) -> dict[str, Any]:
    """一道题问法的作战卡：方向、关键位（带支撑 / 阻力）、止损止盈范围、仓位上限、市场判断。

    理由、进场区间、是否在区间、允许的信号、卡龄不给：10-25 A/B 里这些让「做」普遍压到 0.1 以下
    （去掉后中位 0.08 → 0.32，再换回旧措辞 0.59）。"""
    cs = card.for_symbol(symbol)
    if cs is None:
        return {"status": "symbol_not_in_card", "regime": card.regime[:200]}
    return {
        "bias": cs.bias.value,
        "levels": [[_bps(lv.price, px), lv.kind.value] for lv in cs.key_levels][:6],
        "stop_atr": [cs.stop_atr.min, cs.stop_atr.max],
        "tp_r": [cs.take_profit_r.min, cs.take_profit_r.max],
        "size_max": cs.size_tier_max.value,
        "regime": (cs.source_regime or card.regime)[:200],
        **({"source_market_view_id": cs.source_market_view_id} if cs.source_market_view_id else {}),
    }


DEFAULT_HOLD_MIN = 120


def entry_levels_bps(signal: CandidateSignal, card: BattleCard) -> list[float]:
    """作战卡关键位离触发价多少基点（技术面块算「离最近关键位几个 ATR」用）。"""
    cs = card.for_symbol(signal.symbol)
    px = signal.features.last_price
    return [] if cs is None else [_bps(lv.price, px) for lv in cs.key_levels]


def entry_atr_bps(signal: CandidateSignal) -> float:
    f = signal.features
    return _r(_stop_atr(f) / f.last_price * 1e4) if f.last_price > 0 else 0.0


def entry_snapshot_one(
    signal: CandidateSignal,
    card: BattleCard,
    hold_min: int = DEFAULT_HOLD_MIN,
    extras: EntryExtras | None = None,
) -> str:
    """一道题问法的进场快照：市场块 + 完整作战卡 + 技术面 / 成交流 / 衍生品 / 24 小时行情。

    不带预测员（Aaron 10-24：预测员只在风控员那里按方向加分）。盘口失衡用 1 / 5 分钟平均，
    不用一瞬间的 ``imb``。"""
    f = signal.features
    d: dict[str, Any] = {
        "sym": signal.symbol,
        "side": "long" if signal.side_hint == Side.LONG else "short",
        "sig": signal.kind.value,
        "why": signal.trigger_desc[:120],
        "hold_min": hold_min,
        **market_block(f),
    }
    blocks = extras.blocks if extras is not None else {}
    book = d.get("book")
    if isinstance(book, dict):
        book.pop("imb", None)
        for k, v in (blocks.get("book_imb") or {}).items():
            book[f"imb_{k}"] = v
    for key in ("tech", "flow", "deriv", "day"):
        if blocks.get(key):
            d[key] = json.loads(json.dumps(blocks[key]))  # 拷一份：超长删键不动日志里的原值
    d["card"] = _card_full(card, signal.symbol, f.last_price, hold_min, signal.ts)
    d.update(_current_market(card))
    return _dump(d)


def entry_snapshot(
    signal: CandidateSignal, card: BattleCard, hold_min: int = DEFAULT_HOLD_MIN
) -> str:
    """旧的 6 道题问法的进场快照（``gate_questions = "six"``，回放对照用）。

    ``hold_min``：单笔最长持仓（分钟，``fast_loop.time_limit_s`` / 60），问题按它问时间。"""
    f = signal.features
    d: dict[str, Any] = {
        "sym": signal.symbol,
        "side": "long" if signal.side_hint == Side.LONG else "short",
        "sig": signal.kind.value,
        "why": signal.trigger_desc[:120],
        "hold_min": hold_min,
        **market_block(f),
        "card": _card_block(card, signal.symbol, f.last_price),
        **_current_market(card),
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


def hold_pnl_bps(pos: PositionRecord, px: float) -> float:
    """持仓浮盈（基点，未扣手续费），和快照里的 ``pos.pnl_bps`` 一致。"""
    entry = pos.entry_price or px
    sign = 1.0 if pos.side == Side.LONG else -1.0
    return _r(sign * (px / entry - 1.0) * 1e4)


def _path(history: Sequence[float], n: int = 5) -> list[int]:
    """之前历次复查的浮盈，等间隔抽最多 ``n`` 个，从早到晚。"""
    if len(history) <= n:
        return [round(x) for x in history]
    last = len(history) - 1
    return [round(history[round(i * last / (n - 1))]) for i in range(n)]


def hold_snapshot(
    pos: PositionRecord,
    f: FeatureSnapshot,
    card: BattleCard | None,
    now: datetime,
    *,
    against: AgainstSignalEvent | None = None,
    history: Sequence[float] = (),
    entry: Mapping[str, Any] | None = None,
    forecast: Forecast | None = None,
    fee_bps: float = 5.0,
    current_market: MarketView | None = None,
    market_intra: str | None = None,
) -> str:
    """``against``：主循环因「和卡相反的信号」叫的急查，带上那个信号（只这一次有这个键）。

    ``history``：这笔仓之前历次复查的浮盈（基点，从早到晚），算最高浮盈、回吐和走势；
    ``entry``：进场理由（拍板记录里的信号种类、触发说明、「真」「做」概率），拿不到不写；
    ``forecast``：这个币最新一条没过期的预测，拿不到不写；``fee_bps``：单边吃单手续费。
    进场快照和进场题目不变（方案 E 只改管家）。"""
    px = f.last_price
    pnl = hold_pnl_bps(pos, px)
    peak = max([*history, pnl])
    p: dict[str, Any] = {
        "side": pos.side.value,
        "pnl_bps": pnl,
        "age_min": _r((now - pos.opened_at).total_seconds() / 60.0),
        "to_stop_bps": _r(abs(px / pos.stop_loss_price - 1.0) * 1e4)
        if pos.stop_loss_price
        else None,
        "to_tp_bps": (
            _r(abs(pos.take_profit_price / px - 1.0) * 1e4) if pos.take_profit_price else None
        ),
        "time_left_min": _r((pos.time_limit_s - (now - pos.opened_at).total_seconds()) / 60.0),
    }
    if pos.entry_price:
        p["entry_px"] = pos.entry_price
    p["peak_bps"] = _r(peak)
    p["giveback_bps"] = _r(peak - pnl)
    if pos.entry_price and pos.stop_loss_price:
        stop = abs(pos.entry_price / pos.stop_loss_price - 1.0) * 1e4
        if stop > 0:
            p["pnl_r"] = _r(pnl / stop, 2)
            p["peak_r"] = _r(peak / stop, 2)
    p["be_bps"] = _r(pnl - 2.0 * fee_bps)
    if history:
        p["path_bps"] = _path(history)
    d: dict[str, Any] = {"sym": pos.symbol, "pos": p}
    if entry:
        d["entry"] = dict(entry)
    on = pos.trailing_activation_pct is not None and pos.trailing_stop_pct is not None
    trail: dict[str, Any] = {"on": on, "active": False}
    if on and pos.trailing_activation_pct is not None and pos.trailing_stop_pct is not None:
        active = peak >= pos.trailing_activation_pct * 100.0
        trail["active"] = active
        if active:
            trail["stop_bps"] = _r(pnl - (peak - pos.trailing_stop_pct * 100.0))
    d["trail"] = trail
    mkt = market_block(f)
    book = mkt.get("book")
    if isinstance(book, dict):  # 测下来是噪音，让出字数（进场快照照旧带）
        book.pop("imb", None)
        book.pop("micro_bps", None)
    d.update(mkt)
    d["card"] = _card_block(card, pos.symbol, px)
    d.update(_current_market(card, current_market, market_intra))
    if forecast is not None and forecast.stale_after > now:
        up = forecast.p_up if pos.side == Side.LONG else 1.0 - forecast.p_up
        kr: dict[str, Any] = {"agree": 1 if up > 0.5 else 0}
        if forecast.rank_view is not None:
            kr["rank"] = forecast.rank_view
        d["kronos"] = kr
    if against is not None:
        d["against"] = {
            "sig": against.kind.value,
            "side": against.side.value,
            "vol_x": _r(against.vol_ratio) if against.vol_ratio is not None else None,
            "buy": _r(against.buy_ratio, 2),
            "strong": against.strong,
            "age_s": _r((now - against.ts).total_seconds()),
        }
    return _dump(d)


# ---------------------------------------------------------------------- 版本和日志


def template_fingerprint() -> str:
    """问题措辞、选项说明和快照格式的哈希（前 12 位）。"""
    payload = {
        "entry": {q.value: ENTRY_PROMPTS[q] for q in EntryQuestion},
        "entry_one": ONE_TAKE_PROMPT,
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
        "entry_one": ONE_TAKE_PROMPT,
        "hold": {q.value: HOLD_PROMPTS[q] for q in HoldQuestion},
        "criteria": CRITERIA,
    }


def jev_log_fields(
    request: JevRequest | None, resp: JevResponse, extra: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """一次 Jev 调用写进 ``DecisionRecord`` 的 ``input`` / ``output``（拍板人和管家共用同一格式）。

    ``input.jev``：模板版本、指纹、请求号、类型、问题编号、完整快照；一道题问法还有 ``extra``。
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
    if extra is not None:
        inp["extra"] = dict(extra)  # 一道题问法的额外原始值（含快照超长删掉的），上线后逐项验证
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
