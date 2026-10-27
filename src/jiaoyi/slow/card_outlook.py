"""作战卡上「只记录」的结构化判断（``CardOutlook``）：整理、配上总盘会方向、从记录里读回。

卡的方向混着「大盘往哪走」和「这个币比大盘强还是弱」两个判断，复盘时分不清错在哪一个
（card-direction-accuracy-2026-10-28：判错的卡 86% 同期等权池反向走）。这里把会议的两个判断、
研究经理 / 组合经理 / 交易员的评级和观望卡的倾向结构化记下来。

只记录，不参与任何决定：模型没写或写错只记 None 和原因，卡照旧出；方向、仓位、信号都不看它。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Literal, cast

from jiaoyi.contracts import Bias, CardOutlook, CardSymbol, MarketView
from jiaoyi.contracts.battle_card import MarketDir, Rating, RelDir

logger = logging.getLogger(__name__)

MARKET_DIRS: tuple[MarketDir, ...] = ("up", "down", "flat")
REL_DIRS: tuple[RelDir, ...] = ("outperform", "underperform", "inline")
RATINGS: tuple[Rating, ...] = ("buy", "overweight", "hold", "underweight", "sell")
ACTIONS = ("buy", "hold", "sell")
BOARD_DIR: dict[str, MarketDir] = {"看涨": "up", "看跌": "down", "中性": "flat"}
EVIDENCE_MAX = 300
HORIZON_MIN = 240  # 和 ta_prompts.OUTLOOK_NOTE 里的「4 hours」、复盘打分的 4 小时一致
_SIGN = {"buy": 1, "overweight": 1, "hold": 0, "underweight": -1, "sell": -1}


def choice(raw: object, allowed: Iterable[str]) -> tuple[str | None, str | None]:
    """模型写的值 → (认得的值, 认不得时的原因)。大小写、首尾空白不计较，别的一律不猜。"""
    if raw is None or not str(raw).strip():
        return None, "omitted"
    text = str(raw).strip()
    if text.lower() in allowed:
        return text.lower(), None
    return None, f"invalid: {text[:40]!r}"


def evidence(raw: object) -> tuple[str | None, str | None]:
    if raw is None or not str(raw).strip():
        return None, "omitted"
    return str(raw).strip()[:EVIDENCE_MAX], None


def no_trade_lean(
    bias: Bias, rm: str | None, pm: str | None, trader: str | None
) -> tuple[Literal["long", "short"] | None, str | None]:
    """观望卡里会议已表达的倾向：三人里非观望的评级方向一致才记。方向卡返回 (None, None)。"""
    if bias is not Bias.NO_TRADE:
        return None, None
    signs = {_SIGN[r] for r in (rm, pm, trader) if r in _SIGN} - {0}
    if not signs:
        return None, "no_lean"
    if len(signs) > 1:
        return None, "conflict"
    return ("long" if signs.pop() > 0 else "short"), None


def build_outlook(
    *,
    bias: Bias,
    market_dir: object,
    market_dir_evidence: object,
    rel_dir: object,
    rel_dir_evidence: object,
    rm: object,
    pm: object,
    trader: object,
) -> CardOutlook:
    """会议的原始输出 → ``CardOutlook``。任何一项不合格只记 None 和原因，从不抛错。"""
    missing: dict[str, str] = {}

    def keep(name: str, pair: tuple[str | None, str | None]) -> str | None:
        value, why = pair
        if why is not None:
            missing[name] = why
        return value

    md = keep("market_dir", choice(market_dir, MARKET_DIRS))
    md_ev = keep("market_dir_evidence", evidence(market_dir_evidence))
    rd = keep("rel_dir", choice(rel_dir, REL_DIRS))
    rd_ev = keep("rel_dir_evidence", evidence(rel_dir_evidence))
    rm_r = keep("rm_rating", _rating(rm, RATINGS))
    pm_r = keep("pm_rating", _rating(pm, RATINGS))
    tr = keep("trader_action", _rating(trader, ACTIONS))
    lean = keep("no_trade_lean", no_trade_lean(bias, rm_r, pm_r, tr))
    return CardOutlook(
        horizon_min=HORIZON_MIN,
        market_dir=cast("MarketDir | None", md),
        market_dir_evidence=md_ev,
        rel_dir=cast("RelDir | None", rd),
        rel_dir_evidence=rd_ev,
        rm_rating=cast("Rating | None", rm_r),
        pm_rating=cast("Rating | None", pm_r),
        trader_action=cast("Literal['buy', 'hold', 'sell'] | None", tr),
        no_trade_lean=cast("Literal['long', 'short'] | None", lean),
        missing=missing,
    )


def _rating(raw: object, allowed: Iterable[str]) -> tuple[str | None, str | None]:
    value, why = choice(raw, allowed)
    return value, (None if why is None else "unparsed" if why == "omitted" else why)


def with_board(cs: CardSymbol, market: MarketView | None) -> CardSymbol:
    """给已有的 outlook 配上研究启动时总盘会的方向（同一套 up/down/flat）；没有 outlook 不动。
    出任何错原样返回，不能让开会 / 盖章失败。"""
    if cs.outlook is None:
        return cs
    try:
        missing = {k: v for k, v in cs.outlook.missing.items() if k != "board_market_dir"}
        if market is None:
            missing["board_market_dir"] = "no_market_view"
            update: dict[str, object] = {
                "board_market_dir": None,
                "board_regime_direction": None,
                "board_market_view_id": None,
            }
        else:
            update = {
                "board_market_dir": BOARD_DIR[market.regime_direction],
                "board_regime_direction": market.regime_direction,
                "board_market_view_id": market.market_view_id,
            }
        outlook = cs.outlook.model_copy(update={**update, "missing": missing})
        return cs.model_copy(update={"outlook": outlook})
    except Exception:
        logger.exception("%s 配总盘会方向失败，outlook 照旧", cs.symbol)
        return cs


def card_from_output(data: Mapping[str, Any]) -> CardSymbol:
    """记录里的指令 → ``CardSymbol``：先不带 outlook 校验卡（不合格照常抛 ValueError），
    outlook 再单独校验，坏了当 None。只记录的数据永远不能让面板丢掉这张卡。"""
    cs = CardSymbol.model_validate({k: v for k, v in data.items() if k != "outlook"})
    raw = data.get("outlook")
    if raw is None:
        return cs
    try:
        outlook = CardOutlook.model_validate(raw)
    except (ValueError, TypeError):
        return cs
    return cs.model_copy(update={"outlook": outlook})
