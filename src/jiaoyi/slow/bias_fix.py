"""单币会议的偏空整改开关（见 short-bias-investigation-2026-10-28）和按会议的分组。

四个开关，默认值 = 现状（提示词和发言顺序一字不差）：

- ``debate_first``：bull = 多头先说、空头最后说（现状）；bear = 反过来；alternate = 按会议交替。
- ``funding_label``：legacy = 资金费率写「positive: longs pay, crowded long」（现状）；
  neutral = 只写谁付谁 + 币安基准 +0.01%/8h，并写明只有明显高于基准才算多头拥挤。
- ``bear_bull_symmetry``：legacy = 只有空头有「拥挤 / 已被挤出的不再是燃料」这条（现状）；
  mirror = 多头加一条对称的。
- ``crowd_ratio_mode``：raw = 只给账户多空比原值（现状）；relative = 再给相对这个币自己 30 天
  中位数的偏离，并注明多数币长期在 1 以上。

``bias_fix_ab``（0–1）：0 = 每场会都按上面的配置开；> 0 = 每场会按这个概率用上面的配置（new 组），
否则全部用现状（legacy 组），两组同时跑，便于按超额记分对照。分组和 alternate 都按会议 id
的哈希定，可复现；每场会把实际用的设置记进决策日志和复盘流水。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

DebateFirst = Literal["bull", "bear", "alternate"]
FundingLabel = Literal["legacy", "neutral"]
Symmetry = Literal["legacy", "mirror"]
CrowdRatioMode = Literal["raw", "relative"]


@dataclass(frozen=True)
class BiasFix:
    """配置里写的开关（``slow.toml`` 的 ``[meeting]``）。"""

    debate_first: DebateFirst = "bull"
    funding_label: FundingLabel = "legacy"
    bear_bull_symmetry: Symmetry = "legacy"
    crowd_ratio_mode: CrowdRatioMode = "raw"
    ab: float = 0.0

    @property
    def is_legacy(self) -> bool:
        """四个开关都是现状（不看 ab）。"""
        return (
            self.debate_first,
            self.funding_label,
            self.bear_bull_symmetry,
            self.crowd_ratio_mode,
        ) == ("bull", "legacy", "legacy", "raw")


LEGACY = BiasFix()


@dataclass(frozen=True)
class MeetingSetup:
    """一场会实际用的设置（alternate 已解析成 bull / bear）。"""

    meeting_id: str
    arm: Literal["off", "new", "legacy"]  # off = 没开对照（ab = 0），按配置
    debate_first: Literal["bull", "bear"]
    funding_label: FundingLabel
    bear_bull_symmetry: Symmetry
    crowd_ratio_mode: CrowdRatioMode
    debate_first_setting: DebateFirst  # 这组配置里写的（alternate 时能看出是交替来的）

    def record(self) -> dict[str, str]:
        """写进决策日志 / 复盘流水的字段（只加不删）。"""
        return {
            "meeting_id": self.meeting_id,
            "arm": self.arm,
            "debate_first": self.debate_first,
            "debate_first_setting": self.debate_first_setting,
            "funding_label": self.funding_label,
            "bear_bull_symmetry": self.bear_bull_symmetry,
            "crowd_ratio_mode": self.crowd_ratio_mode,
        }

    def describe(self) -> str:
        """面板实况里的一行。"""
        arm = {"off": "全部按配置", "new": "对照：新设置组", "legacy": "对照：现状组"}[self.arm]
        return (
            f"会议设置（{arm}）：先发言 {self.debate_first}（配置 {self.debate_first_setting}），"
            f"资金费率说明 {self.funding_label}，多空对称 {self.bear_bull_symmetry}，"
            f"多空比 {self.crowd_ratio_mode}"
        )


LEGACY_SETUP = MeetingSetup("", "off", "bull", "legacy", "legacy", "raw", "bull")


def unit(meeting_id: str, salt: str) -> float:
    """会议 id 的哈希 → [0, 1)，确定、可复现（不同 salt 互不相关）。"""
    h = hashlib.sha256(f"{salt}|{meeting_id}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def resolve(cfg: BiasFix, meeting_id: str) -> MeetingSetup:
    """这场会用哪组设置。ab = 0 → 按配置；ab > 0 → 按 ab 的概率进 new 组，否则 legacy 组。"""
    if cfg.ab > 0:
        arm: Literal["off", "new", "legacy"] = (
            "new" if unit(meeting_id, "bias_fix_ab") < cfg.ab else "legacy"
        )
    else:
        arm = "off"
    chosen = LEGACY if arm == "legacy" else cfg
    first: Literal["bull", "bear"]
    if chosen.debate_first == "alternate":
        first = "bull" if unit(meeting_id, "debate_first") < 0.5 else "bear"
    else:
        first = chosen.debate_first
    return MeetingSetup(
        meeting_id=meeting_id,
        arm=arm,
        debate_first=first,
        funding_label=chosen.funding_label,
        bear_bull_symmetry=chosen.bear_bull_symmetry,
        crowd_ratio_mode=chosen.crowd_ratio_mode,
        debate_first_setting=chosen.debate_first,
    )
