"""大盘台：独立判断「候选池等权组合未来 4 小时」涨 / 平 / 跌（设计 2026-10-28，CONFLICTS 复核）。

每 ``cadence_min`` 分钟（默认 HH:02 UTC）组装一份数据包（``market_dossier``），用 DeepSeek V4.1
Flash（经 OpenRouter，和开会同一个模型）独立问 ``samples`` 次，概率取平均；方向由代码按 θ / δ 派生，
不让模型直接说方向。1/6 的轮次再把数据包涨跌取反问一遍（镜像测试），量方向偏置。

``mode = "replace"`` 时大盘台是唯一的大盘发布者：经指挥官的 ``_new_market_generation`` /
``_publish_market`` 同一条路发布 ``MarketView``（``source = "market_desk"``，共用版本号，管家复核和
追单检查照旧），只在方向变化且滞回确认后才发新版本；翻转参照只由它设。三项概率、desk_id 等放在
``data/market_desk_state.json``，不往 ``MarketView`` 加字段（契约 extra=forbid，回滚安全）。

``mode = "off"``（默认）不建这个对象，一次模型调用都没有。

花费单独记账（不占军师团日上限），但每轮写一条 COMMANDER 决策记录带 ``cost_usd``，面板总 AI 费
照算。OpenRouter 余额低于 ``balance_floor_usd`` 时自停，把余额留给开会和 Jev。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import math
import os
import re
import statistics
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx

from jiaoyi.contracts import DecisionRecord, MarketView, Outcome, Role, SymbolPool
from jiaoyi.interfaces import DecisionLog
from jiaoyi.slow import binance_gate, market_table
from jiaoyi.slow.credits import is_credit_error
from jiaoyi.slow.desk_intra import INTRA_FIELDS, compose_intra
from jiaoyi.slow.market_dossier import BTC, Dossier, DossierBuilder
from jiaoyi.slow.settings import MarketDeskSection
from jiaoyi.slow.tradingagents_engine import openrouter_body

logger = logging.getLogger(__name__)

DESK_SOURCE = "market_desk"
STATE_FILE = "market_desk_state.json"
LOG_DIR = "market_desk"
KLINE_CACHE = "klines_1h.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PROMPT_VERSION = "desk-v1"
MIRROR_WINDOW = 12
"""镜像偏置看最近几次镜像测试的平均。"""
MIRROR_MIN_TESTS = 3
"""至少几次镜像测试才判偏置（一次的噪声太大）。"""
RAW_KEEP = 8000
"""日志里每次回答原文最多留多少字。"""
FLASH_PRICE_PER_MTOK = (0.15, 0.60)
"""通道没报 usage.cost 时的估算价（OpenRouter deepseek-v4.1-flash，2026-10-28 核对）。"""

Direction = Literal["看涨", "看跌", "中性"]
Order = Literal["up_first", "down_first"]


# ------------------------------------------------------------------ 中立措辞

SWAP_PAIRS: tuple[tuple[str, str], ...] = (
    ("p1h_up", "p1h_down"),
    ("p_up", "p_down"),
    ("bull_case", "bear_case"),
    ("UP", "DOWN"),
    ("up", "down"),
    ("rise", "fall"),
    ("rising", "falling"),
    ("long", "short"),
    ("longs", "shorts"),
    ("bull", "bear"),
    ("bullish", "bearish"),
    ("buy", "sell"),
    ("buyers", "sellers"),
    ("above", "below"),
    ("higher", "lower"),
    ("high", "low"),
    ("support", "resistance"),
    ("rebound", "pullback"),
    ("positive", "negative"),
    ("gain", "loss"),
    ("看涨", "看跌"),
    ("上涨", "下跌"),
    ("多头", "空头"),
    ("做多", "做空"),
    ("买入", "卖出"),
    ("反弹", "回落"),
    ("支撑", "阻力"),
    ("涨", "跌"),
    ("多", "空"),
)
"""多↔空、涨↔跌、支撑↔阻力、反弹↔回落……对调表。提示词每一句偏向一边的话都要有对调后的另一句
（或者一句里两边都有）；``tests/test_market_desk.py`` 逐句检查。"""

_SWAP = {a: b for a, b in SWAP_PAIRS} | {b: a for a, b in SWAP_PAIRS}
_SWAP_RE = re.compile(
    "|".join(
        rf"(?<![A-Za-z0-9_]){re.escape(w)}(?![A-Za-z0-9_])" if w.isascii() else re.escape(w)
        for w in sorted(_SWAP, key=len, reverse=True)
    )
)


def swap_words(text: str) -> str:
    """按对调表把一句话里多空两边的词互换（测试和日志自查用）。"""
    return _SWAP_RE.sub(lambda m: _SWAP[m.group(0)], text)


def cycle_order(cycle_id: str) -> Order:
    """这一轮涨 / 跌谁先出现（提示词选项、数据表成对字段、回答键的顺序）。
    按轮次哈希交替，可复现。"""
    h = int(hashlib.sha256(f"order:{cycle_id}".encode()).hexdigest()[:8], 16)
    return "up_first" if h % 2 == 0 else "down_first"


def mirror_selected(cycle_id: str, rate: float) -> bool:
    """这一轮做不做镜像测试：按轮次哈希抽 ``rate`` 的比例，可复现。"""
    if rate <= 0:
        return False
    h = int(hashlib.sha256(f"mirror:{cycle_id}".encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < rate


def _ordered(order: Order, up: str, down: str) -> tuple[str, str]:
    return (up, down) if order == "up_first" else (down, up)


def instruction_lines(order: Order, band: float) -> list[str]:
    """提示词的说明部分（英文），每个元素是一句。偏向一边的句子成对出现、长度相当。"""
    p_a, p_b = _ordered(order, "p_up", "p_down")
    h_a, h_b = _ordered(order, "p1h_up", "p1h_down")
    c_a, c_b = _ordered(order, "bull_case", "bear_case")
    cls_a, cls_b = _ordered(
        order,
        f"UP when the return is above +{band:g} sigma",
        f"DOWN when the return is below -{band:g} sigma",
    )
    case_up = "bull_case: one Chinese sentence giving the strongest evidence in the data for UP."
    case_down = (
        "bear_case: one Chinese sentence giving the strongest evidence in the data for DOWN."
    )
    cases = [case_up, case_down] if order == "up_first" else [case_down, case_up]
    return [
        "Task: estimate probabilities for the equal-weight return of the listed perpetual "
        "futures basket over the next 4 hours and over the next 1 hour.",
        f"Outcome classes: {cls_a}, {cls_b}, FLAT otherwise; sigma is given in the data.",
        "Start from the base rates in the data (the prior) and move away from them only as far "
        "as the data justifies.",
        "When the evidence is weak or mixed, stay close to the prior; FLAT is a valid outcome.",
        "Evidence for a rise and evidence for a fall must be judged by the same standard.",
        "Every number is raw data with a unit, not a signal; the notes only explain units.",
        "No news, prior views, lessons or positions are provided on purpose.",
        "Answer with one JSON object only and no other text.",
        f"Keys: {p_a}, p_flat, {p_b}, {h_a}, p1h_flat, {h_b}, median_ret_4h_bp, drivers, "
        f"regime, {c_a}, {c_b}.",
        "Probabilities are decimals between 0.02 and 0.96, and each triple sums to 1.",
        "median_ret_4h_bp: your median estimate of the next 4h basket return in basis points.",
        'drivers: at most 6 items of the form {"field": name, "value": number}, copying the '
        "field name and the shown value exactly from the data.",
        "regime: one neutral Chinese sentence that cites at least two numbers from the data.",
        *cases,
    ]


def render_prompt(dossier: Dossier, order: Order, band: float, cycle_id: str) -> str:
    """数据包 → 提示词。不接任何锚：没有上一轮观点、教训、持仓、卡片倾斜、总盘会文字。"""
    lines = [
        *instruction_lines(order, band),
        "",
        f"Cycle: {cycle_id}",
        f"Run time (as_of): {dossier.t0.astimezone(UTC):%Y-%m-%d %H:%M:%S}Z. Every window ends "
        "at the run time unless its own as_of says otherwise; sigma and base rates come from "
        "closed 1h bars.",
        "Data:",
    ]
    names = {f.name for f in dossier.fields}
    done: set[str] = set()
    up_words = ("_up_", "_buy_", "_long_", "_above_")
    for f in dossier.fields:
        if f.name in done:
            continue
        group = [f]
        if f.mirror == "pair" and f.partner in names:
            partner = dossier.get(f.partner or "")
            if partner is not None:
                first_up = any(w in f"_{f.name}_" for w in up_words)
                pair = [f, partner] if first_up else [partner, f]
                group = pair if order == "up_first" else pair[::-1]
        for g in group:
            done.add(g.name)
            lines.append(
                f"- {g.name} = {g.shown()} [{g.unit}; source {g.source}; as_of "
                f"{g.source_ts.astimezone(UTC):%Y-%m-%d %H:%M:%S}Z] {g.note_for(order)}"
            )
    if dossier.missing:
        lines.append("Unavailable this cycle: " + ", ".join(sorted(dossier.missing)))
    return "\n".join(lines)


# ------------------------------------------------------------------ 解析


@dataclass(frozen=True)
class Probs:
    up: float
    flat: float
    down: float

    def as_dict(self) -> dict[str, float]:
        return {"up": round(self.up, 6), "flat": round(self.flat, 6), "down": round(self.down, 6)}


@dataclass(frozen=True)
class Sample:
    """一次回答。``probs`` 为 None = 不合格（原因在 ``error``）。"""

    index: int
    mirrored: bool
    raw: str = ""
    probs: Probs | None = None
    probs_1h: Probs | None = None
    median_bp: float | None = None
    regime: str = ""
    bull_case: str = ""
    bear_case: str = ""
    drivers_ok: list[dict[str, Any]] = field(default_factory=list)
    drivers_rejected: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    cost_usd: float = 0.0
    cost_reported: bool = False
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "mirrored": self.mirrored,
            "valid": self.probs is not None,
            "probs_4h": self.probs.as_dict() if self.probs else None,
            "probs_1h": self.probs_1h.as_dict() if self.probs_1h else None,
            "median_ret_4h_bp": self.median_bp,
            "regime": self.regime,
            "bull_case": self.bull_case,
            "bear_case": self.bear_case,
            "drivers_ok": self.drivers_ok,
            "drivers_rejected": self.drivers_rejected,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "cost_reported": self.cost_reported,
            "latency_ms": round(self.latency_ms, 1),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "raw": self.raw[:RAW_KEEP],
        }


P_MIN, P_MAX = 0.02, 0.96


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def normalize_probs(up: Any, flat: Any, down: Any) -> Probs:
    """三项概率 → 合法的三项（和为 1，每项夹在 [0.02, 0.96]）。不是数字、为负、和离 1（或 100）
    太远都抛 ``ValueError``：宁可这次回答作废，也不猜。"""
    vals = [_number(v) for v in (up, flat, down)]
    if any(v is None or v < 0 for v in vals):
        raise ValueError("概率不是非负数字")
    xs = [float(v) for v in vals if v is not None]
    total = sum(xs)
    if 90.0 <= total <= 110.0:  # 写成百分数了
        xs = [x / 100.0 for x in xs]
        total = sum(xs)
    if not 0.9 <= total <= 1.1:
        raise ValueError(f"三项概率之和 {total:.3f} 离 1 太远")
    xs = [x / total for x in xs]
    for _ in range(3):  # 夹紧后再归一，几轮就收敛
        xs = [min(max(x, P_MIN), P_MAX) for x in xs]
        s = sum(xs)
        xs = [x / s for x in xs]
    return Probs(*xs)


def extract_json(text: str) -> dict[str, Any]:
    """回答里的 JSON 对象（允许包在 ```json 代码块里、前后有空白）。"""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("回答里没有 JSON 对象") from None
        obj = json.loads(s[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("回答不是 JSON 对象")
    return obj


def check_drivers(raw: Any, dossier: Dossier) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """drivers 必须原样抄自数据包：字段存在，数值等于显示值（容差 = 显示精度的一半）。"""
    ok: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return ok, ([{"item": raw, "why": "drivers 不是列表"}] if raw is not None else [])
    for item in raw[:6]:
        if not isinstance(item, dict):
            bad.append({"item": item, "why": "不是对象"})
            continue
        name, value = item.get("field"), _number(item.get("value"))
        f = dossier.get(str(name)) if isinstance(name, str) else None
        if f is None:
            bad.append({"item": item, "why": "数据包里没有这个字段"})
        elif value is None:
            bad.append({"item": item, "why": "值不是数字"})
        elif abs(value - float(f.shown())) > 0.5 * 10 ** (-f.digits) + 1e-9:
            bad.append({"item": item, "why": f"值和数据包不符（数据包 {f.shown()}）"})
        else:
            ok.append({"field": f.name, "value": value})
    for item in raw[6:]:
        bad.append({"item": item, "why": "超过 6 条"})
    return ok, bad


def _text(v: Any, limit: int = 300) -> str:
    return v.strip()[:limit] if isinstance(v, str) else ""


def parse_sample(reply: ModelReply, index: int, dossier: Dossier, *, mirrored: bool) -> Sample:
    base = {
        "index": index,
        "mirrored": mirrored,
        "raw": reply.text,
        "cost_usd": reply.cost_usd,
        "cost_reported": reply.cost_reported,
        "latency_ms": reply.latency_ms,
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
    }
    try:
        obj = extract_json(reply.text)
        probs = normalize_probs(obj.get("p_up"), obj.get("p_flat"), obj.get("p_down"))
        try:
            probs_1h = normalize_probs(obj.get("p1h_up"), obj.get("p1h_flat"), obj.get("p1h_down"))
        except ValueError as exc:
            raise ValueError(f"1 小时概率不合格：{exc}") from exc
    except (ValueError, TypeError) as exc:
        return Sample(**base, error=str(exc) or type(exc).__name__)  # type: ignore[arg-type]
    ok, bad = check_drivers(obj.get("drivers"), dossier)
    return Sample(
        **base,  # type: ignore[arg-type]
        probs=probs,
        probs_1h=probs_1h,
        median_bp=_number(obj.get("median_ret_4h_bp")),
        regime=_text(obj.get("regime")),
        bull_case=_text(obj.get("bull_case")),
        bear_case=_text(obj.get("bear_case")),
        drivers_ok=ok,
        drivers_rejected=bad,
    )


def average(probs: Sequence[Probs]) -> Probs:
    if not probs:
        raise ValueError("没有可平均的概率")
    up = statistics.fmean(p.up for p in probs)
    flat = statistics.fmean(p.flat for p in probs)
    down = statistics.fmean(p.down for p in probs)
    s = up + flat + down
    return Probs(up / s, flat / s, down / s)


def derive_direction(p: Probs, p_min: float, edge_min: float) -> Direction:
    """方向由代码派生：p_up ≥ θ 且 p_up − p_down ≥ δ 为看涨，看跌对称，其余中性。"""
    if p.up >= p_min and p.up - p.down >= edge_min:
        return "看涨"
    if p.down >= p_min and p.down - p.up >= edge_min:
        return "看跌"
    return "中性"


def compose_regime(sample: Sample | None, avg: Probs, dossier: Dossier, order: Order) -> str:
    """发布给各币会议看的一句话：中立的中文判断（至少两个数）+ 三项概率 + 两边理由。
    涨 / 跌的先后按本轮顺序交替。没有合格的句子就由代码用数据包里的数写一句。"""
    sentence = sample.regime if sample is not None else ""
    if market_table.count_numbers(sentence) < 2:
        r4 = dossier.value("pool_ret_4h_pct")
        s4 = dossier.value("sigma_4h_pct")
        bits = []
        if r4 is not None:
            bits.append(f"等权池近 4 小时 {r4:+.2f}%")
        if s4 is not None:
            bits.append(f"4 小时 σ {s4:.2f}%")
        sentence = "，".join(bits) or "数据不足"
    pu, pd = f"涨 {avg.up:.0%}", f"跌 {avg.down:.0%}"
    a, b = (pu, pd) if order == "up_first" else (pd, pu)
    text = f"{sentence.rstrip('。')}。未来 4 小时概率：{a} / 平 {avg.flat:.0%} / {b}。"
    if sample is not None and sample.bull_case and sample.bear_case:
        bull = f"看涨理由：{sample.bull_case.rstrip('。')}。"
        bear = f"看跌理由：{sample.bear_case.rstrip('。')}。"
        text += bull + bear if order == "up_first" else bear + bull
    return text


# ------------------------------------------------------------------ 4 小时之内（publish_intra）


def intra_block(dossier: Dossier, probs_1h: Probs, order: Order, cycle_id: str) -> dict[str, Any]:
    """一轮的「4 小时之内」：1 小时概率 + 短线现状要用的数（只取数据包里的值，不取模型的话）。
    存进 ``market_desk_state.json`` 的 ``latest.intra``；``as_of`` 是这些数里最旧的那个。"""
    got = {n: f for n in INTRA_FIELDS if (f := dossier.get(n)) is not None}
    as_of = min((f.source_ts for f in got.values()), default=dossier.t0)
    return {
        "cycle_id": cycle_id,
        "t0": dossier.t0.isoformat(),
        "as_of": as_of.isoformat(),
        "order": order,
        "probs_1h": probs_1h.as_dict(),
        "values": {n: f.value for n, f in got.items()},
    }


def intra_status(
    symbols: Sequence[str],
    exclude: Sequence[str],
    rets_15m: Mapping[str, float],
    rets_1h: Mapping[str, float],
) -> dict[str, float]:
    """池（等权平均、涨跌家数，至少 5 个币才给）和 BTC 的 15 分钟 / 1 小时涨跌，字段名同数据包。"""
    basket = [s for s in dict.fromkeys(symbols) if s not in set(exclude)]
    out: dict[str, float] = {}
    for tag, rets in (("15m", rets_15m), ("1h", rets_1h)):
        xs = [rets[s] for s in basket if s in rets]
        if len(xs) >= 5:
            out[f"pool_ret_{tag}_pct"] = statistics.fmean(xs)
            out[f"pool_up_count_{tag}"] = float(sum(x > 0 for x in xs))
            out[f"pool_down_count_{tag}"] = float(sum(x < 0 for x in xs))
        if BTC in rets:
            out[f"btc_ret_{tag}_pct"] = rets[BTC]
    return out


# ------------------------------------------------------------------ 模型


@dataclass(frozen=True)
class ModelReply:
    text: str
    cost_usd: float
    cost_reported: bool
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None


class DeskModel(Protocol):
    async def complete(self, prompt: str) -> ModelReply: ...


class OpenRouterDeskModel:
    """经 OpenRouter 调 DeepSeek V4.1 Flash（和开会同一个模型、同一套请求参数：usage.include 拿真实
    花费、reasoning.effort 限思考长度）。只连 openrouter.ai。"""

    def __init__(
        self,
        key: Callable[[], str | None],
        cfg: MarketDeskSection,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = key
        self._cfg = cfg
        self._transport = transport

    async def complete(self, prompt: str) -> ModelReply:
        key = self._key()
        if not key:
            raise RuntimeError("缺少 OPENROUTER_API_KEY")
        body = {
            "model": self._cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._cfg.max_tokens,
            "response_format": {"type": "json_object"},
            **openrouter_body(self._cfg.reasoning_effort),
        }
        started = time.monotonic()
        async with httpx.AsyncClient(
            timeout=self._cfg.timeout_s, transport=self._transport
        ) as client:
            r = await client.post(
                OPENROUTER_URL, json=body, headers={"Authorization": f"Bearer {key}"}
            )
        ms = (time.monotonic() - started) * 1000
        if r.status_code >= 400:
            raise DeskHTTPError(r.status_code, f"OpenRouter HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        usage = data.get("usage") or {}
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
        cost = _number(usage.get("cost"))
        reported = cost is not None and cost >= 0
        if not reported:
            cost = (
                (tin or len(prompt) / 3) * FLASH_PRICE_PER_MTOK[0]
                + (tout or 0) * FLASH_PRICE_PER_MTOK[1]
            ) / 1e6
        return ModelReply(
            text,
            float(cost or 0.0),
            reported,
            ms,
            tin if isinstance(tin, int) else None,
            tout if isinstance(tout, int) else None,
        )


# ------------------------------------------------------------------ 发布接口（指挥官）


class MarketPublisher(Protocol):
    """``BattleCardCommander`` 提供的那几样：取代数、发布、看当前观点。"""

    latest_market_view: MarketView | None

    def new_market_generation(self) -> int: ...

    def publish_desk_view(
        self,
        *,
        regime: str,
        direction: Direction,
        reason: str,
        pool: SymbolPool,
        generation: int,
        desk_id: str,
        probs: Mapping[str, float],
    ) -> MarketView | None: ...

    def set_market_desk(self, desk: MarketDeskOwner) -> None: ...


class MarketDeskOwner(Protocol):
    def owns_market(self, now: datetime) -> bool: ...


# ------------------------------------------------------------------ 状态


@dataclass
class DeskState:
    """落盘在 ``data/market_desk_state.json``；重启后接着用（超过 max_age 的结果算不可用）。"""

    latest: dict[str, Any] | None = None
    candidate: str | None = None
    candidate_count: int = 0
    candidate_since: datetime | None = None
    candidate_event: bool = False
    last_run_at: datetime | None = None
    last_publish_at: datetime | None = None
    last_publish_view_id: str | None = None
    last_slot: int | None = None
    cost_day: str = ""
    cost_today_usd: float = 0.0
    runs_today: int = 0
    mirror_gaps: list[dict[str, Any]] = field(default_factory=list)
    mirror_flag: bool = False
    stopped: str | None = None
    stopped_day: str = ""
    owner: Literal["desk", "flash"] = "flash"
    owner_since: datetime | None = None
    last_error: str | None = None
    fails: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "latest": self.latest,
            "candidate": self.candidate,
            "candidate_count": self.candidate_count,
            "candidate_since": _iso(self.candidate_since),
            "candidate_event": self.candidate_event,
            "last_run_at": _iso(self.last_run_at),
            "last_publish_at": _iso(self.last_publish_at),
            "last_publish_view_id": self.last_publish_view_id,
            "last_slot": self.last_slot,
            "cost_day": self.cost_day,
            "cost_today_usd": round(self.cost_today_usd, 6),
            "runs_today": self.runs_today,
            "mirror_gaps": self.mirror_gaps[-MIRROR_WINDOW:],
            "mirror_flag": self.mirror_flag,
            "stopped": self.stopped,
            "stopped_day": self.stopped_day,
            "last_error": self.last_error,
            "fails": self.fails,
            "owner": self.owner,
            "owner_since": _iso(self.owner_since),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> DeskState:
        latest = raw.get("latest")
        if latest is not None:
            if not isinstance(latest, dict):
                raise ValueError("latest 不是对象")
            done = datetime.fromisoformat(str(latest["completed_at"]))
            if done.tzinfo is None:
                raise ValueError("时间必须带时区")
        slot = raw.get("last_slot")
        return cls(
            latest=latest,
            candidate=raw.get("candidate"),
            candidate_count=int(raw.get("candidate_count", 0)),
            candidate_since=_parse_at(raw.get("candidate_since")),
            candidate_event=bool(raw.get("candidate_event", False)),
            last_run_at=_parse_at(raw.get("last_run_at")),
            last_publish_at=_parse_at(raw.get("last_publish_at")),
            last_publish_view_id=raw.get("last_publish_view_id"),
            last_slot=int(slot) if slot is not None else None,
            cost_day=str(raw.get("cost_day", "")),
            cost_today_usd=float(raw.get("cost_today_usd", 0.0)),
            runs_today=int(raw.get("runs_today", 0)),
            mirror_gaps=list(raw.get("mirror_gaps", [])),
            mirror_flag=bool(raw.get("mirror_flag", False)),
            stopped=raw.get("stopped"),
            stopped_day=str(raw.get("stopped_day", "")),
            last_error=raw.get("last_error"),
            fails=int(raw.get("fails", 0)),
            owner="desk" if raw.get("owner") == "desk" else "flash",
            owner_since=_parse_at(raw.get("owner_since")),
        )


@dataclass(frozen=True)
class DeskRun:
    """一轮的结果（测试和主循环看）。"""

    cycle_id: str
    status: Literal["ok", "failed", "skipped"]
    direction: Direction | None = None
    probs: Probs | None = None
    published: MarketView | None = None
    cost_usd: float = 0.0
    reason: str = ""


# ------------------------------------------------------------------ 大盘台


class MarketDesk:
    def __init__(
        self,
        cfg: MarketDeskSection,
        publisher: MarketPublisher,
        model: DeskModel,
        log: DecisionLog,
        data_dir: str | Path,
        *,
        builder: DossierBuilder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        day_tz: str = "Asia/Bangkok",
        balance: Callable[[], float | None] | None = None,
        banned: Callable[[], float] = binance_gate.banned_until,
        credit_alarm: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._pub = publisher
        self._model = model
        self._log = log
        self._clock = clock
        self._tz = ZoneInfo(day_tz)
        if balance is None and cfg.mode != "off" and cfg.balance_floor_usd > 0:
            logger.warning(
                "大盘台拿不到 OpenRouter 余额（没有额度看护），余额低线 $%.2f 自停不生效",
                cfg.balance_floor_usd,
            )
        self._balance = balance or (lambda: None)
        self._banned = banned
        self._credit_alarm = credit_alarm
        root = Path(data_dir)
        self._builder = builder or DossierBuilder(
            band_sigma=cfg.band_sigma,
            sigma_lookback_h=cfg.sigma_lookback_h,
            base_rate_days=cfg.base_rate_days,
            exclude_symbols=cfg.exclude_symbols,
            max_input_age_min=cfg.max_input_age_min,
            cache_path=root / LOG_DIR / KLINE_CACHE,
        )
        self._state_path = root / STATE_FILE
        self._log_dir = root / LOG_DIR
        self.state = DeskState()
        self._requested: str | None = None
        self._prices: list[tuple[datetime, float | None, dict[str, float]]] = []
        self._last_move: dict[str, Any] | None = None
        self._running = False
        self._notices: list[str] = []
        self._restore()
        publisher.set_market_desk(self)

    # ---------------------------------------------------------- 给指挥官 / 主循环看
    def available(self, now: datetime) -> bool:
        """最新一轮结果还在 ``max_age_min`` 内。重启后读回来的也算，超时了就不算。"""
        latest = self.state.latest
        if latest is None:
            return False
        done = datetime.fromisoformat(str(latest["completed_at"]))
        return timedelta(0) <= now - done <= timedelta(minutes=self.cfg.max_age_min)

    def owns_market(self, now: datetime) -> bool:
        """此刻大盘方向归大盘台管（总盘会的大盘判断只记录、不发布、不设翻转参照）。

        归属是有状态的、按边沿切换（``check_owner``）：大盘台出一轮合格结果就切到大盘台；
        ``fallback = "flash"`` 时结果超过 ``max_age_min`` 才切回 Flash。切换各记一次。大盘台
        恢复后哪怕接管发布被更晚的发布取代，Flash 也不会在下一轮接着发（不来回抢）。"""
        if self.cfg.mode != "replace":
            return False
        self.check_owner(now)
        return self.state.owner == "desk"

    def check_owner(self, now: datetime) -> None:
        """大盘台不可用了就切回 Flash（只在 fallback = flash 时；边沿触发，记录并提醒一次）。"""
        st = self.state
        if (
            self.cfg.mode != "replace"
            or st.owner != "desk"
            or self.cfg.fallback != "flash"
            or self.available(now)
        ):
            return
        latest = st.latest
        why = (
            f"大盘台最新结果 {latest['completed_at']} 已超过 {self.cfg.max_age_min:g} 分钟"
            if latest is not None
            else "大盘台没有结果"
        ) + (f"（{st.stopped}）" if st.stopped else "")
        self._set_owner(now, "flash", why)

    def _set_owner(self, now: datetime, owner: Literal["desk", "flash"], why: str) -> None:
        st = self.state
        if st.owner == owner:
            return
        before, st.owner, st.owner_since = st.owner, owner, now
        text = (
            f"大盘方向改由 Flash（总盘会）发布：{why}"
            if owner == "flash"
            else f"大盘台接管大盘方向：{why}"
        )
        self._notices.append(text)
        self._record(
            Outcome.INFO,
            text,
            {"owner_change": {"from": before, "to": owner, "reason": why}},
            0.0,
            0.0,
        )
        self._append_log(
            now,
            {
                "event": "owner_change",
                "at": now.isoformat(),
                "from": before,
                "to": owner,
                "reason": why,
            },
        )
        self._save()

    @property
    def _intra_on(self) -> bool:
        return self.cfg.publish_intra or self.cfg.keeper_intra

    def intra_text(self, now: datetime) -> str | None:
        """最近一轮（不是上次发布）的「1 小时概率 + 短线现状」，接在大盘台观点后面给各币会议。
        方向只在滞回确认后才发布，这一段每轮都换。``publish_intra`` 关着、没有大盘台的结果、或
        结果已超过 ``max_age_min``（这时大盘方向也不归它管）返回 None。"""
        return self._intra(now) if self.cfg.publish_intra else None

    def keeper_intra_text(self, now: datetime) -> str | None:
        """同一段给管家复查快照，只看 ``keeper_intra``。"""
        return self._intra(now) if self.cfg.keeper_intra else None

    def _intra(self, now: datetime) -> str | None:
        if not self.available(now):
            return None
        latest = self.state.latest
        block = latest.get("intra") if latest is not None else None
        if not isinstance(block, dict):
            return None
        try:
            return compose_intra(block, now, self.cfg.max_input_age_min)
        except (KeyError, TypeError, ValueError):
            logger.exception("大盘台短线现状读不了，这次不带")
            return None

    def observe_breadth(
        self,
        at: datetime,
        symbols: Sequence[str],
        rets_15m: Mapping[str, float],
        rets_1h: Mapping[str, float],
    ) -> bool:
        """两轮之间刷新「短线现状」：大盘翻转检查每分钟取到的 1 分钟收盘价算出的涨跌（``at`` =
        那根线的收盘时刻），按大盘台同一个篮子（剔除 ``exclude_symbols``）重算池和 BTC 的数，
        写进 ``latest.intra.status``。不发请求、不调模型；离上次刷新不到 ``intra_refresh_s``、
        不比这一轮数据包新、或大盘台还没有结果都不动。返回是否刷新了。"""
        cfg = self.cfg
        latest = self.state.latest
        block = latest.get("intra") if latest is not None else None
        if not self._intra_on or cfg.intra_refresh_s <= 0 or not isinstance(block, dict):
            return False
        status = block.get("status")
        prev = datetime.fromisoformat(str(status["as_of"])) if isinstance(status, dict) else None
        if at <= datetime.fromisoformat(str(block["as_of"])):
            return False
        if prev is not None and (at - prev).total_seconds() < cfg.intra_refresh_s - 1:
            return False
        block["status"] = {
            "as_of": at.isoformat(),
            "values": intra_status(symbols, cfg.exclude_symbols, rets_15m, rets_1h),
        }
        self._save()
        return True

    def request_run(self, reason: str = "regime_flip") -> None:
        """翻转确认 / 急动后要求立即加跑一轮（不占定时的格子；离上一轮开跑不到
        ``min_run_gap_min`` 就等到点）。"""
        if self.cfg.mode != "off":
            self._requested = reason

    def observe_prices(self, now: datetime, btc: float | None, pool: Mapping[str, float]) -> None:
        """主循环每 30 秒喂一次此刻的 BTC 和池内各币价（快环行情，不另发请求）。时间窗内（只算
        上一轮开跑之后）|BTC| 或 |池内涨跌中位数| ≥ ``move_trigger_pct`` 就要求加跑。"""
        pct = self.cfg.move_trigger_pct
        if pct <= 0 or self.cfg.mode == "off":
            return
        window = timedelta(minutes=self.cfg.move_trigger_window_min)
        since = now - window
        if self.state.last_run_at is not None:
            since = max(since, self.state.last_run_at)
        self._prices.append((now, btc, dict(pool)))
        self._prices = [p for p in self._prices if p[0] >= now - window]
        base = next((p for p in self._prices if p[0] >= since), None)
        if base is None or base[0] >= now:
            return
        move = price_move(base[1], base[2], btc, pool)
        if (move is not None and abs(move[0]) >= pct) or (move is not None and abs(move[1]) >= pct):
            self._last_move = {
                "btc_pct": move[0],
                "pool_median_pct": move[1],
                "since": base[0].isoformat(),
                "at": now.isoformat(),
            }
            if self._requested is None:
                self.request_run("move")

    @property
    def running(self) -> bool:
        return self._running

    def take_notice(self) -> str | None:
        return self._notices.pop(0) if self._notices else None

    def slot(self, now: datetime) -> int:
        """当前所在的格子编号（UTC 分钟按 cadence 对齐，偏移 offset）。"""
        minutes = int(now.timestamp() // 60) - self.cfg.offset_min
        return minutes // self.cfg.cadence_min

    def due(self, now: datetime) -> str | None:
        """该不该跑：当前格子还没跑过 → "scheduled"；有加跑请求（翻转 / 急动）且离上一轮开跑够
        ``min_run_gap_min`` → 请求的原因；否则 None。"""
        if self.cfg.mode == "off" or self._running:
            return None
        last = self.state.last_slot
        if last is None or self.slot(now) > last:
            return "scheduled"
        if self._requested is not None:
            prev = self.state.last_run_at
            gap = timedelta(minutes=self.cfg.min_run_gap_min)
            if prev is None or now - prev >= gap:
                return self._requested
        return None

    # ---------------------------------------------------------- 一轮
    async def run(self, pool: SymbolPool | None, reason: str = "scheduled") -> DeskRun:
        """跑一轮。不占 ``_slow_lock`` / ``_refreshing``：开始时取代数，只在发布那一下（同步、不
        让出事件循环）改市场状态。封禁期间跳过（格子不消耗，解封后再跑）。"""
        now = self._clock()
        cycle_id = f"{now.astimezone(UTC):%Y-%m-%dT%H:%MZ}" + (
            "" if reason == "scheduled" else f"-{reason}"
        )
        if self.cfg.mode == "off":
            return DeskRun(cycle_id, "skipped", reason="mode = off")
        if self._running:
            return DeskRun(cycle_id, "skipped", reason="上一轮还在跑")
        if self._banned():
            return DeskRun(cycle_id, "skipped", reason="币安限流中")
        self._consume(now, reason)
        self._roll_day(now)
        if why := self._stop_reason(now):
            self._note_stopped(now, why)
            return DeskRun(cycle_id, "skipped", reason=why)
        if self.state.stopped is not None:
            self.state.stopped = None
        if pool is None or not pool.in_pool:
            self._save()
            return DeskRun(cycle_id, "skipped", reason="还没有候选池")
        self._running = True
        try:
            return await self._run(now, pool, reason, cycle_id)
        finally:
            self._running = False
            self._save()

    def _consume(self, now: datetime, reason: str) -> None:
        """开跑：定时轮占掉当前格子；任何一轮都满足了待办的加跑请求（刚跑过就不用再加跑）。"""
        if reason == "scheduled":
            self.state.last_slot = self.slot(now)
        self._requested = None
        self.state.last_run_at = now
        self._prices = [p for p in self._prices if p[0] >= now]

    def _day(self, now: datetime) -> str:
        return now.astimezone(self._tz).date().isoformat()

    def _roll_day(self, now: datetime) -> None:
        day = self._day(now)
        if self.state.cost_day != day:
            self.state.cost_day, self.state.cost_today_usd, self.state.runs_today = day, 0.0, 0

    def _stop_reason(self, now: datetime) -> str | None:
        limit = self.cfg.daily_cost_limit_usd
        if limit > 0 and self.state.cost_today_usd >= limit:
            return f"大盘台今天已花 ${self.state.cost_today_usd:.3f}，到日上限 ${limit:.2f}"
        floor = self.cfg.balance_floor_usd
        bal = self._balance()
        if floor > 0 and bal is not None and bal < floor:
            return (
                f"OpenRouter 余额 ${bal:.2f} 低于大盘台低线 ${floor:.2f}，自停，余额留给开会和 Jev"
            )
        return None

    def _note_stopped(self, now: datetime, why: str) -> None:
        day = self._day(now)
        first = self.state.stopped_day != day or self.state.stopped != why
        self.state.stopped, self.state.stopped_day = why, day
        if first:
            self._notices.append(why)
            self._record(Outcome.INFO, f"大盘台自停：{why}", {"stopped": why}, 0.0, 0.0)
        self._save()

    async def _run(self, now: datetime, pool: SymbolPool, reason: str, cycle_id: str) -> DeskRun:
        started = time.monotonic()
        generation = self._pub.new_market_generation()
        order = cycle_order(cycle_id)
        mirror = mirror_selected(cycle_id, self.cfg.mirror_test_rate)
        row: dict[str, Any] = {
            "cycle_id": cycle_id,
            "t0": now.isoformat(),
            "reason": reason,
            "generation": generation,
            "settings": self.cfg.model_dump(mode="json"),
            "settings_sha256": _sha(self.cfg.model_dump(mode="json")),
            "prompt_version": PROMPT_VERSION,
            "order": order,
            "mirror_test": mirror,
            "move_trigger": self._last_move if reason == "move" else None,
        }
        cost = 0.0
        try:
            dossier = await asyncio.to_thread(self._builder.build, pool, now)
            row["dossier"] = dossier.to_json()
            row["dossier_sha256"] = dossier.sha256()
            prompt = render_prompt(dossier, order, self.cfg.band_sigma, cycle_id)
            row["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            jobs = [self._ask(prompt, i, dossier, mirrored=False) for i in range(self.cfg.samples)]
            mirrored = dossier.mirror() if mirror else None
            if mirrored is not None:
                mprompt = render_prompt(mirrored, order, self.cfg.band_sigma, cycle_id)
                row["mirror_dossier_sha256"] = mirrored.sha256()
                jobs += [
                    self._ask(mprompt, i, mirrored, mirrored=True) for i in range(self.cfg.samples)
                ]
            samples = await asyncio.gather(*jobs)
            cost = sum(s.cost_usd for s in samples)
            main = [s for s in samples if not s.mirrored]
            row["samples"] = [s.to_json() for s in main]
            valid = [s for s in main if s.probs is not None]
            if len(valid) < self.cfg.min_valid_samples:
                raise DeskError(
                    f"合格回答只有 {len(valid)}/{self.cfg.samples} 次（至少 "
                    f"{self.cfg.min_valid_samples}）："
                    + "；".join({s.error for s in main if s.error})
                )
            avg = average([s.probs for s in valid if s.probs is not None])
            avg_1h = average([s.probs_1h for s in valid if s.probs_1h is not None])
            row["score_targets"] = score_targets(now, dossier, self.cfg.band_sigma)
            direction = derive_direction(avg, self.cfg.call_p_min, self.cfg.call_edge_min)
            if mirrored is not None:
                row["mirror"] = self._mirror(cycle_id, now, avg, samples)
            pick = min(
                (s for s in valid if market_table.count_numbers(s.regime) >= 2),
                key=lambda s: _l1(s.probs, avg),
                default=None,
            )
            regime = compose_regime(pick, avg, dossier, order)
            if not dossier.has_prior:
                # 没有先验（重启后长历史还在回填）：只记录，不发布、不接管、不当成「可用」结果
                self.state.fails, self.state.last_error = 0, None
                row.update(
                    status="ok",
                    prior_missing=True,
                    probs_4h=avg.as_dict(),
                    probs_1h=avg_1h.as_dict(),
                    direction=direction,
                    regime=regime,
                    published_view=None,
                )
                return DeskRun(cycle_id, "ok", direction, avg, None, cost, "prior_missing")
            desk_id = uuid.uuid4().hex
            completed = self._clock()
            self.state.latest = {
                "desk_id": desk_id,
                "cycle_id": cycle_id,
                "reason": reason,
                "t0": now.isoformat(),
                "completed_at": completed.isoformat(),
                "probs_4h": avg.as_dict(),
                "probs_1h": avg_1h.as_dict() if avg_1h else None,
                "direction": direction,
                "regime": regime,
                "valid_samples": len(valid),
                "samples": self.cfg.samples,
                "sigma_4h_pct": dossier.sigma_4h_pct,
                "dossier_sha256": dossier.sha256(),
                "published_view_id": None,
            }
            if self._intra_on:
                intra = {
                    **intra_block(dossier, avg_1h, order, cycle_id),
                    "max_age_min": self.cfg.max_input_age_min,
                    "panel": self.cfg.publish_intra,
                }
                self.state.latest["intra"] = intra
                row["intra"] = copy.deepcopy(intra)  # 之后的刷新改的是状态里那份，日志不跟着变
            if self.cfg.mode == "replace":
                self._set_owner(completed, "desk", f"第 {cycle_id} 轮结果合格")
            published = self._maybe_publish(
                now, completed, direction, regime, reason, pool, generation, desk_id, avg
            )
            self.state.fails, self.state.last_error = 0, None
            row.update(
                status="ok",
                probs_4h=avg.as_dict(),
                probs_1h=avg_1h.as_dict() if avg_1h else None,
                direction=direction,
                regime=regime,
                desk_id=desk_id,
                hysteresis={
                    "candidate": self.state.candidate,
                    "count": self.state.candidate_count,
                    "confirm": self.cfg.publish_confirm,
                },
                published_view=published.model_dump(mode="json") if published else None,
            )
            return DeskRun(cycle_id, "ok", direction, avg, published, cost)
        except binance_gate.RateLimited as exc:
            self.state.last_slot = None if reason == "scheduled" else self.state.last_slot
            row.update(status="skipped", error=f"币安限流：{exc}")
            return DeskRun(cycle_id, "skipped", cost_usd=cost, reason="币安限流")
        except Exception as exc:
            why = str(exc) or type(exc).__name__
            self.state.fails += 1
            self.state.last_error = why
            row.update(status="failed", error=why)
            logger.warning("大盘台这一轮失败：%s", why)
            return DeskRun(cycle_id, "failed", cost_usd=cost, reason=why)
        finally:
            ms = (time.monotonic() - started) * 1000
            self.state.cost_today_usd += cost
            self.state.runs_today += 1
            row.update(cost_usd=round(cost, 6), latency_ms=round(ms, 1))
            self._append_log(now, row)
            if row.get("status") != "skipped" or cost > 0:
                self._record_run(row, cost, ms)

    async def _ask(self, prompt: str, index: int, dossier: Dossier, *, mirrored: bool) -> Sample:
        try:
            reply = await asyncio.wait_for(self._model.complete(prompt), self.cfg.timeout_s + 5)
        except Exception as exc:
            if is_credit_error(exc) and self._credit_alarm is not None:
                try:
                    self._credit_alarm()  # 和开会撞 402 一样：额度看护立即告警、面板横幅
                except Exception:
                    logger.exception("大盘台通知额度不足失败")
            est = len(prompt) / 3 * FLASH_PRICE_PER_MTOK[0] / 1e6  # 失败也可能计费：按输入估
            return Sample(
                index, mirrored, error=f"调用失败：{str(exc) or type(exc).__name__}", cost_usd=est
            )
        return parse_sample(reply, index, dossier, mirrored=mirrored)

    def _mirror(
        self, cycle_id: str, now: datetime, avg: Probs, samples: Sequence[Sample]
    ) -> dict[str, Any]:
        """镜像测试：数据包涨跌取反后的平均概率，和原来比。p_up(原) 应该≈ p_down(镜像)。"""
        ms = [s for s in samples if s.mirrored]
        mv = [s.probs for s in ms if s.probs is not None]
        out: dict[str, Any] = {"samples": [s.to_json() for s in ms], "valid": len(mv)}
        if len(mv) < self.cfg.min_valid_samples:
            out["error"] = "镜像合格回答不够，这次不计"
            return out
        m = average(mv)
        gap = abs(avg.up - m.down)
        gap_rev = abs(avg.down - m.up)
        out.update(probs_4h=m.as_dict(), gap_up_vs_mirror_down=gap, gap_down_vs_mirror_up=gap_rev)
        self.state.mirror_gaps = [
            *self.state.mirror_gaps,
            {"cycle_id": cycle_id, "at": now.isoformat(), "gap": gap, "gap_rev": gap_rev},
        ][-MIRROR_WINDOW:]
        flag, mean = mirror_bias(self.state.mirror_gaps, self.cfg.mirror_alert_gap)
        if flag and not self.state.mirror_flag:
            self._notices.append(
                f"大盘台镜像测试：最近 {len(self.state.mirror_gaps)} 次 |p_up(原) − p_down(镜像)| "
                f"平均 {mean:.3f}，超过 {self.cfg.mirror_alert_gap:.2f}，有方向偏置"
            )
        self.state.mirror_flag = flag
        out.update(mirror_mean_gap=mean, mirror_flag=flag)
        return out

    def _maybe_publish(
        self,
        now: datetime,
        completed: datetime,
        direction: Direction,
        regime: str,
        reason: str,
        pool: SymbolPool,
        generation: int,
        desk_id: str,
        avg: Probs,
    ) -> MarketView | None:
        """滞回：大盘台第一次接管立即发布；之后新方向要连续 ``publish_confirm`` 轮一致，并且从第一次
        出现起持续 ``publish_confirm_min`` 分钟（这几轮里有急动 / 翻转加跑的轮时只看轮数），还要离
        上次发布够 ``publish_min_gap_min``。方向没变不发（每发一版所有持仓都要排队复核）。"""
        if self.cfg.mode != "replace":
            return None
        view = self._pub.latest_market_view
        # 当前观点不是大盘台发的（第一次接管、Flash 回退期间发过、或上次接管被取代）：立即发布
        takeover = view is None or view.source != DESK_SOURCE
        st = self.state
        if not takeover and view is not None and direction == view.regime_direction:
            st.candidate, st.candidate_count = None, 0
            st.candidate_since, st.candidate_event = None, False
            return None
        if not takeover:
            event = reason != "scheduled"
            if direction == st.candidate and st.candidate_since is not None:
                st.candidate_count += 1
                st.candidate_event = st.candidate_event or event
            else:
                st.candidate, st.candidate_count = direction, 1
                st.candidate_since, st.candidate_event = now, event
            if st.candidate_count < self.cfg.publish_confirm:
                return None
            # 确认的几轮要在时间上分开：加跑紧挨着定时轮（:16 加跑、:17 定时）不算两次独立确认
            lasted = now - st.candidate_since
            spread = timedelta(minutes=max(self.cfg.publish_confirm_min / 2, self.cfg.cadence_min))
            need = (
                spread
                if st.candidate_event
                else max(spread, timedelta(minutes=self.cfg.publish_confirm_min))
            )
            if lasted < need:
                return None
            gap = timedelta(minutes=self.cfg.publish_min_gap_min)
            if st.last_publish_at is not None and completed - st.last_publish_at < gap:
                return None
        published = self._pub.publish_desk_view(
            regime=regime,
            direction=direction,
            reason=DESK_SOURCE if reason == "scheduled" else reason,
            pool=pool,
            generation=generation,
            desk_id=desk_id,
            probs=avg.as_dict(),
        )
        if published is None:  # 已被更新的发布取代
            return None
        st.candidate, st.candidate_count = None, 0
        st.candidate_since, st.candidate_event = None, False
        st.last_publish_at = completed
        st.last_publish_view_id = published.market_view_id
        if st.latest is not None:
            st.latest["published_view_id"] = published.market_view_id
            st.latest["published_version"] = published.version
        return published

    # ---------------------------------------------------------- 记录
    def _record(
        self, outcome: Outcome, summary: str, out: dict[str, Any], cost: float, ms: float
    ) -> None:
        try:
            self._log.write(
                DecisionRecord(
                    record_id=uuid.uuid4().hex,
                    ts=self._clock(),
                    role=Role.COMMANDER,
                    symbol=None,
                    outcome=outcome,
                    summary=summary[:200],
                    output={DESK_SOURCE: out},
                    latency_ms=ms,
                    cost_usd=max(cost, 0.0),
                    model_version=self.cfg.model,
                )
            )
        except Exception:
            logger.exception("写大盘台决策记录失败")

    def _record_run(self, row: Mapping[str, Any], cost: float, ms: float) -> None:
        status = row.get("status")
        if status == "ok":
            p = row.get("probs_4h") or {}
            pub = row.get("published_view")
            summary = (
                f"大盘台：{row.get('direction')}（涨 {p.get('up', 0):.0%} / 平 "
                f"{p.get('flat', 0):.0%} / 跌 {p.get('down', 0):.0%}）"
                + (
                    f"，发布大盘 v{pub['version']}"
                    if pub
                    else "，没有先验（长历史回填中），只记录"
                    if row.get("prior_missing")
                    else "，方向未发布"
                )
            )
            outcome = Outcome.ACTION if pub else Outcome.INFO
        else:
            summary = (
                f"大盘台这一轮{'失败' if status == 'failed' else '跳过'}：{row.get('error', '')}"
            )
            outcome = Outcome.ERROR if status == "failed" else Outcome.INFO
        out = {
            k: row.get(k)
            for k in (
                "cycle_id",
                "status",
                "reason",
                "direction",
                "probs_4h",
                "probs_1h",
                "desk_id",
                "order",
                "mirror_test",
                "prompt_version",
                "dossier_sha256",
                "prior_missing",
                "error",
            )
        }
        mirror = row.get("mirror")
        if isinstance(mirror, dict):
            out["mirror_mean_gap"] = mirror.get("mirror_mean_gap")
            out["mirror_flag"] = mirror.get("mirror_flag")
        pub = row.get("published_view")
        out["published_view_id"] = pub.get("market_view_id") if isinstance(pub, dict) else None
        out["cost_today_usd"] = round(self.state.cost_today_usd, 6)
        self._record(outcome, summary, out, cost, ms)

    def _append_log(self, now: datetime, row: Mapping[str, Any]) -> None:
        path = self._log_dir / f"{now.astimezone(UTC):%Y-%m-%d}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.exception("写大盘台日志失败")

    def _save(self) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.state.to_json(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except Exception:
            logger.exception("大盘台状态存盘失败")
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _restore(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.state = DeskState.from_json(raw)
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("大盘台状态读不了，从空状态开始：%s", exc)
            self.state = DeskState()

    def status(self, now: datetime) -> dict[str, Any]:
        """给面板 / 心跳用的一行状态。"""
        return {
            "mode": self.cfg.mode,
            "available": self.available(now),
            "owns_market": self.owns_market(now),
            "owner": self.state.owner,
            "latest": self.state.latest,
            "stopped": self.state.stopped,
            "last_error": self.state.last_error,
            "cost_today_usd": self.state.cost_today_usd,
            "mirror_flag": self.state.mirror_flag,
        }


class DeskError(RuntimeError):
    pass


class DeskHTTPError(RuntimeError):
    """OpenRouter 返回错误状态；带 ``status_code``，402（额度不足）由 ``is_credit_error`` 认出。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def score_targets(t0: datetime, dossier: Dossier, band: float) -> dict[str, Any]:
    """记分要的东西（记分器 D3 再做）：每轮按开跑时刻 t0 记 1h 和 4h 两个结算点，各带当时的 σ 和
    「平」的宽度，以及等权池成员。结算价取 t0 / t0 + h 的已收盘 1 分钟线（和设计 §1.1 一致）。"""
    out: dict[str, Any] = {"t0": t0.isoformat(), "basket": list(dossier.symbols)}
    for h in (1, 4):
        sigma = dossier.value(f"sigma_{h}h_pct")
        out[f"{h}h"] = {
            "resolve_at": (t0 + timedelta(hours=h)).isoformat(),
            "sigma_pct": sigma,
            "flat_band_pct": band * sigma if sigma is not None else None,
            "base_rate": {k: dossier.value(f"base_rate_{k}_{h}h") for k in ("up", "flat", "down")},
        }
    return out


def price_move(
    btc0: float | None,
    pool0: Mapping[str, float],
    btc1: float | None,
    pool1: Mapping[str, float],
) -> tuple[float, float] | None:
    """两个时刻之间 BTC 涨跌（%）和池内各币涨跌的中位数（%）；缺的一边记 0。都算不出返回 None。"""
    btc = (btc1 / btc0 - 1) * 100 if btc0 and btc1 else None
    rets = [(pool1[s] / p - 1) * 100 for s, p in pool0.items() if p > 0 and pool1.get(s, 0) > 0]
    med = statistics.median(rets) if len(rets) >= 5 else None
    if btc is None and med is None:
        return None
    return (btc or 0.0, med or 0.0)


def mirror_bias(gaps: Sequence[Mapping[str, Any]], threshold: float) -> tuple[bool, float]:
    """最近几次镜像测试 |p_up(原) − p_down(镜像)| 的平均；次数够且超过门槛 → 偏置。"""
    vals = [float(g["gap"]) for g in gaps[-MIRROR_WINDOW:] if isinstance(g.get("gap"), int | float)]
    if not vals:
        return False, 0.0
    mean = statistics.fmean(vals)
    return len(vals) >= MIRROR_MIN_TESTS and mean > threshold, mean


def _l1(p: Probs | None, q: Probs) -> float:
    if p is None:
        return math.inf
    return abs(p.up - q.up) + abs(p.flat - q.flat) + abs(p.down - q.down)


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at is not None else None


def _parse_at(raw: Any) -> datetime | None:
    if raw is None:
        return None
    at = datetime.fromisoformat(str(raw))
    if at.tzinfo is None:
        raise ValueError("时间必须带时区")
    return at


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()[:16]
