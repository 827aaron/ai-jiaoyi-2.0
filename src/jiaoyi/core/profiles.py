"""持仓档位（profile）：一个开关选档，档位给出跨模块的策略默认值。

Aaron 2026-10-23 定：系统只做 4 个档位 2h / 60m / 30m / 15m，默认 2h。
数值来自研究报告「持仓档位参数表」（币安 U 本位 1m K 线 2026-09-22 → 10-21）。

优先级：配置文件里写了的单项 > 档位默认 > 代码默认。也就是说档位只填你没写的键；
想让档位生效，就把 app.toml / slow.toml 里对应的行删掉或注释掉。
``check-config`` / ``status`` 会列出哪些键覆盖了档位。

档位只管策略参数，**不碰 risk.toml 硬上限**。30m / 15m 只能在 paper 跑（费用账本不成立），
testnet / live 选这两档拒绝启动。切档要重启，只对之后的作战卡和新开仓生效。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, get_args

from jiaoyi.contracts.risk_config import PAPER_ONLY_PROFILES, HoldingProfile

PROFILES: tuple[str, ...] = get_args(HoldingProfile)
DEFAULT_PROFILE: HoldingProfile = "2h"
PAPER_ONLY = PAPER_ONLY_PROFILES

Section = Literal["app", "fast_loop", "slow.schedule", "slow.budget", "predictor"]

_SLOW_1H = {
    "card_ttl_hours": 1.0,
    "symbol_min_interval_hours": 0.75,
    "max_cards_per_round": 5,
    "concurrency": 8,
    "refresh_lead_min": 20,  # 单币超时 15 分钟 + 5 分钟余量
    "min_refresh_gap_min": 40,  # 卡有效期 − 提前量
}
_STRICT_GATE = {"min_p_genuine": 0.65, "min_p_take": 0.65, "max_p_toxic": 0.4}

DEFAULTS: dict[str, dict[Section, dict[str, Any]]] = {
    "2h": {
        "app": {"keeper_interval_s": 20, "slow_loop_interval_min": 100, "predictor_enabled": True},
        "fast_loop": {
            "time_limit_s": 7200,
            "atr_period_min": 15,
            "min_stop_atr": 1.0,
            "max_stop_atr": 3.0,
            "min_tp_r": 1.2,
            "max_tp_r": 3.0,
            "stop_level_buffer_atr": 0.1,
        },
        "slow.schedule": {
            "card_ttl_hours": 2.0,
            "symbol_min_interval_hours": 1.67,
            "max_cards_per_round": 10,
            "concurrency": 8,
            "refresh_lead_min": 20,  # 一轮 3 波估 7–19 分钟；单币超时 15 分钟 + 5 分钟余量
            # 关键位突破 / 新入池也要隔这么久：2h 档 = 2 小时一轮会（实测一轮约 $0.7）
            "min_refresh_gap_min": 100,
        },
        "slow.budget": {"fail_backoff_minutes": [5, 10, 30]},
        "predictor": {"pred_len": 24},  # 24 × 5m = 2 小时
    },
    "60m": {
        "app": {"keeper_interval_s": 15, "slow_loop_interval_min": 45, "predictor_enabled": True},
        "fast_loop": {
            "time_limit_s": 3600,
            "atr_period_min": 15,
            "min_stop_atr": 0.8,
            "max_stop_atr": 2.0,
            "min_tp_r": 1.0,
            "max_tp_r": 2.5,
            "stop_level_buffer_atr": 0.1,
        },
        "slow.schedule": _SLOW_1H,
        "slow.budget": {"fail_backoff_minutes": [5, 10, 15]},
        "predictor": {"pred_len": 12},
    },
    "30m": {
        "app": {"keeper_interval_s": 10, "slow_loop_interval_min": 45, "predictor_enabled": True},
        "fast_loop": {
            "time_limit_s": 1800,
            "atr_period_min": 5,
            "min_stop_atr": 1.2,
            "max_stop_atr": 3.0,
            "min_tp_r": 1.0,
            "max_tp_r": 2.0,
            "stop_level_buffer_atr": 0.2,  # 5m ATR 约为 15m 的一半
            **_STRICT_GATE,
        },
        "slow.schedule": _SLOW_1H,
        "slow.budget": {"fail_backoff_minutes": [5, 10, 15]},
        "predictor": {"pred_len": 6},
    },
    "15m": {
        # Kronos 只预测 3 根 5m，价值低，默认关
        "app": {"keeper_interval_s": 10, "slow_loop_interval_min": 45, "predictor_enabled": False},
        "fast_loop": {
            "time_limit_s": 900,
            "atr_period_min": 5,
            "min_stop_atr": 1.0,
            "max_stop_atr": 2.5,
            "min_tp_r": 1.0,
            "max_tp_r": 1.5,
            "stop_level_buffer_atr": 0.2,
            **_STRICT_GATE,
        },
        "slow.schedule": _SLOW_1H,
        "slow.budget": {"fail_backoff_minutes": [5, 10, 15]},
        "predictor": {"pred_len": 3},
    },
}


def profile_of(raw_app: Mapping[str, Any]) -> str:
    """app.toml 里写的档位；没写就是默认 2h。不认识的档位原样返回，由 AppConfig 校验拒绝。"""
    value = raw_app.get("profile", DEFAULT_PROFILE)
    return value if isinstance(value, str) else str(value)


def defaults(profile: str, section: Section) -> dict[str, Any]:
    return dict(DEFAULTS.get(profile, {}).get(section, {}))


def fill(raw: Mapping[str, Any], profile: str, section: Section) -> dict[str, Any]:
    """档位默认垫底，配置文件里写了的键覆盖。返回新 dict，不改 ``raw``。"""
    return {**defaults(profile, section), **raw}


def overrides(raw: Mapping[str, Any], profile: str, section: Section) -> list[str]:
    """配置文件里覆盖了档位默认、且值和档位不一样的键（给 check-config / status 显示）。"""
    base = defaults(profile, section)
    return sorted(k for k in base if k in raw and raw[k] != base[k])
