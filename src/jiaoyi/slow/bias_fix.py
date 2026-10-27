"""单币会议的偏空整改开关（见 short-bias-investigation-2026-10-28）和按会议的分组。

四个开关，默认值 = 现状（提示词和发言顺序一字不差）：

- ``debate_first``：bull = 多头先说、空头最后说（现状）；bear = 反过来；alternate = 按会议交替。
- ``funding_label``：legacy = 资金费率写「positive: longs pay, crowded long」（现状）；
  neutral = 只写谁付谁 + 币安基准（按结算间隔折算），明显高于 / 低于基准才算多头 / 空头拥挤。
- ``bear_bull_symmetry``：legacy = 现状（空头独有「拥挤 / 不再是燃料」，多头独有「强平耗尽后反弹」
  等）；mirror = 多空两份正文逐句对称，经理 / 交易员 / 共用说明里的单边写法也补齐另一边。
- ``crowd_ratio_mode``：raw = 只给账户多空比原值（现状）；relative = 再给相对这个币自己 30 天
  中位数的偏离，并注明多数币长期在 1 以上。

``bias_fix_ab``（0–1）：0 = 每场会都按上面的配置开。> 0 = 析因对照：上面每个改了的开关**各自独立**
按这个概率用配置值、否则用现状（每个开关一个独立加盐哈希；四个都改时是 2⁴ 组），所以能分别估计
每个开关的效果。分组和 alternate 都按会议 id 的哈希定，可复现；每场会（含失败的会）把实际用的
设置、ab 和配置版本号记进决策日志，开成的会也记进复盘流水。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

DebateFirst = Literal["bull", "bear", "alternate"]
FundingLabel = Literal["legacy", "neutral"]
Symmetry = Literal["legacy", "mirror"]
CrowdRatioMode = Literal["raw", "relative"]
Arm = Literal["off", "new", "legacy", "mixed"]

SWITCHES = ("debate_first", "funding_label", "bear_bull_symmetry", "crowd_ratio_mode")


@dataclass(frozen=True)
class BiasFix:
    """配置里写的开关（``slow.toml`` 的 ``[meeting]``）。"""

    debate_first: DebateFirst = "bull"
    funding_label: FundingLabel = "legacy"
    bear_bull_symmetry: Symmetry = "legacy"
    crowd_ratio_mode: CrowdRatioMode = "raw"
    ab: float = 0.0

    @property
    def changed(self) -> tuple[str, ...]:
        """和现状不同的开关（不看 ab）。"""
        return tuple(k for k in SWITCHES if getattr(self, k) != getattr(LEGACY, k))

    @property
    def is_legacy(self) -> bool:
        """四个开关都是现状（不看 ab）。"""
        return not self.changed

    @property
    def version(self) -> str:
        """配置版本号：四个开关 + ab 的哈希，改了配置就换一个，复盘时据此分开不同批次。"""
        raw = json.dumps(asdict(self), sort_keys=True)
        return "bf-" + hashlib.sha256(raw.encode()).hexdigest()[:8]


LEGACY = BiasFix()


@dataclass(frozen=True)
class MeetingSetup:
    """一场会实际用的设置（alternate 已解析成 bull / bear）。"""

    meeting_id: str
    # off = 没开对照（ab = 0），按配置；new / legacy = 改了的开关全开 / 全没开；mixed = 部分开
    arm: Arm
    debate_first: Literal["bull", "bear"]
    funding_label: FundingLabel
    bear_bull_symmetry: Symmetry
    crowd_ratio_mode: CrowdRatioMode
    debate_first_setting: DebateFirst  # 这场会这个开关取的值（alternate 时能看出是交替来的）
    ab: float = 0.0
    config_version: str = LEGACY.version
    switches_on: tuple[str, ...] = ()  # 这场会用了配置值的开关（对照分到 new 的那几个）

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
            "ab": f"{self.ab:g}",
            "config_version": self.config_version,
            "switches_on": ",".join(self.switches_on),
        }

    def describe(self) -> str:
        """面板实况里的一行。"""
        arm = {
            "off": "全部按配置",
            "new": "对照：改了的开关全开",
            "legacy": "对照：全部现状",
            "mixed": "对照：部分开关开",
        }[self.arm]
        return (
            f"会议设置（{arm}，{self.config_version}）：先发言 {self.debate_first}"
            f"（取值 {self.debate_first_setting}），资金费率说明 {self.funding_label}，"
            f"多空对称 {self.bear_bull_symmetry}，多空比 {self.crowd_ratio_mode}"
        )


LEGACY_SETUP = MeetingSetup("", "off", "bull", "legacy", "legacy", "raw", "bull")


def unit(meeting_id: str, salt: str) -> float:
    """会议 id 的哈希 → [0, 1)，确定、可复现（不同 salt 互不相关）。"""
    h = hashlib.sha256(f"{salt}|{meeting_id}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def resolve(cfg: BiasFix, meeting_id: str) -> MeetingSetup:
    """这场会用哪组设置。ab = 0 → 全按配置；ab > 0 → 每个改了的开关各自按 ab 的概率用配置值。"""
    changed = cfg.changed
    if cfg.ab > 0:
        on = tuple(k for k in changed if unit(meeting_id, f"bias_fix_ab:{k}") < cfg.ab)
        arm: Arm = "new" if on == changed else "legacy" if not on else "mixed"
    else:
        on, arm = changed, "off"
    value = {k: getattr(cfg if k in on else LEGACY, k) for k in SWITCHES}
    first: Literal["bull", "bear"]
    if value["debate_first"] == "alternate":
        first = "bull" if unit(meeting_id, "debate_first") < 0.5 else "bear"
    else:
        first = value["debate_first"]
    return MeetingSetup(
        meeting_id=meeting_id,
        arm=arm,
        debate_first=first,
        funding_label=value["funding_label"],
        bear_bull_symmetry=value["bear_bull_symmetry"],
        crowd_ratio_mode=value["crowd_ratio_mode"],
        debate_first_setting=value["debate_first"],
        ab=cfg.ab,
        config_version=cfg.version,
        switches_on=on,
    )
