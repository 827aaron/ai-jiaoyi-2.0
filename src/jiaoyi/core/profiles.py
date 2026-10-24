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
DEFAULT_PROFILE = "2h"
DEFAULT_HOLD = "4h"
DEFAULT_TIME_LIMIT_S = 14400
PAPER_ONLY = PAPER_ONLY_HOLDS  # 最长持仓档里只能 paper 的

# 旧 ``profile`` 键 → 开会间隔档（旧值 60m = 现 1h，其余同名）
LEGACY_PROFILE = {"2h": "2h", "60m": "1h", "30m": "30m", "15m": "15m", "1h": "1h"}

Section = Literal["app", "fast_loop", "slow.schedule", "slow.budget", "predictor"]


_E2 = ("key_level_break", "move_since_card", "new_head_symbol")
_E6 = (*_E2, "predictor_against_card", "signal_against_card", "news_high_impact")
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
        "app": {
            "slow_loop_interval_min": interval_min,
            "screener_interval_min": interval_min,  # 选币跟开会同节奏
            "predictor_enabled": True,
        },
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
        # 反向信号反复出（30 分钟 2 次）的单币临时会，研究 2026-10-24
        events=_events((*_E2, "signal_against_card"), gap=60, per_day=12, entry_age=360),
    ),
    "6h": _meeting(
        360,
        gap=120,
        sym_gap=300,
        cards=15,
        backoff=[10, 30, 60],
        daily_usd=10.0,
        events=_events(_E6, gap=60, per_day=20, entry_age=1080),
    ),
    "12h": _meeting(
        720,
        gap=180,
        sym_gap=600,
        cards=15,
        backoff=[10, 30, 60],
        daily_usd=5.0,
        events=_events(_E12, gap=90, per_day=20, entry_age=2160),
    ),
    "24h": _meeting(
        1440,
        gap=240,
        sym_gap=1200,
        cards=15,
        backoff=[10, 30, 60],
        daily_usd=5.0,
        events=_events(_E12, gap=120, per_day=24, entry_age=2160, at="08:00"),
    ),
}

# 与档位无关、保持现值：keeper_close_p 0.6 / strong 0.85 / confirmations 2；
# 巡逻兵 patrol_shock_pct_1m 2.0 / patrol_alert_pct_1m 1.0（价格百分比的崩盘线）；
# 预测员 paths 4 / lookback 400；risk.toml 全部不动。
_LOOSE_GATE = {"min_p_genuine": 0.6, "min_p_take": 0.6, "max_p_toxic": 0.5}
_STRICT_GATE = {"min_p_genuine": 0.65, "min_p_take": 0.65, "max_p_toxic": 0.4}
PAPER_GATE = {"min_p_genuine": 0.35, "min_p_take": 0.35, "max_p_toxic": 0.7}
"""paper 的临时推荐门槛（各持仓档一样），盖在档位默认上面；配置文件写了的仍优先。
上面两组是 testnet / live 的推荐，等 ``jiaoyi jev-backtest`` 攒够样本校准后再改。"""

