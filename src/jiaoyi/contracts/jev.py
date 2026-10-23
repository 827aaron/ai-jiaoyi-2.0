"""拍板人 / 持仓管家与快速决策模型（Jev，以后可换本地模型）之间的请求和回复。

- 开仓一次问 6 个问题（``EntryQuestion``），盯仓一次问 3 个问题（``HoldQuestion``）。
- 每个问题的选项固定（``ENTRY_CHOICES`` / ``HOLD_CHOICES``），模型返回每个选项的概率。
- 问题的具体措辞（目标、时间、该看哪些输入）由快环工程师写在 gate 模块里，
  契约只固定问题编号和选项，换模型不用改下游。
- 故障即关闭：超时、报错、解析失败 → 开仓一律不做，盯仓维持现状。

JSON 示例见 ``contracts/examples/jev_entry_request.json`` 等。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from jiaoyi.contracts.common import Contract, NonNegFloat, Probability, Symbol


class EntryQuestion(StrEnum):
    """开仓 6 问（设计文档 §二 拍板人）。"""

    GENUINE = "genuine"
    """信号是真是假。"""
    DIRECTION = "direction"
    """接下来往哪个方向走。"""
    TOXICITY = "toxicity"
    """订单流毒性：对手是不是知情资金 / 会不会被收割。"""
    TAKE = "take"
    """做不做。"""
    STOP_WIDTH = "stop_width"
    """止损放窄、中、宽。"""
    SIZE = "size"
    """仓位小、中、大。"""


class HoldQuestion(StrEnum):
    """盯仓 3 问（持仓管家）。"""

    THESIS_INTACT = "thesis_intact"
    """开仓理由还成立吗。"""
    ADVERSE_RISK = "adverse_risk"
    """接下来几分钟出现不利大波动的风险。"""
    ACTION = "action"
    """拿住还是全平（v1 只有这两种，不能加仓、不能放宽止损）。"""


ENTRY_CHOICES: dict[EntryQuestion, tuple[str, ...]] = {
    EntryQuestion.GENUINE: ("real", "fake"),
    EntryQuestion.DIRECTION: ("up", "down", "flat"),
    EntryQuestion.TOXICITY: ("low", "high"),
    EntryQuestion.TAKE: ("take", "skip"),
    EntryQuestion.STOP_WIDTH: ("narrow", "mid", "wide"),
    EntryQuestion.SIZE: ("S", "M", "L"),
}

HOLD_CHOICES: dict[HoldQuestion, tuple[str, ...]] = {
    HoldQuestion.THESIS_INTACT: ("yes", "no"),
    HoldQuestion.ADVERSE_RISK: ("low", "high"),
    HoldQuestion.ACTION: ("hold", "close"),
}


class JevQuestion(Contract):
    id: str = Field(min_length=1, description="EntryQuestion 或 HoldQuestion 的值。")
    prompt: str = Field(min_length=1, description="给模型的问题说明（目标、时间、看哪些输入）。")
    choices: tuple[str, ...] = Field(min_length=2)


class JevRequest(Contract):
    """一次调用。``snapshot`` 是压缩后的状态文本，控制在约 400 token 以内。"""

    request_id: str = Field(min_length=1)
    kind: Literal["entry", "hold"]
    symbol: Symbol
    ts: AwareDatetime
    signal_id: str | None = None
    position_id: str | None = None
    snapshot: str = Field(min_length=1, max_length=2400)
    questions: list[JevQuestion] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_questions(self) -> JevRequest:
        expected: dict[str, tuple[str, ...]] = (
            {q.value: c for q, c in ENTRY_CHOICES.items()}
            if self.kind == "entry"
            else {q.value: c for q, c in HOLD_CHOICES.items()}
        )
        got = {q.id: q.choices for q in self.questions}
        if got != expected:
            raise ValueError(f"{self.kind} 请求的问题和选项必须正好是 {expected}")
        if self.kind == "entry" and not self.signal_id:
            raise ValueError("开仓请求必须带 signal_id")
        if self.kind == "hold" and not self.position_id:
            raise ValueError("盯仓请求必须带 position_id")
        return self


class JevAnswer(Contract):
    """一个问题的回答：每个选项的概率，总和约为 1。"""

    id: str = Field(min_length=1)
    probs: dict[str, Probability] = Field(min_length=2)

    @model_validator(mode="after")
    def _sums_to_one(self) -> JevAnswer:
        total = sum(self.probs.values())
        if abs(total - 1.0) > 0.02:
            raise ValueError(f"问题 {self.id} 的概率和为 {total:.3f}，应约等于 1")
        return self

    def top(self) -> tuple[str, float]:
        """概率最高的选项和它的概率。"""
        choice = max(self.probs, key=lambda k: self.probs[k])
        return choice, self.probs[choice]


class JevUsage(Contract):
    """通道报的 token 用量。决策日志写在 ``output.usage``（面板「AI 费用详情」读这里）。

    没报的记 0。
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class JevResponse(Contract):
    """模型的回复。``ok=False`` 时 ``answers`` 为空，下游必须按故障即关闭处理。"""

    request_id: str = Field(min_length=1)
    ok: bool
    error: str | None = None
    model_version: str = Field(min_length=1, description="锁定的模型版本，写进日志用于校准。")
    latency_ms: NonNegFloat
    cost_usd: NonNegFloat = 0.0
    usage: JevUsage | None = None
    """token 用量；没真正调用（故障、规则版）时为 None。"""
    answers: list[JevAnswer] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> JevResponse:
        if self.ok and not self.answers:
            raise ValueError("ok=True 时必须有 answers")
        if not self.ok and not self.error:
            raise ValueError("ok=False 时必须写 error")
        if not self.ok and self.answers:
            raise ValueError("ok=False 时不能带 answers（故障即关闭）")
        return self

    def answer(self, question: str) -> JevAnswer | None:
        """某个问题的回答。``ok=False`` 时永远返回 None。

        这里不检查答案是否覆盖了请求的全部问题（契约拿不到请求）：保镖和管家用之前必须核对，
        缺任何一问、选项对不上，一律按故障处理，不得用公式补。
        """
        if not self.ok:
            return None
        return next((a for a in self.answers if a.id == question), None)
