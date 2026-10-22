"""规则版拍板人（M0 用；M1 拿它和 Jev 做对比）。

和 Jev 走同一个 ``DecisionModel`` 接口、读同一份快照，只是用几条固定公式代替模型。
它是独立的一个“模型版本”（``rules-v1``），由配置选择，**不是** Jev 出错时的兜底：
Jev 出错照样故障即关闭。快照解析不了也返回 ``ok=False``。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from jiaoyi.contracts import JevAnswer, JevRequest, JevResponse

MODEL_VERSION = "rules-v1"


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _two(p_first: float, a: str, b: str) -> dict[str, float]:
    p = min(max(p_first, 0.01), 0.99)
    return {a: p, b: 1.0 - p}


def _three(scores: dict[str, float]) -> dict[str, float]:
    m = max(scores.values())
    ex = {k: math.exp(v - m) for k, v in scores.items()}
    total = sum(ex.values())
    return {k: v / total for k, v in ex.items()}


class RuleDecisionModel:
    async def ask(self, request: JevRequest) -> JevResponse:
        started = time.perf_counter()
        try:
            d: dict[str, Any] = json.loads(request.snapshot)
            probs = self._entry(d) if request.kind == "entry" else self._hold(d)
            answers = [JevAnswer(id=q.id, probs=probs[q.id]) for q in request.questions]
        except (ValueError, KeyError, TypeError) as e:
            return JevResponse(
                request_id=request.request_id,
                ok=False,
                error=f"规则版拍板人读不懂快照：{e}",
                model_version=MODEL_VERSION,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        return JevResponse(
            request_id=request.request_id,
            ok=True,
            model_version=MODEL_VERSION,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            answers=answers,
        )

    @staticmethod
    def _flow(d: dict[str, Any]) -> float:
        """主动成交 + 盘口失衡，正数偏多，大致在 [-3, 3]。"""
        book = d.get("book") or {}
        return (
            4.0 * (float(d.get("buy", 0.5)) - 0.5)
            + 1.5 * float(book.get("imb", 0.0))
            + (1.0 if float(d.get("cvd30_k", 0.0)) > 0 else -1.0)
        )

    def _entry(self, d: dict[str, Any]) -> dict[str, dict[str, float]]:
        sign = 1.0 if d["side"] == "long" else -1.0
        flow = sign * self._flow(d)
        vol_x = float(d.get("vol_x", 0.0))
        spread = float((d.get("book") or {}).get("spread_bps", 99.0))
        p_real = _sig(0.6 * flow + 0.3 * (vol_x - 2.0))
        p_toxic = _sig(0.4 * (spread - 3.0) - 0.5 * flow)
        mom = sign * (float(d.get("ret1m_bps", 0.0)) / 20.0)
        up = mom + 0.3 * flow if sign > 0 else -(mom + 0.3 * flow)
        direction = _three({"up": up, "down": -up, "flat": 0.3})
        p_take = _sig(2.5 * (p_real - 0.5) - 2.0 * (p_toxic - 0.5) + 0.5 * flow)
        rv, atr = float(d.get("rv5m_bps", 0.0)), float(d.get("atr_bps", 0.0)) or 1.0
        noise = rv * math.sqrt(30) / atr  # 5 分钟波动相对 ATR
        stop = _three({"narrow": 1.0 - noise, "mid": 0.5, "wide": noise - 1.0})
        conv = p_take - 0.5
        size = _three({"S": -conv * 4, "M": 0.5, "L": conv * 4 - 0.5})
        return {
            "genuine": _two(p_real, "real", "fake"),
            "direction": direction,
            "toxicity": _two(1.0 - p_toxic, "low", "high"),
            "take": _two(p_take, "take", "skip"),
            "stop_width": stop,
            "size": size,
        }

    def _hold(self, d: dict[str, Any]) -> dict[str, dict[str, float]]:
        pos = d["pos"]
        sign = 1.0 if pos["side"] == "long" else -1.0
        flow = sign * self._flow(d)
        card = d.get("card") or {}
        against_card = card.get("bias") not in (None, pos["side"], "both")
        pnl = float(pos.get("pnl_bps", 0.0))
        to_stop = pos.get("to_stop_bps")
        near_stop = to_stop is not None and float(to_stop) < 0.25 * float(d.get("atr_bps", 1.0))
        p_intact = _sig(0.8 * flow + pnl / 50.0 - (2.0 if against_card else 0.0))
        p_adverse = _sig(-0.8 * flow + (1.0 if near_stop else 0.0))
        p_close = _sig(3.0 * (p_adverse - 0.5) - 3.0 * (p_intact - 0.5) - 0.5)
        return {
            "thesis_intact": _two(p_intact, "yes", "no"),
            "adverse_risk": _two(1.0 - p_adverse, "low", "high"),
            "action": _two(1.0 - p_close, "hold", "close"),
        }
