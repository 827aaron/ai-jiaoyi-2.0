"""配置热生效：设置文件改了，运行中的主程序不重启就换上热生效的项；其余项仍要重启。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from jiaoyi.app import App
from jiaoyi.contracts import AppConfig, RiskConfig
from jiaoyi.contracts.risk_config import FastLoopConfig
from jiaoyi.core import hotconfig
from jiaoyi.core.commands import RUNTIME_FILE, write_runtime
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.core.hotconfig import ConfigFiles, restart_fingerprint
from jiaoyi.panel import actions
from jiaoyi.slow.settings import load_slow_config
from tests.test_panel_actions import NOW, PW, _setup


def _running(tmp_path: Path) -> tuple[App, actions.ConfigPaths, Path]:
    paths, data = _setup(tmp_path)
    app = App(
        load_risk_config(paths.risk),
        load_app_config(paths.app),
        clock=lambda: NOW,
        offline=True,
        config_files=ConfigFiles(paths.risk, paths.app, paths.slow),
    )
    write_runtime(data, started_at=NOW.isoformat(), config_fingerprint="x")
    return app, paths, data


def _edit(paths: actions.ConfigPaths, data: Path, section: str, changes: dict) -> None:
    actions.update_config(paths, data, section, changes, PW, NOW)  # type: ignore[arg-type]


def test_hot_names_are_real_fields() -> None:
    assert set(RiskConfig.model_fields) >= hotconfig.HOT_RISK
    assert set(FastLoopConfig.model_fields) >= hotconfig.HOT_FAST
    assert set(AppConfig.model_fields) >= hotconfig.HOT_APP
    # 启动时抄进别处的，不能进名单
    assert not hotconfig.HOT_APP & {"mode", "hold", "meeting_interval", "data_dir", "pool_size"}
    assert not hotconfig.HOT_FAST & {"atr_period_min", "min_stop_atr", "time_limit_s", "jev_model"}
    assert not hotconfig.HOT_RISK & {"day_reset_tz", "taker_fee_bps", "max_spread_bps"}


def test_panel_only_card_score_settings_do_not_ask_for_a_restart(tmp_path: Path) -> None:
    """方向记分的两个设置只有面板用：改了不能让面板提示「重启交易程序」。"""
    app, _, _ = _running(tmp_path)
    risk, slow = app.risk_cfg, app.slow_cfg
    before = restart_fingerprint(risk, app.app_cfg, slow)
    changed = app.app_cfg.model_copy(
        update={"card_score_every_min": 0, "card_score_window_days": 7}
    )
    assert restart_fingerprint(risk, changed, slow) == before
    assert "app.card_score_every_min" in hotconfig.hot_keys()


def test_hot_edit_is_applied_in_place(tmp_path: Path) -> None:
    app, paths, data = _running(tmp_path)
    risk, fast = app.risk_cfg, app.app_cfg.fast_loop  # 部件手里拿的就是这两个对象
    assert app.reload_config() == {}  # 文件没变
    _edit(paths, data, "risk", {"max_leverage": 7, "daily_loss_halt_pct": 12.5})
    _edit(paths, data, "fast_loop", {"breakout_vol_ratio": 4.25, "giveback_close_pct": 33})
    _edit(paths, data, "app", {"signal_dedup_s": 77, "notify_system": False})
    changed = app.reload_config()
    assert {
        "risk.max_leverage",
        "risk.daily_loss_halt_pct",
        "fast_loop.breakout_vol_ratio",
        "fast_loop.giveback_close_pct",
        "app.signal_dedup_s",
        "app.notify_system",
    } <= set(changed)
    assert app.risk_cfg is risk and app.app_cfg.fast_loop is fast
    assert risk.max_leverage == 7 and fast.breakout_vol_ratio == 4.25
    assert fast.giveback_close_pct == 33 and app.app_cfg.signal_dedup_s == 77
    # 启动时抄进变量的部件被通知重新算
    assert app.m.risk.breaker._halt == 12.5  # type: ignore[attr-defined]
    assert app.m.trigger._dedup == timedelta(seconds=77)  # type: ignore[attr-defined]
    assert "system" in app.app_cfg.muted_alert_kinds
    runtime = json.loads((data / RUNTIME_FILE).read_text(encoding="utf-8"))
    assert runtime["hot_applied_at"] == NOW.isoformat()
    assert app.reload_config() == {}  # 同一份文件不重复换


def test_tp_r_cap_reaches_the_running_guard(tmp_path: Path) -> None:
    app, paths, data = _running(tmp_path)
    assert app.m.risk._fast.tp_r_cap == 0.0  # type: ignore[attr-defined]
    _edit(paths, data, "fast_loop", {"tp_r_cap": 1.0})
    assert "fast_loop.tp_r_cap" in app.reload_config()
    assert app.m.risk._fast.tp_r_cap == 1.0  # type: ignore[attr-defined]


def test_restart_only_keys_are_not_applied(tmp_path: Path) -> None:
    app, paths, data = _running(tmp_path)
    before = app.app_cfg.pool_size
    _edit(paths, data, "app", {"pool_size": before + 5})
    _edit(paths, data, "risk", {"max_spread_bps": 7})
    assert app.reload_config() == {}
    assert app.app_cfg.pool_size == before and app.risk_cfg.max_spread_bps != 7
    # 面板那边：还是「待重启」
    risk, new_app = load_risk_config(paths.risk), load_app_config(paths.app)
    write_runtime(
        data, config_fingerprint=restart_fingerprint(app.risk_cfg, app.app_cfg, app.slow_cfg)
    )
    assert actions.config_pending(paths, data) is True
    assert (
        restart_fingerprint(risk, new_app, load_slow_config(paths.slow))
        != json.loads((data / RUNTIME_FILE).read_text(encoding="utf-8"))["config_fingerprint"]
    )


def test_bad_file_keeps_old_config(tmp_path: Path) -> None:
    app, paths, _data = _running(tmp_path)
    old = app.risk_cfg.max_leverage
    paths.risk.write_text(
        paths.risk.read_text(encoding="utf-8") + "\nmax_leverage = [\n", encoding="utf-8"
    )
    assert app.reload_config() == {}
    assert app.risk_cfg.max_leverage == old
    paths.risk.write_text("max_leverage = -3\n", encoding="utf-8")  # 缺项 / 不合格
    assert app.reload_config() == {}
    assert app.risk_cfg.max_leverage == old


def test_profile_change_does_not_shift_hot_defaults(tmp_path: Path) -> None:
    """hold / meeting_interval 要重启才换；重读时仍按正在跑的档位垫默认值。"""
    app, paths, _data = _running(tmp_path)
    fast_before = app.app_cfg.fast_loop.model_dump()
    hold_before = app.app_cfg.hold
    new_hold = "1h" if hold_before != "1h" else "4h"
    paths.app.write_text(
        paths.app.read_text(encoding="utf-8").replace(
            f'hold = "{hold_before}"', f'hold = "{new_hold}"'
        )
        + f"\n# touched {new_hold}\n",
        encoding="utf-8",
    )
    app.reload_config()
    assert app.app_cfg.hold == hold_before
    hot_now = {
        k: v for k, v in app.app_cfg.fast_loop.model_dump().items() if k in hotconfig.HOT_FAST
    }
    assert hot_now == {k: v for k, v in fast_before.items() if k in hotconfig.HOT_FAST}


def test_panel_marks_applied_hot_changes(tmp_path: Path) -> None:
    app, paths, data = _running(tmp_path)
    ctl = actions.control_state(data, paths)
    assert "risk.max_leverage" in ctl.hot_keys and "category.*" in ctl.hot_keys
    assert "app.pool_size" not in ctl.hot_keys
    _edit(paths, data, "risk", {"max_leverage": 6})
    app.reload_config()
    assert actions.control_state(data, paths).hot_applied_at == NOW.isoformat()


def test_keeper_assistant_settings_edit_on_panel_and_apply_without_restart(
    tmp_path: Path,
) -> None:
    app, paths, data = _running(tmp_path)
    changes = {
        "keeper_assistant_on": True,
        "keeper_assistant_close": True,
        "keeper_assistant_b_record_only": False,
        "keeper_assistant_flip_confirm": True,
        "keeper_assistant_flip_neutral": True,
        "card_flip_confirm_by": "assistant",
        "card_neutral_flip": "ignore",
        "card_ignore_max_age_min": 240.0,
        "card_flip_assistant_fallback_min": 120.0,
        "keeper_assistant_b_model": "microsoft/decision-1",
        "keeper_assistant_gap_s": 60,
        "keeper_assistant_horizon": "1 小时",
        "keeper_assistant_concurrency": 1,
    }
    _edit(paths, data, "fast_loop", changes)
    app.reload_config()
    for key, value in changes.items():
        assert getattr(app.app_cfg.fast_loop, key) == value
