"""持仓档位（profile）：一个开关选档，档位给出跨模块的策略默认值。

Aaron 2026-10-23 定：系统做 7 个档位 24h / 12h / 6h / 2h / 60m / 30m / 15m，
默认 2h（18:25 加了 6h / 12h / 24h）。
数值来自研究报告「持仓档位参数表」（币安 U 本位 1m K 线 2026-09-22 → 10-21）。

优先级：配置文件里写了的单项 > 档位默认 > 代码默认。也就是说档位只填你没写的键；
想让档位生效，就把 app.toml / slow.toml 里对应的行删掉或注释掉。
``check-config`` / ``status`` 会列出哪些键覆盖了档位。

档位只管策略参数，**不碰 risk.toml 硬上限**。30m / 15m 只能在 paper 跑（费用账本不成立），
testnet / live 选这两档拒绝启动。
档位 = 军师团开会间隔（2h 每 120 分钟 / 60m 60 / 30m 30 / 15m 15），不是持仓时长；
单笔最长持仓 ``fast_loop.time_limit_s`` 是独立设置（默认 7200 秒），止损 / 止盈 / ATR 周期、
军师团 hold_minutes、关键位范围、提示词里的「最多拿 N 分钟」、预测员预测长度都跟它走，不跟档位。
切档要重启，只对之后的作战卡和新开仓生效。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, get_args

from jiaoyi.contracts.risk_config import PAPER_ONLY_PROFILES, HoldingProfile

PROFILES: tuple[str, ...] = get_args(HoldingProfile)
DEFAULT_PROFILE: HoldingProfile = "2h"
PAPER_ONLY = PAPER_ONLY_PROFILES

Section = Literal["app", "fast_loop", "slow.schedule", "slow.budget", "predictor"]


def _slow(interval_min: int, cards: int, lead_min: int = 20) -> dict[str, Any]:
    """档位 = 军师团开会间隔（分钟）。卡有效期 = 间隔 + 提前量，换卡赶在旧卡到期前；
    提前量 20 = 单币超时 15 分钟 + 5 分钟余量。"""
    return {
        "card_ttl_min": interval_min + lead_min,
        "symbol_min_interval_min": max(interval_min - 5, 5),
        "max_cards_per_round": cards,
        "concurrency": 8,
        "refresh_lead_min": lead_min,
        "min_refresh_gap_min": interval_min,
    }


# 持仓时长相关的策略参数跟「单笔最长持仓」fast_loop.time_limit_s 走，不跟档位走。
# 按持仓时长分段取默认（≤上限秒数），没写 time_limit_s 就按 7200 秒（2 小时）那套。
_STRICT_GATE = {"min_p_genuine": 0.65, "min_p_take": 0.65, "max_p_toxic": 0.4}

DEFAULT_TIME_LIMIT_S = 7200
_FAST_2H = {
    "atr_period_min": 15,
    "min_stop_atr": 1.0,
    "max_stop_atr": 3.0,
    "min_tp_r": 1.2,
    "max_tp_r": 3.0,
    "stop_level_buffer_atr": 0.1,
    "min_p_genuine": 0.6,
    "min_p_take": 0.6,
    "max_p_toxic": 0.5,
}
HOLD_BANDS: tuple[tuple[int, dict[str, Any]], ...] = (
    (
        900,
        {
            "atr_period_min": 5,
            "min_stop_atr": 1.0,
            "max_stop_atr": 2.5,
            "min_tp_r": 1.0,
            "max_tp_r": 1.5,
            "stop_level_buffer_atr": 0.2,
            **_STRICT_GATE,
        },
    ),
    (
        1800,
        {
            "atr_period_min": 5,
            "min_stop_atr": 1.2,
            "max_stop_atr": 3.0,
            "min_tp_r": 1.0,
            "max_tp_r": 2.0,
            "stop_level_buffer_atr": 0.2,  # 5m ATR 约为 15m 的一半
            **_STRICT_GATE,
        },
    ),
)


def hold_defaults(time_limit_s: int) -> dict[str, Any]:
    """单笔最长持仓 ``time_limit_s`` 秒对应的止损 / 止盈 / ATR 周期 / 拍板门槛默认值。"""
    for limit, params in HOLD_BANDS:
        if time_limit_s <= limit:
            return dict(params)
    return dict(_FAST_2H)


def _long(interval_min: int) -> dict[Section, dict[str, Any]]:
    """6h / 12h / 24h 档：只是开会间隔拉长，其余同 2h 档（参数细节等研究结果再调）。"""
    return {
        "app": {
            "keeper_interval_s": 20,
            "slow_loop_interval_min": interval_min,
            "predictor_enabled": True,
        },
        "slow.schedule": _slow(interval_min, 10),
        "slow.budget": {"fail_backoff_minutes": [5, 10, 30]},
    }


DEFAULTS: dict[str, dict[Section, dict[str, Any]]] = {
    "24h": _long(1440),
    "12h": _long(720),
    "6h": _long(360),
    "2h": {
        "app": {"keeper_interval_s": 20, "slow_loop_interval_min": 120, "predictor_enabled": True},
        "slow.schedule": _slow(120, 10),
        "slow.budget": {"fail_backoff_minutes": [5, 10, 30]},
    },
    "60m": {
        "app": {"keeper_interval_s": 15, "slow_loop_interval_min": 60, "predictor_enabled": True},
        "slow.schedule": _slow(60, 5),
        "slow.budget": {"fail_backoff_minutes": [5, 10, 15]},
    },
    "30m": {
        "app": {"keeper_interval_s": 10, "slow_loop_interval_min": 30, "predictor_enabled": True},
        "slow.schedule": _slow(30, 5),
        "slow.budget": {"fail_backoff_minutes": [5, 10, 15]},
    },
    "15m": {
        "app": {"keeper_interval_s": 10, "slow_loop_interval_min": 15, "predictor_enabled": True},
        "slow.schedule": _slow(15, 5),
        "slow.budget": {"fail_backoff_minutes": [5, 10, 15]},
    },
}


def profile_of(raw_app: Mapping[str, Any]) -> str:
    """app.toml 里写的档位；没写就是默认 2h。不认识的档位原样返回，由 AppConfig 校验拒绝。"""
    value = raw_app.get("profile", DEFAULT_PROFILE)
    return value if isinstance(value, str) else str(value)


def defaults(profile: str, section: Section) -> dict[str, Any]:
    if section == "fast_loop":
        return hold_defaults(DEFAULT_TIME_LIMIT_S)
    if section == "predictor":
        return {}
    return dict(DEFAULTS.get(profile, {}).get(section, {}))


def _base(raw: Mapping[str, Any], profile: str, section: Section) -> dict[str, Any]:
    if section == "fast_loop":
        limit = raw.get("time_limit_s", DEFAULT_TIME_LIMIT_S)
        return hold_defaults(limit if isinstance(limit, int) else DEFAULT_TIME_LIMIT_S)
    return defaults(profile, section)


def fill(raw: Mapping[str, Any], profile: str, section: Section) -> dict[str, Any]:
    """默认垫底，配置文件里写了的键覆盖。返回新 dict，不改 ``raw``。fast_loop 的默认看
    ``time_limit_s``（单笔最长持仓），其它看档位。"""
    return {**_base(raw, profile, section), **raw}


def overrides(raw: Mapping[str, Any], profile: str, section: Section) -> list[str]:
    """配置文件里覆盖了默认、且值和默认不一样的键（给 check-config / status 显示）。"""
    base = _base(raw, profile, section)
    return sorted(k for k in base if k in raw and raw[k] != base[k])
