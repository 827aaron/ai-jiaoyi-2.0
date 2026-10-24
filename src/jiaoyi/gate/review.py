"""M1 之后复盘 Jev：把决策日志里每次 Jev 调用摊平成一行，按模板版本 / 模型版本分组看。

只读 ``data/decision_log/*.jsonl``，不连网络。拍板人（gate）和持仓管家（keeper）的记录
都带 ``input.jev`` / ``output.jev``（见 ``gate.questions.jev_log_fields``）；没有这两个键的
旧记录（模板版本化之前）跳过。

q5（管家方案 E，2026-10-24）只改了持仓题目和持仓快照，进场题目和进场快照和 q4 一字不差：
按版本分组校准拍板门槛时，q4 和 q5 的进场（gate）记录可以合在一起算。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiaoyi.contracts import DecisionRecord, Role

_JEV_ROLES = (Role.GATE, Role.KEEPER)


@dataclass(frozen=True)
class JevCall:
    record: DecisionRecord
    template: str
    template_fp: str
    kind: str | None
    ok: bool
    model: str
    latency_ms: float
    answers: dict[str, dict[str, float]]

    def p(self, question: str, choice: str) -> float | None:
        return self.answers.get(question, {}).get(choice)


def iter_jev_calls(data_dir: str | Path) -> Iterator[JevCall]:
    """按时间顺序产出真正调用过决策模型的记录（被作战卡等拦下、没调用的不算）。"""
    log_dir = Path(data_dir) / "decision_log"
    for f in sorted(log_dir.glob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = DecisionRecord.model_validate_json(line)
                if rec.role not in _JEV_ROLES:
                    continue
                inp: Any = rec.input.get("jev")
                out: Any = rec.output.get("jev")
                if not isinstance(inp, dict) or not isinstance(out, dict) or not inp.get("called"):
                    continue
                yield JevCall(
                    record=rec,
                    template=str(inp.get("template")),
                    template_fp=str(inp.get("template_fp")),
                    kind=inp.get("kind"),
                    ok=bool(out.get("ok")),
                    model=str(out.get("model")),
                    latency_ms=float(out.get("latency_ms") or 0.0),
                    answers=dict(out.get("answers") or {}),
                )