HOLD: dict[str, dict[Section, dict[str, Any]]] = {
    "30m": {
        "fast_loop": {
            "time_limit_s": 1800,
            "atr_period_min": 5,
            "min_stop_atr": 0.45,
            "max_stop_atr": 1.4,
            "min_tp_r": 1.0,
            "max_tp_r": 3.0,
            "stop_level_buffer_atr": 0.2,
            "min_stop_fee_multiple": 3.5,  # 止损 ≥ 35bp
            **_STRICT_GATE,  # 短持仓费用占比高，要更确定才做
        },
        "app": {"keeper_interval_s": 10, "signal_dedup_s": 60},
        "predictor": {"interval": "5m", "pred_len": 6},  # 6 × 5m = 30 分钟
    },
    "1h": {
        "fast_loop": {
            "time_limit_s": 3600,
            "atr_period_min": 5,
            "min_stop_atr": 0.6,
            "max_stop_atr": 2.05,
            "min_tp_r": 1.0,
            "max_tp_r": 3.0,
            "stop_level_buffer_atr": 0.1,
            "min_stop_fee_multiple": 3.0,  # ≥ 30bp
            **_LOOSE_GATE,
        },
        "app": {"keeper_interval_s": 15, "signal_dedup_s": 120},
        "predictor": {"interval": "5m", "pred_len": 12},
    },
    "2h": {
        "fast_loop": {
            "time_limit_s": 7200,
            "atr_period_min": 5,
            "min_stop_atr": 0.9,
            "max_stop_atr": 2.95,
            "min_tp_r": 1.0,
            "max_tp_r": 3.0,
            "stop_level_buffer_atr": 0.1,
            "min_stop_fee_multiple": 2.5,  # ≥ 25bp
            **_LOOSE_GATE,
        },
        "app": {"keeper_interval_s": 20, "signal_dedup_s": 300},
        "predictor": {"interval": "5m", "pred_len": 24},
    },
    "4h": {
        "fast_loop": {
            "time_limit_s": 14400,
            "atr_period_min": 15,
            "min_stop_atr": 0.65,
            "max_stop_atr": 2.15,
            "min_tp_r": 1.0,
            "max_tp_r": 3.0,
            "stop_level_buffer_atr": 0.1,
            "min_stop_fee_multiple": 2.0,  # ≥ 20bp（4h 止损本来就宽，基本不会卡）
            **_LOOSE_GATE,
        },
        "app": {"keeper_interval_s": 30, "signal_dedup_s": 600},
        "predictor": {"interval": "5m", "pred_len": 48},  # 48 × 5m = 4 小时；回看 400 + 48 ≤ 512
    },
}

# 推荐搭配：开会档位 → 最长持仓默认（app.toml 没写 hold 时取它；写了就按用户的）。
# 取 7×7 表里「合适」的最长一档（研究 v3，Aaron 2026-10-23 定）。
RECOMMENDED_HOLD = {
    "15m": "1h",
    "30m": "2h",
    "1h": "4h",
    "2h": "4h",
    "6h": "12h",
    "12h": "24h",
    "24h": "24h",
}

# 分析师看的 K 线（周期 × 根数）：短持仓看 15m / 1h / 4h，长持仓看 1h / 4h / 1d（研究 v3）。
_KL_SHORT = ("15m x 96", "1h x 72", "4h x 60")
_KL_LONG = ("1h x 72", "4h x 90", "1d x 60")
ANALYST_KLINES = {h: _KL_SHORT for h in ("30m", "1h", "2h")} | {
    h: _KL_LONG for h in ("4h", "8h", "12h", "24h")
}
# 关键位的摆动 / 成交密集区从哪个周期的 K 线找（昨日 / 7 日高低点照旧用日线）。
LEVEL_INTERVAL = {
    "30m": "15m",
    "1h": "15m",
    "2h": "15m",
    "4h": "1h",
    "8h": "1h",
    "12h": "4h",
    "24h": "4h",
}


# 出卡提示词里建议的止损倍数范围（窄—宽，中间 = 随机入场被扫约 35%；研究 v3，按 stop_atr 口径）
STOP_HINT = {
    "30m": (0.55, 0.9),
    "1h": (0.8, 1.25),
    "2h": (1.15, 1.85),
    "4h": (0.85, 1.35),
    "8h": (1.05, 1.65),
    "12h": (1.1, 1.7),
    "24h": (1.1, 1.75),
}


# 各最长持仓档 BTC 止损约多宽（%，研究 v3：2h ≈ 0.45、4h ≈ 0.64，其余按 √时间推）；只给设置页显示
BTC_STOP_PCT = {"30m": 0.23, "1h": 0.32, "2h": 0.45, "4h": 0.64, "8h": 0.9, "12h": 1.1, "24h": 1.6}


def stop_hint(hold: str) -> tuple[float, float]:
    return STOP_HINT.get(hold, STOP_HINT[DEFAULT_HOLD])


def analyst_klines(hold: str) -> tuple[str, ...]:
    return ANALYST_KLINES.get(hold, _KL_SHORT)


def level_interval(hold: str) -> str:
    return LEVEL_INTERVAL.get(hold, "15m")


