"""档位（profile）：两套各选各的档位，给出跨模块的策略默认值。

Aaron 2026-10-23 定、研究 v2 落地：

* ``meeting_interval`` 开会间隔 7 档 15m / 30m / 1h / 2h / 6h / 12h / 24h（默认 1h）：
  军师团每隔多久开一轮会；卡有效期 = 间隔 + 提前量 20 分钟，没有「下限 1 小时」。
* ``hold`` 最长持仓 7 档 30m / 1h / 2h / 4h / 8h / 12h / 24h
  （不写 = 跟开会档位的推荐搭配，1h 开会 → 2h）：
  单笔最长拿多久（time barrier）。
  止损 ATR 周期、止损 / 止盈倍数、关键位范围、预测员预测多远、拍板门槛、管家复查频率都跟它走。

优先级：配置文件里写了的单项 > 档位默认 > 代码默认。也就是说档位只填你没写的键；
想让档位生效，就把 app.toml / slow.toml 里对应的行删掉或注释掉。
``check-config`` / ``status`` 会列出哪些键覆盖了档位。

档位只管策略参数，**不碰 risk.toml 硬上限**，只给推荐，用户的设置就是上限。
最长持仓 30m 的止损要贴近手续费，只能在 paper 跑；testnet / live 选它拒绝启动。
旧的 ``profile`` 键（原「持仓档位」= 开会间隔）自动映射到 ``meeting_interval`` 并提示。
切档要重启，只对之后的作战卡和新开仓生效。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from jiaoyi.contracts.risk_config import (
    HOLD_PROFILES,
    MEETING_PROFILES,
    PAPER_ONLY_HOLDS,
)

PROFILES: tuple[str, ...] = MEETING_PROFILES
HOLDS: tuple[str, ...] = HOLD_PROFILES
DEFAULT_PROFILE = "1h"
DEFAULT_HOLD = "2h"
DEFAULT_TIME_LIMIT_S = 7200
PAPER_ONLY = PAPER_ONLY_HOLDS  # 最长持仓档里只能 paper 的

# 旧 ``profile`` 键 → 开会间隔档（旧值 60m = 现 1h，其余同名）
LEGACY_PROFILE = {"2h": "2h", "60m": "1h", "30m": "30m", "15m": "15m", "1h": "1h"}

Section = Literal["app", "fast_loop", "slow.schedule", "slow.budget", "predictor"]


_E2 = ("key_level_break", "move_since_card", "new_head_symbol")
_E6 = (*_E2, "predictor_against_card", "news_high_impact")
_E12 = (*_E6, "stale_card_on_entry")


def _events(
    triggers: tuple[str, ...], *, gap: int, per_day: int, entry_age: int = 0, at: str | None = None
) -> dict[str, Any]:
    """临时会（只重开一个币）：哪些事件能触发、同一个币隔多久、一天最多几次。"""
    return {
        "event_meeting_enabled": True,
        "event_triggers": list(triggers),
        "event_symbol_gap_min": gap,
        "event_max_per_day": per_day,
        "entry_max_card_age_min": entry_age,
        "schedule_at": at,
    }


def _meeting(
    interval_min: int,
    *,
    ttl_extra: int = 20,
    gap: int | None = None,
    sym_gap: int,
    cards: int,
    backoff: list[float],
    daily_usd: float,
    slow_to: int | None = None,
    events: dict[str, Any] | None = None,
) -> dict[Section, dict[str, Any]]:
    """开会间隔一档：卡有效期 = 间隔 + 提前量（20 = 单币超时 15 分钟 + 5 分钟余量）。"""
    return {
        "app": {"slow_loop_interval_min": interval_min, "predictor_enabled": True},
        "slow.schedule": {
            "card_ttl_min": interval_min + ttl_extra,
            "symbol_min_interval_min": sym_gap,
            "max_cards_per_round": cards,
            "concurrency": 8,
            "refresh_lead_min": ttl_extra,
            "min_refresh_gap_min": interval_min if gap is None else gap,
            # 花到日上限 80%：有下一档就放慢到它（slow），没有（6h 起）直接当天停会（stop）
            "over_budget": "stop" if slow_to is None else "slow",
            "slowdown_interval_min": slow_to,
            **(events or {"event_meeting_enabled": False}),
        },
        "slow.budget": {"fail_backoff_minutes": backoff, "daily_cost_limit_usd": daily_usd},
    }


MEETING: dict[str, dict[Section, dict[str, Any]]] = {
    "15m": _meeting(
        15, sym_gap=12, cards=8, backoff=[2, 5, 10], daily_usd=75.0, slow_to=30
    ),  # 一波开完约 6–7 分钟
    "30m": _meeting(30, sym_gap=25, cards=8, backoff=[3, 5, 10], daily_usd=40.0, slow_to=60),
    "1h": _meeting(60, sym_gap=50, cards=12, backoff=[5, 10, 15], daily_usd=30.0, slow_to=120),
    "2h": _meeting(
        120,
        gap=60,
        sym_gap=100,
        cards=15,
        backoff=[5, 10, 30],
        daily_usd=20.0,
        slow_to=360,
        events=_events(_E2, gap=60, per_day=12),
    ),
    "6h": _meeting(
        360,
        gap=120,
        sym_gap=300,
        cards=15,
        backoff=[10, 30, 60],
        daily_usd=10.0,
        events=_events(_E6, gap=60, per_day=20),
    ),
    "12h": _meeting(
        720,
        gap=180,
        sym_gap=600,
        cards=15,
        backoff=[10, 30, 60],
        daily_usd=5.0,
        events=_events(_E12, gap=90, per_day=20, entry_age=360),
    ),
    "24h": _meeting(
        1440,
        gap=240,
        sym_gap=1200,
        cards=15,
        backoff=[10, 30, 60],
        daily_usd=5.0,
        events=_events(_E12, gap=120, per_day=24, entry_age=480, at="08:00"),
    ),
}

# 与档位无关、保持现值：keeper_close_p 0.6 / strong 0.85 / confirmations 2；
# 巡逻兵 patrol_shock_pct_1m 2.0 / patrol_alert_pct_1m 1.0（价格百分比的崩盘线）；
# 预测员 interval 5m / paths 4 / lookback 400；risk.toml 全部不动。
_LOOSE_GATE = {"min_p_genuine": 0.6, "min_p_take": 0.6, "max_p_toxic": 0.5}
_STRICT_GATE = {"min_p_genuine": 0.65, "min_p_take": 0.65, "max_p_toxic": 0.4}

HOLD: dict[str, dict[Section, dict[str, Any]]] = {
    "30m": {
        "fast_loop": {
            "time_limit_s": 1800,
            "atr_period_min": 5,
            "min_stop_atr": 1.2,
            "max_stop_atr": 3.0,
            "min_tp_r": 1.0,
            "max_tp_r": 2.0,
            "stop_level_buffer_atr": 0.2,  # 5m ATR 约为 15m 的一半
            "min_stop_fee_multiple": 3.5,  # 止损 ≥ 35bp
            **_STRICT_GATE,  # 短持仓费用占比高，要更确定才做
        },
        "app": {"keeper_interval_s": 10},
        "predictor": {"pred_len": 6},  # 6 × 5m = 30 分钟
    },
    "1h": {
        "fast_loop": {
            "time_limit_s": 3600,
            "atr_period_min": 15,
            "min_stop_atr": 0.8,
            "max_stop_atr": 2.0,
            "min_tp_r": 1.0,
            "max_tp_r": 2.5,
            "stop_level_buffer_atr": 0.1,
            "min_stop_fee_multiple": 3.0,  # ≥ 30bp
            **_LOOSE_GATE,
        },
        "app": {"keeper_interval_s": 15},
        "predictor": {"pred_len": 12},
    },
    "2h": {
        "fast_loop": {
            "time_limit_s": 7200,
            "atr_period_min": 15,
            "min_stop_atr": 1.0,
            "max_stop_atr": 3.0,
            "min_tp_r": 1.2,
            "max_tp_r": 3.0,
            "stop_level_buffer_atr": 0.1,
            "min_stop_fee_multiple": 2.5,  # ≥ 25bp
            **_LOOSE_GATE,
        },
        "app": {"keeper_interval_s": 20},
        "predictor": {"pred_len": 24},
    },
    "4h": {
        "fast_loop": {
            "time_limit_s": 14400,
            "atr_period_min": 30,  # 30m ATR 适合 2–4 小时
            "min_stop_atr": 1.0,
            "max_stop_atr": 3.0,
            "min_tp_r": 1.2,
            "max_tp_r": 4.0,
            "stop_level_buffer_atr": 0.1,
            "min_stop_fee_multiple": 2.0,  # ≥ 20bp（4h 止损本来就宽，基本不会卡）
            **_LOOSE_GATE,
        },
        "app": {"keeper_interval_s": 30},
        "predictor": {"pred_len": 48},  # 48 × 5m = 4 小时；回看 400 + 48 ≤ Kronos 上下文 512
    },
}

# 推荐搭配：开会档位 → 最长持仓默认（app.toml 没写 hold 时取它；写了就按用户的）。
RECOMMENDED_HOLD = {
    "15m": "30m",
    "30m": "1h",
    "1h": "2h",
    "2h": "4h",
    "6h": "8h",
    "12h": "12h",
    "24h": "24h",
}
HOLD.update(
    {
        # 长持仓 3 档（研究 2026-10-23 18:40 补）。patrol_*：山寨中档止损 1.6–3.5%，
        # 按 2% 全平会误杀，所以长档放宽。atr_floor_7d / 预测员 15m·1h K 线见 docs/slow.md「待做」。
        "8h": {
            "fast_loop": {
                "time_limit_s": 28800,
                "atr_period_min": 60,
                "min_stop_atr": 1.0,
                "max_stop_atr": 3.0,
                "min_tp_r": 1.2,
                "max_tp_r": 4.0,
                "stop_level_buffer_atr": 0.1,
                "min_stop_fee_multiple": 2.0,
                "funding_against_max_bp": 5.0,
                "patrol_shock_pct_1m": 2.5,
                "patrol_alert_pct_1m": 1.2,
                **_LOOSE_GATE,
            },
            "app": {"keeper_interval_s": 45},
        },
        "12h": {
            "fast_loop": {
                "time_limit_s": 43200,
                "atr_period_min": 60,
                "min_stop_atr": 1.2,
                "max_stop_atr": 3.5,
                "min_tp_r": 1.2,
                "max_tp_r": 4.0,
                "stop_level_buffer_atr": 0.1,
                "min_stop_fee_multiple": 2.0,
                "funding_against_max_bp": 5.0,
                "patrol_shock_pct_1m": 3.0,
                "patrol_alert_pct_1m": 1.5,
                **_LOOSE_GATE,
            },
            "app": {"keeper_interval_s": 60},
        },
        "24h": {
            "fast_loop": {
                "time_limit_s": 86400,
                "atr_period_min": 240,  # 4 小时 ATR，启动回填 3840 根 1 分钟线（分页拉）
                "min_stop_atr": 0.8,
                "max_stop_atr": 2.5,
                "min_tp_r": 1.2,
                "max_tp_r": 4.0,
                "stop_level_buffer_atr": 0.1,
                "min_stop_fee_multiple": 2.0,
                "funding_against_max_bp": 5.0,
                "patrol_shock_pct_1m": 3.0,
                "patrol_alert_pct_1m": 1.5,
                **_LOOSE_GATE,
            },
            "app": {"keeper_interval_s": 90},
        },
    }
)

_HOLD_BY_SECONDS = tuple((h["fast_loop"]["time_limit_s"], name) for name, h in HOLD.items())


def hold_for_seconds(time_limit_s: int) -> str:
    """只写了 ``time_limit_s`` 没写 ``hold`` 时：取不短于它的最近一档（超过 24h 按 24h）。"""
    for limit, name in sorted(_HOLD_BY_SECONDS):
        if time_limit_s <= limit:
            return name
    return "24h"


def profile_of(raw_app: Mapping[str, Any]) -> str:
    """app.toml 里的开会间隔档：``meeting_interval``，旧 ``profile`` 键自动映射，都没写 = 1h。
    不认识的值原样返回，由 AppConfig 校验拒绝。"""
    value = raw_app.get("meeting_interval")
    if value is None and "profile" in raw_app:
        legacy = str(raw_app["profile"])
        return LEGACY_PROFILE.get(legacy, legacy)
    if value is None:
        return DEFAULT_PROFILE
    return value if isinstance(value, str) else str(value)


def hold_of(raw_app: Mapping[str, Any]) -> str:
    """最长持仓档：``hold``；没写但 ``fast_loop.time_limit_s`` 写了就按它取档；
    都没写 = 开会档位的推荐搭配（``RECOMMENDED_HOLD``）。"""
    value = raw_app.get("hold")
    if isinstance(value, str):
        return value
    fast = raw_app.get("fast_loop")
    limit = fast.get("time_limit_s") if isinstance(fast, Mapping) else None
    if isinstance(limit, int) and not isinstance(limit, bool):
        return hold_for_seconds(limit)
    return RECOMMENDED_HOLD.get(profile_of(raw_app), DEFAULT_HOLD)


def hold_defaults(time_limit_s: int) -> dict[str, Any]:
    """单笔最长持仓 ``time_limit_s`` 秒对应的 fast_loop 默认值（取不短于它的最近一档）。"""
    return dict(HOLD[hold_for_seconds(time_limit_s)]["fast_loop"])


def hold_is_custom(raw_app: Mapping[str, Any]) -> bool:
    """最长持仓是用户自己定的（写了 hold 或 time_limit_s），不是跟开会档位走的推荐值。"""
    fast = raw_app.get("fast_loop")
    return isinstance(raw_app.get("hold"), str) or (
        isinstance(fast, Mapping) and "time_limit_s" in fast
    )


def legacy_notice(raw_app: Mapping[str, Any]) -> str | None:
    """app.toml 还在用旧 ``profile`` 键时的提示（映射结果见 ``profile_of``）。"""
    if "profile" not in raw_app or raw_app.get("meeting_interval") is not None:
        return None
    return (
        f"app.toml 的 profile = {raw_app['profile']!r} 是旧写法，已按开会间隔 "
        f"meeting_interval = {profile_of(raw_app)!r} 处理；请改用 meeting_interval，"
        "最长持仓另用 hold 选（默认 2h）"
    )


def defaults(profile: str, section: Section, hold: str = DEFAULT_HOLD) -> dict[str, Any]:
    """``profile`` 是开会间隔档；fast_loop / predictor / keeper_interval_s 看 ``hold``。"""
    profile = LEGACY_PROFILE.get(profile, profile)
    out: dict[str, Any] = dict(MEETING.get(profile, {}).get(section, {}))
    if section in HOLD.get(hold, HOLD[DEFAULT_HOLD]):
        out.update(HOLD.get(hold, HOLD[DEFAULT_HOLD])[section])
    return out


def _hold_in(raw: Mapping[str, Any], hold: str | None, section: Section) -> str:
    if hold is not None:
        return hold
    if section == "fast_loop":
        limit = raw.get("time_limit_s")
        if isinstance(limit, int) and not isinstance(limit, bool):
            return hold_for_seconds(limit)
    return DEFAULT_HOLD


def fill(
    raw: Mapping[str, Any], profile: str, section: Section, hold: str | None = None
) -> dict[str, Any]:
    """默认垫底，配置文件里写了的键覆盖。返回新 dict，不改 ``raw``。"""
    return {**defaults(profile, section, _hold_in(raw, hold, section)), **raw}


def overrides(
    raw: Mapping[str, Any], profile: str, section: Section, hold: str | None = None
) -> list[str]:
    """配置文件里覆盖了默认、且值和默认不一样的键（给 check-config / status 显示）。"""
    base = defaults(profile, section, _hold_in(raw, hold, section))
    return sorted(k for k in base if k in raw and raw[k] != base[k])
