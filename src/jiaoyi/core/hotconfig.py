"""配置热生效：改了设置文件，运行中的主程序在下一个空档自己换上，不用重启。

做法：主程序每隔几秒看一眼 risk.toml / app.toml / slow.toml 的修改时间，变了就先整份读出来、
按启动时一样的规则校验；读坏了继续用旧的（面板设置页会显示错误）。校验过了，只把下面
``HOT_*`` 里的字段改到正在用的那几个配置对象上（部件手里拿的就是这几个对象，下次读就是新值）；
有些部件在启动时把值抄进了自己的变量，由 ``App.reload_config`` 通知它们重新算。
换配置只在事件循环的空档做（没有 ``await`` 夹在中间），一笔单不会前半截用旧的、后半截用新的。

不在名单里的字段（运行模式、密钥、数据目录、档位、ATR 周期、止损 / 止盈的 ATR 倍数、
拍板模型 ……）启动时就被抄进了别处，换不了，仍然要重启；面板对这些照旧显示「待重启」。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from jiaoyi.alerting.kinds import KINDS, field_of

# 守卫 / 开仓前的算法每次都从配置对象里现读；日亏熔断的线由 App 另外通知
HOT_RISK: frozenset[str] = frozenset(
    {
        "risk_per_trade_pct",
        "max_leverage",
        "max_total_exposure_x",
        "category_exposure_x",
        "daily_loss_halt_pct",
        "max_notional_per_trade_x",
        "max_notional_per_trade_usdt",
        "max_open_positions",
        "lifeboat_stop_multiplier",
        "max_slippage_bps",
        "min_tp_fee_multiple",
    }
)

HOT_FAST: frozenset[str] = frozenset(
    {
        # 拍板门槛 / 仓位
        "min_p_genuine",
        "min_p_take",
        "min_p_direction",
        "max_p_toxic",
        "predictor_weight",
        "predictor_nudge_mode",
        "gate_questions",
        "gate_record_only",
        "signal_max_age_s",
        "size_fraction_s",
        "size_fraction_m",
        "max_slippage_stop_frac",
        "max_spread_stop_frac",
        "card_caps_binding",
        "card_range_clamp",
        "max_notional_book_frac",
        "book_depth_levels",
        "margin_safety_pct",
        "session_factor",
        "session_factor_weekday",
        "session_factor_weekend",
        "atr_floor_7d",
        "stop_level_snap",
        "stop_level_buffer_atr",
        "min_stop_fee_multiple",
        "stop_floor_widen",
        "funding_against_max_bp",
        "new_listing_block_h",
        "adl_high_block",
        # 管家 / 回吐保护 / 巡逻兵
        "keeper_close_p",
        "keeper_close_p_strong",
        "keeper_confirmations",
        "giveback_on",
        "giveback_close_pct",
        "giveback_arm_r",
        "patrol_shock_pct_1m",
        "patrol_alert_pct_1m",
        "patrol_shock_stop_mult",
        "patrol_alert_stop_mult",
        # 移动止损 / 保本 / 挂单回踩
        "breakeven_on",
        "breakeven_trigger_r",
        "trailing_on",
        "trail_start_r",
        "trail_distance_r",
        "entry_zone_only",
        "entry_limit_pullback",
        "entry_limit_pullback_live",
        "entry_limit_offset_stop",
        "entry_limit_wait_min",
        "entry_limit_chase",
        # 哨兵阈值 / 开关
        "volume_surge_live",
        "session_cme_open",
        "session_cme_open_live",
        "breakout_vol_ratio",
        "surge_vol_ratio",
        "surge_taker_ratio",
        "burst_10s_on",
        "burst_10s_ratio",
        "burst_min_1m_ratio",
        "liq_min_usdt_1m",
        "liq_ratio",
        "liq_drop_pct_5m",
        "crowded_funding_rate",
        "session_window_min",
        "session_vol_ratio",
        "oi_follow_on",
        "oi_min_pct",
        "oi_price_z",
        "oi_follow_live",
        "top_crowd_on",
        "top_crowd_min_pct",
        "top_crowd_live",
        "btc_lead_on",
        "btc_lead_z",
        "btc_lead_live",
        "regime_flip_on",
        "regime_flip_live",
        "regime_flip_breadth",
        "regime_flip_btc_pct",
        "regime_flip_confirm",
        "regime_flip_min_gap_min",
        "regime_flip_min_symbols",
        "regime_flip_check_min",
        "regime_flip_suspect_log_min",
        "regime_flip_price_max_age_min",
        # 反向信号
        "against_signal_on",
        "against_dedup_s",
        "against_grace_s",
        "against_recheck_cooldown_s",
        "against_strong_breakout_vol_ratio",
        "against_loss_stop_frac",
        "against_meeting_count",
        "against_meeting_window_s",
        "against_meeting_gap_s",
    }
)

HOT_APP: frozenset[str] = frozenset(
    {
        "signal_dedup_s",
        "trade_notices",
        "credit_low_floor_usd",
        "credit_resend_hours",
        *(field_of(k) for k in KINDS),  # Telegram 告警类别开关
    }
)

# 启动时就用 hold / meeting_interval / mode 给没写的键垫默认值；它们没换之前，重读时仍按
# 正在跑的这几个垫，免得「档位默认」悄悄变了热生效项的值
PROFILE_KEYS = ("mode", "hold", "meeting_interval")


def hot_keys() -> list[str]:
    """面板用：热生效的设置键，``段.键`` 形式；``category.*`` 是类别敞口那一段整段。"""
    return sorted(
        [f"risk.{k}" for k in HOT_RISK]
        + [f"fast_loop.{k}" for k in HOT_FAST]
        + [f"app.{k}" for k in HOT_APP]
        + ["category.*"]
    )


def restart_fingerprint(risk: BaseModel, app: BaseModel, slow: BaseModel) -> str:
    """只算「必须重启才生效」的那部分配置的指纹（热生效的字段不进来）。

    主循环写进 runtime.json，面板拿文件现算的指纹对比：只改了热生效的项，两边仍然一致。"""
    app_exclude: dict[str, Any] = {k: True for k in HOT_APP}
    app_exclude["fast_loop"] = {k: True for k in HOT_FAST}
    parts = [
        risk.model_dump(mode="json", exclude={k: True for k in HOT_RISK}),
        app.model_dump(mode="json", exclude=app_exclude),
        slow.model_dump(mode="json"),
    ]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def patch_inplace(
    target: BaseModel, new: BaseModel, names: Iterable[str]
) -> dict[str, tuple[Any, Any]]:
    """把 ``new`` 里 ``names`` 这些字段的值写到 ``target`` 上（配置对象是冻结的，这里绕过冻结）。

    返回 ``{字段: (旧值, 新值)}``，只含真变了的。"""
    changed: dict[str, tuple[Any, Any]] = {}
    for name in names:
        old, val = getattr(target, name), getattr(new, name)
        if old != val:
            object.__setattr__(target, name, val)
            changed[name] = (old, val)
    return changed


@dataclass(frozen=True)
class ConfigFiles:
    risk: Path
    app: Path
    slow: Path

    def stamp(self) -> tuple[tuple[int, int] | None, ...]:
        out: list[tuple[int, int] | None] = []
        for p in (self.risk, self.app, self.slow):
            try:
                st = p.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)


def describe(changes: Mapping[str, tuple[Any, Any]]) -> str:
    return "；".join(f"{k} {a} → {b}" for k, (a, b) in sorted(changes.items()))