HOLD.update(
    {
        # 长持仓 3 档（研究 2026-10-23 18:40 补）。patrol_*：山寨中档止损 1.6–3.5%，
        # 按 2% 全平会误杀，所以长档放宽。
        "8h": {
            "fast_loop": {
                "time_limit_s": 28800,
                "atr_period_min": 30,
                "min_stop_atr": 0.8,
                "max_stop_atr": 2.65,
                "min_tp_r": 1.0,
                "max_tp_r": 3.0,
                "stop_level_buffer_atr": 0.1,
                "min_stop_fee_multiple": 2.0,
                "funding_against_max_bp": 5.0,
                "patrol_shock_pct_1m": 2.5,
                "patrol_alert_pct_1m": 1.2,
                **_LOOSE_GATE,
            },
            "app": {"keeper_interval_s": 45, "signal_dedup_s": 600},
            "predictor": {"interval": "15m", "pred_len": 32},  # 32 × 15m = 8 小时
        },
        "12h": {
            "fast_loop": {
                "time_limit_s": 43200,
                "atr_period_min": 60,
                "min_stop_atr": 0.8,
                "max_stop_atr": 2.75,
                "min_tp_r": 1.0,
                "max_tp_r": 3.0,
                "stop_level_buffer_atr": 0.1,
                "min_stop_fee_multiple": 2.0,
                "funding_against_max_bp": 5.0,
                "patrol_shock_pct_1m": 3.0,
                "patrol_alert_pct_1m": 1.5,
                **_LOOSE_GATE,
            },
            "app": {"keeper_interval_s": 60, "signal_dedup_s": 600},
            "predictor": {"interval": "15m", "pred_len": 48},
        },
        "24h": {
            "fast_loop": {
                "time_limit_s": 86400,
                "atr_period_min": 240,  # 4 小时 ATR，启动回填 3840 根 1 分钟线（分页拉）
                "min_stop_atr": 0.85,
                "max_stop_atr": 2.8,
                "min_tp_r": 1.0,
                "max_tp_r": 3.0,
                "stop_level_buffer_atr": 0.1,
                "min_stop_fee_multiple": 2.0,
                "funding_against_max_bp": 5.0,
                "patrol_shock_pct_1m": 4.0,
                "patrol_alert_pct_1m": 2.0,
                **_LOOSE_GATE,
            },
            "app": {"keeper_interval_s": 90, "signal_dedup_s": 600},
            "predictor": {"interval": "1h", "pred_len": 24},
        },
    }
)


# K 线表定稿（研究 v3，12 个月数据，2026-10-23）：止损 ATR = √(短周期 ATR × 长周期 ATR) × 曼谷
# 时段系数（入场那一刻查一次）；min/max_stop_atr、止盈 1–3 R 已按新尺度改；7 天下限关掉
# （atr_floor_7d 默认 0），止损不再贴关键位（stop_level_snap 默认关）。24h 档只用 4 小时 ATR、
# 不乘时段系数。系数表每月用最近 9 个月 5 个币重算一次。
_LONG_ATR = {"30m": 60, "1h": 60, "2h": 60, "4h": 240, "8h": 240, "12h": 240, "24h": 0}
# 曼谷 0–23 点；工作日 / 周六周日（曼谷日期）
# fmt: off
_SESSION: dict[str, tuple[list[float], list[float]]] = {
    "30m": (
        [1.10, 1.01, 0.97, 0.97, 0.95, 1.03, 0.90, 1.07, 1.09, 1.00, 0.89, 0.93,
         0.93, 0.93, 0.97, 1.00, 0.96, 0.88, 0.93, 1.08, 1.37, 1.56, 1.39, 1.16],
        [0.97, 0.93, 0.82, 0.88, 0.87, 0.91, 0.78, 0.90, 0.93, 0.94, 0.82, 0.90,
         0.86, 0.93, 0.94, 0.97, 0.96, 0.88, 0.90, 1.04, 1.11, 1.21, 1.08, 1.12],
    ),
    "1h": (
        [1.08, 0.98, 0.97, 0.94, 0.97, 1.00, 0.93, 1.08, 1.07, 0.99, 0.88, 0.96,
         0.96, 0.93, 0.96, 1.00, 0.93, 0.89, 0.98, 1.10, 1.50, 1.74, 1.31, 1.11],
        [0.94, 0.87, 0.78, 0.85, 0.87, 0.86, 0.80, 0.87, 0.90, 0.90, 0.81, 0.87,
         0.85, 0.92, 0.94, 1.00, 0.99, 0.93, 0.92, 1.05, 1.18, 1.20, 1.09, 1.04],
    ),
    "2h": (
        [1.01, 0.94, 0.92, 0.95, 0.97, 0.96, 1.01, 1.06, 1.02, 0.94, 0.92, 0.98,
         0.94, 0.94, 0.97, 0.98, 0.89, 0.95, 1.04, 1.34, 1.75, 1.62, 1.22, 1.07],
        [0.89, 0.78, 0.78, 0.82, 0.86, 0.83, 0.81, 0.88, 0.86, 0.86, 0.83, 0.82,
         0.89, 0.95, 0.99, 0.98, 0.93, 0.92, 1.02, 1.21, 1.23, 1.16, 1.02, 0.98],
    ),
    "4h": (
        [1.01, 0.96, 0.96, 0.94, 0.98, 0.99, 0.99, 1.03, 1.00, 0.95, 0.91, 0.93,
         0.93, 0.94, 0.94, 0.95, 0.95, 1.14, 1.46, 1.68, 1.76, 1.60, 1.28, 1.10],
        [0.76, 0.73, 0.76, 0.74, 0.74, 0.74, 0.73, 0.76, 0.77, 0.76, 0.75, 0.79,
         0.84, 0.86, 0.83, 0.79, 0.85, 0.99, 1.10, 1.13, 1.07, 1.02, 0.95, 0.88],
    ),
    "8h": (
        [1.04, 1.01, 1.00, 0.97, 0.97, 0.97, 0.95, 0.94, 0.91, 0.88, 0.87, 0.89,
         0.90, 0.97, 1.13, 1.25, 1.34, 1.40, 1.44, 1.47, 1.42, 1.30, 1.13, 0.98],
        [0.73, 0.70, 0.69, 0.69, 0.69, 0.69, 0.68, 0.71, 0.75, 0.74, 0.74, 0.73,
         0.76, 0.80, 0.90, 0.95, 0.96, 0.95, 0.96, 0.95, 0.92, 0.97, 1.00, 1.13],
    ),
    "12h": (
        [1.02, 1.00, 0.98, 0.95, 0.95, 0.94, 0.93, 0.91, 0.89, 0.95, 1.01, 1.08,
         1.13, 1.17, 1.20, 1.23, 1.23, 1.22, 1.22, 1.22, 1.22, 1.16, 1.03, 0.92],
        [0.69, 0.65, 0.64, 0.66, 0.67, 0.66, 0.65, 0.64, 0.65, 0.70, 0.76, 0.81,
         0.81, 0.81, 0.80, 0.81, 0.83, 0.91, 0.97, 1.08, 1.10, 1.13, 1.11, 1.11],
    ),
}
# fmt: on
for _name, _h in HOLD.items():
    _h["fast_loop"]["atr_long_period_min"] = _LONG_ATR[_name]
    _h["fast_loop"]["session_factor"] = _name in _SESSION
    if _name in _SESSION:
        _wd, _we = _SESSION[_name]
        _h["fast_loop"]["session_factor_weekday"] = _wd
        _h["fast_loop"]["session_factor_weekend"] = _we

# 滑点上限（risk.toml max_slippage_bps，只拦不利方向）的推荐值：约止损距离的 1/4。按 BTC 止损中位数
# 估（研究 v3：2h ≈ 45bp、4h ≈ 64bp，其余按 √时间推），山寨止损更宽，可以再放。只给推荐，不拦。
SLIPPAGE_BPS = {"30m": 6, "1h": 8, "2h": 11, "4h": 16, "8h": 22, "12h": 28, "24h": 40}

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
