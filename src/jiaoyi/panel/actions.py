"""面板的写操作：给主循环发指令、改配置文件。面板自己不执行任何交易动作。

- 指令走 ``jiaoyi.core.commands``：面板写 ``data/panel_commands/<id>.json``，主循环每秒扫一次执行，
  结果写到 ``done/<id>.json``。暂停、全部平仓、平单笔这类“收手”的动作不要密码；
  恢复开仓、人工解封会重新放开风险，要面板密码。
- 改配置一律要面板密码（硬上限是 Aaron 的规定，其余顺带）。先在内存里改 TOML 文本、
  用契约整份校验，过了才写；写前留备份，每项改动记一行 ``data/config_changes.jsonl``。
  热生效名单（``core/hotconfig.py``）里的项主循环几秒内自己换上，其余重启后生效
  （总览按 ``data/runtime.json`` 的配置指纹提示）。
- 面板密码复用实盘门槛的同一份哈希（``live_gate.json``），连错 5 次锁 15 分钟，状态落盘。
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import secrets
import shutil
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from jiaoyi.alerting.kinds import KINDS, field_of
from jiaoyi.contracts import AppConfig, Category, RiskConfig
from jiaoyi.contracts.risk_config import FastLoopConfig, PredictorConfig
from jiaoyi.core import profiles
from jiaoyi.core.commands import (
    RUNTIME_FILE,
    CommandKind,
    CommandResult,
    PanelCommand,
    config_fingerprint,
    read_paused,
    write_command,
)
from jiaoyi.core.config import ConfigError, load_app_config, load_risk_config, with_profile
from jiaoyi.core.hotconfig import hot_keys, restart_fingerprint
from jiaoyi.core.pool_overrides import (
    CATEGORY_ACTIONS,
    POOL_ACTIONS,
    SYMBOL_RE,
    parse_category_command,
)
from jiaoyi.core.secrets import key_needs
from jiaoyi.execution.live_gate import LiveGate, LiveGateError  # 只用密码校验；不碰交易所
from jiaoyi.panel.files import read_live_gate
from jiaoyi.panel.overview import Control
from jiaoyi.slow.settings import (
    BudgetConfig,
    LlmConfig,
    MarketDeskSection,
    MeetingConfig,
    ScheduleConfig,
    ScreenerSection,
    SlowConfig,
    TiersSection,
    load_slow_config,
    market_desk_conflict,
)
from jiaoyi.slow.settings import with_profile as slow_with_profile

CHANGES_FILE = "config_changes.jsonl"
COMMAND_TTL = timedelta(seconds=60)
# 会让系统多做交易的指令要密码（固定、手动加币、取消拉黑）；拉黑、取消固定只会更保守，不要
NEEDS_PASSWORD: frozenset[CommandKind] = frozenset(
    {
        "resume",
        "unblock_symbol",
        "restart",
        "pin_symbol",
        "add_symbol",
        "unblacklist_symbol",
        # 类别开关 / 每类上限：同一条指令既可能放宽也可能收紧，一律要密码
        "set_category",
        "set_category_cap",
        # 人工放行 = 绕过风控员的一道过滤去开仓，要密码；重新检查只看结果、不下单
        "release_signal",
    }
)

Section = Literal[
    "risk",
    "category",
    "app",
    "fast_loop",
    "predictor",
    "schedule",
    "llm",
    "budget",
    "screener",
    "tiers",
    "market_desk",
    "meeting",
]
_SLOW_SECTIONS = ("schedule", "llm", "budget", "screener", "tiers", "market_desk", "meeting")
# [market_desk] 其余项（模式、模型、节奏、采样）改了要重启、影响大盘方向，只在终端改
DESK_EDITABLE = frozenset(
    {
        "band_sigma",
        "prior_mode",
        "momentum_guidance",
        "aggregation",
        "call_p_min",
        "call_edge_min",
        "publish_min_gap_min",
        "publish_intra",
        "keeper_intra",
        "intra_refresh_s",
    }
)
# [meeting] 里网页上能改的开关（偏空整改、输入新鲜度、教训口径）；模型 / 通道 / 分析师名单 /
# 辩论轮数 / 回答长度不在网页改。
# analyst_klines 是 "周期 x 根数" 的字符串列表，网页没有对应的输入，也不放。
MEETING_EDITABLE = (
    "lessons_scoring",
    "debate_first",
    "funding_label",
    "bear_bull_symmetry",
    "crowd_ratio_mode",
    "bias_fix_ab",
    "snapshot_interval",
    "snapshot_label",
    "live_price_line",
    "stale_on_arrival",
    "stale_on_arrival_action",
    "input_time_labels",
    "market_table_rolling",
    "latest_market_regime",
    "event_brief_refresh",
)
# 网页上能改的运行配置；mode（走 go-live）、data_dir、模拟环境、Jev 通道和模型版本不在网页改
APP_EDITABLE = frozenset(
    {
        "credit_low_floor_usd",
        "credit_resend_hours",
        *(field_of(k) for k in KINDS),  # Telegram 告警类别开关（alerting/kinds.py）
        "predictor_enabled",
        "signal_dedup_s",
        "jev_timeout_ms",
        "jev_max_concurrent",
        "keeper_interval_s",
        "reconcile_interval_s",
        "reconcile_opening_grace_s",
        "reconcile_closing_grace_s",
        "protection_stale_s",
        "protection_leg_order",
        "hosted_close_mode",
        "slow_loop_interval_min",
        "screener_interval_min",
        "pool_size",
        "card_pick_max",
        "reconciler_close_unknown_positions",
        "meeting_interval",
        "hold",
    }
)

SIGNAL_ACTIONS = frozenset({"recheck_signal", "release_signal"})

BUDGET_EDITABLE = "daily_cost_limit_usd"
"""花费段在设置页只能改日上限。"""

# 面板的段 → 档位里对应的段（core/profiles.py）。这些段里档位给默认值的键可以“清掉”回到档位默认。
_PROFILE_SECTION: dict[str, profiles.Section] = {
    "app": "app",
    "fast_loop": "fast_loop",
    "predictor": "predictor",
    "schedule": "slow.schedule",
    "budget": "slow.budget",
}


class ActionError(Exception):
    """给人看的中文；HTTP 400 原样返回。"""


def check_password(data_dir: str | Path, password: str) -> None:
    """每次新建 LiveGate 读最新状态（命令行 go-live 也在写同一个文件）。"""
    try:
        LiveGate(Path(data_dir) / "live_gate.json")._verify(password)
    except LiveGateError as e:
        raise ActionError(str(e)) from e


# ---------------------------------------------------------------- 指令


def send_command(
    data_dir: str | Path,
    kind: CommandKind,
    params: dict[str, str],
    now: datetime,
    password: str | None,
) -> str:
    if kind in NEEDS_PASSWORD:
        check_password(data_dir, password or "")
    if kind == "close_position" and not params.get("position_id"):
        raise ActionError("平单笔要给 position_id")
    if kind == "unblock_symbol" and not params.get("symbol"):
        raise ActionError("解封要给币种")
    if kind in SIGNAL_ACTIONS:
        if not 0 < len(params.get("signal_id", "")) <= 128 or not SYMBOL_RE.match(
            params.get("symbol", "")
        ):
            raise ActionError("要给信号编号和币种代码，例如 BTCUSDT")
        params = {k: params[k] for k in ("signal_id", "symbol")}
    if kind in POOL_ACTIONS and not SYMBOL_RE.match(params.get("symbol", "")):
        raise ActionError("选币页操作要给币种代码，例如 BTCUSDT")
    if kind in CATEGORY_ACTIONS:
        try:
            parse_category_command(kind, params)
        except ValueError as e:
            raise ActionError(str(e)) from e
    cmd = PanelCommand(
        id=f"p{now.strftime('%Y%m%d%H%M%S')}{secrets.token_hex(4)}",
        ts=now,
        expires_at=now + COMMAND_TTL,
        kind=kind,
        params=params,
        by="panel",
    )
    write_command(data_dir, cmd)
    return cmd.id


def command_status(data_dir: str | Path, command_id: str) -> CommandResult | None:
    """主循环还没处理返回 None。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", command_id):
        raise ActionError("指令 id 不对")
    path = Path(data_dir) / "panel_commands" / "done" / f"{command_id}.json"
    try:
        return CommandResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def control_state(data_dir: str | Path, paths: ConfigPaths | None) -> Control:
    paused = read_paused(data_dir)
    password_set, _ = read_live_gate(Path(data_dir))
    return Control(
        paused=paused is not None,
        paused_corrupt=bool(paused and paused.get("corrupt")),
        paused_since=str(paused["since"]) if paused and paused.get("since") else None,
        paused_reason=str(paused["reason"]) if paused and paused.get("reason") else None,
        config_pending=config_pending(paths, data_dir) if paths else None,
        pw_set=password_set,
        writable=paths is not None,
        started_at=runtime_started_at(data_dir),
        hot_applied_at=_runtime_field(data_dir, "hot_applied_at"),
        hot_keys=hot_keys(),
        paper_only_ignored=paper_only_ignored(paths) if paths else [],
    )


def paper_only_ignored(paths: ConfigPaths) -> list[str]:
    """app.toml 里开着、但不是模拟盘所以主程序当关的开关。"""
    raw = _raw(paths.app)
    if raw.get("mode", "paper") == "paper":
        return []
    fast = raw.get("fast_loop")
    fast = fast if isinstance(fast, dict) else {}
    return [name for key, name in PAPER_ONLY_SWITCHES.items() if fast.get(key) is True]


def _runtime_field(data_dir: str | Path, key: str) -> str | None:
    try:
        runtime = json.loads((Path(data_dir) / RUNTIME_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    at = runtime.get(key) if isinstance(runtime, dict) else None
    return at if isinstance(at, str) else None


def runtime_started_at(data_dir: str | Path) -> str | None:
    return _runtime_field(data_dir, "started_at")


# ---------------------------------------------------------------- 配置


class ConfigPaths(BaseModel):
    risk: Path
    app: Path
    slow: Path


def load_configs(paths: ConfigPaths) -> tuple[RiskConfig, AppConfig, SlowConfig]:
    """和主循环同一套读法（slow.toml 缺了用默认值），指纹才对得上。"""
    app = load_app_config(paths.app)
    return load_risk_config(paths.risk), app, load_slow_config(paths.slow, app.profile)


class LiveConfigs:
    """面板页面用的风控 / 运行配置：文件变了（修改时间或大小）就重读，不用重启面板。

    主程序“重启生效”后面板马上显示新上限（例如离熔断按新的日亏百分比算）。
    文件读坏了沿用上一份能用的，等下一次改动再试。
    """

    def __init__(self, risk: RiskConfig, app: AppConfig, paths: ConfigPaths | None) -> None:
        self.risk, self.app = risk, app
        self._paths = paths
        self._stamp = self._stat() if paths else None

    def _stat(self) -> tuple[tuple[int, int] | None, ...]:
        assert self._paths is not None
        out: list[tuple[int, int] | None] = []
        for p in (self._paths.risk, self._paths.app, self._paths.slow):
            try:
                st = p.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def get(self) -> tuple[RiskConfig, AppConfig]:
        if self._paths is not None and (stamp := self._stat()) != self._stamp:
            self._stamp = stamp
            # 读坏了沿用上一份；设置页会把错误显示出来
            with contextlib.suppress(ConfigError):
                self.risk = load_risk_config(self._paths.risk)
            with contextlib.suppress(ConfigError):
                self.app = load_app_config(self._paths.app)
        return self.risk, self.app


def config_pending(paths: ConfigPaths, data_dir: str | Path) -> bool | None:
    """配置文件和主循环启动时加载的不一致（改了还没重启）。主循环没跑过或读不出来返回 None。"""
    try:
        runtime = json.loads((Path(data_dir) / RUNTIME_FILE).read_text(encoding="utf-8"))
        risk, app, slow = load_configs(paths)
    except (OSError, ValueError, ConfigError):
        return None
    loaded = runtime.get("config_fingerprint") if isinstance(runtime, dict) else None
    if not isinstance(loaded, str):
        return None
    return loaded != restart_fingerprint(risk, app, slow)


# 档位给默认值的全部段（面板段 → 档位段）
_PROFILED: tuple[tuple[Section, profiles.Section], ...] = (
    ("app", "app"),
    ("fast_loop", "fast_loop"),
    ("predictor", "predictor"),
    ("schedule", "slow.schedule"),
    ("budget", "slow.budget"),
)
_PROFILE_MODEL: dict[profiles.Section, type[BaseModel]] = {
    "app": AppConfig,
    "fast_loop": FastLoopConfig,
    "predictor": PredictorConfig,
    "slow.schedule": ScheduleConfig,
    "slow.budget": BudgetConfig,
}


class PinItem(BaseModel):
    """配置文件里写死的一个档位键：切到某档位后仍按文件里的值。"""

    section: str
    key: str
    value: object
    """文件里的值。"""
    profile_default: object
    """那个档位的默认值（档位没给就是程序默认）。"""


class HoldSpec(BaseModel):
    """最长持仓一档：多少秒、止损按几分钟 K 线 ATR。"""

    time_limit_s: int
    atr_period_min: int


class ProfileInfo(BaseModel):
    """设置页的档位卡片：开会间隔档、最长持仓档，以及配置文件里写死、不随档位变的键。"""

    current: str
    """开会间隔档（meeting_interval）。"""
    choices: list[str]
    paper_only: list[str]
    hold: str = profiles.DEFAULT_HOLD
    """现在生效的最长持仓档。"""
    hold_custom: bool = False
    """True = 用户自己定的（写了 hold 或 time_limit_s）；False = 跟开会档位的推荐搭配。"""
    hold_choices: list[str] = []
    hold_paper_only: list[str] = []
    hold_recommended: dict[str, str] = {}
    """开会间隔档 → 推荐最长持仓档。"""
    hold_specs: dict[str, HoldSpec] = {}
    pinned: dict[str, dict[str, object]]
    """段 → {键: 当前档位的默认值}：这些键在配置文件里写了值，切档位也不会跟着变。"""
    pins: dict[str, list[PinItem]] = {}
    """档位 → 切到它时会压过档位默认的写死项（任何档位给默认值的键，只要文件里写了就算）。"""
    defaults: dict[str, dict[str, dict[str, object]]] = {}
    """开会档位 → 段 → {键: 换到这档后的默认值}（最长持仓按换档后的算；paper 盖临时拍板门槛）。"""
    stop_pct: dict[str, float] = {}
    """最长持仓档 → BTC 止损约多宽（%），只显示。"""


def _hold_for(raw_app: dict[str, Any], profile: str) -> str:
    """开会间隔切到 ``profile`` 时的最长持仓档：自定义的不变，跟档位的换成推荐搭配。"""
    return profiles.hold_of({**raw_app, "meeting_interval": profile})


def _profile_keys(psec: profiles.Section) -> list[str]:
    """档位给默认值的键：每个开会档 × 每个最长持仓档都算（有的键只有长持仓档给，如巡逻兵线）。"""
    return sorted(
        {
            k
            for p in profiles.PROFILES
            for h in profiles.HOLDS
            for k in profiles.defaults(p, psec, h)
        }
    )


def _tier_defaults(raw_app: dict[str, Any], profile: str) -> dict[str, dict[str, object]]:
    """换到 ``profile`` 后各段档位键的值：档位没给的键用程序默认，每档键齐全，前端好比。"""
    hold = _hold_for(raw_app, profile)
    out: dict[str, dict[str, object]] = {}
    for section, psec in _PROFILED:
        base = profiles.defaults(profile, psec, hold)
        if psec == "fast_loop" and raw_app.get("mode") == "paper":
            base = {**base, **profiles.PAPER_GATE}  # 和 core/config.py 一样
        fields = _PROFILE_MODEL[psec].model_fields
        row: dict[str, object] = {}
        for k in _profile_keys(psec):
            if k in base:
                row[k] = base[k]
            elif (f := fields.get(k)) is not None and not f.is_required():
                row[k] = f.get_default(call_default_factory=True)
        out[section] = row
    return out


def _file_table(paths: ConfigPaths, section: Section, raws: dict[str, dict[str, Any]]) -> Any:
    file, table = _WHERE[section]
    raw = raws.setdefault(file, _raw(getattr(paths, file)))
    return raw.get(table, {}) if table else raw


def profile_pins(paths: ConfigPaths, profile: str) -> list[PinItem]:
    """切到 ``profile`` 后哪些键还按配置文件里的值：文件值 → 档位默认值。"""
    raws: dict[str, dict[str, Any]] = {}
    out: list[PinItem] = []
    hold = _hold_for(_raw(paths.app), profile)
    for section, psec in _PROFILED:
        raw = _file_table(paths, section, raws)
        if not isinstance(raw, dict):
            continue
        base = profiles.defaults(profile, psec, hold)
        fields = _PROFILE_MODEL[psec].model_fields
        for k in _profile_keys(psec):
            if k not in raw:
                continue
            if k in base:
                default = base[k]
            else:
                f = fields.get(k)
                default = None if f is None or f.is_required() else f.default
            out.append(PinItem(section=section, key=k, value=raw[k], profile_default=default))
    return out


def clear_profile_pins(
    paths: ConfigPaths, data_dir: str | Path, password: str, now: datetime
) -> list[dict[str, Any]]:
    """删掉配置文件里所有档位键的行，之后完全按档位走。两个文件都校验过才写；各自留备份。"""
    check_password(data_dir, password)
    todo: list[tuple[str, Path, str, list[dict[str, Any]]]] = []
    for file in ("app", "slow"):
        path: Path = getattr(paths, file)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            current = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ActionError(f"{path.name} 现在就不是合法 TOML，先在终端修好：{e}") from e
        log: list[dict[str, Any]] = []
        for section, psec in _PROFILED:
            where, table = _WHERE[section]
            if where != file:
                continue
            raw = current.get(table, {}) if table else current
            for k in _profile_keys(psec):
                if isinstance(raw, dict) and k in raw:
                    text = set_toml_value(text, table, k, None)
                    log.append({"section": section, "key": k, "old": raw[k], "new": None})
        if log:
            todo.append((file, path, text, log))
    if not todo:
        raise ActionError("配置文件里没有写死的档位项，已经完全按档位走")
    for file, _, text, _ in todo:
        _validate("app" if file == "app" else "schedule", text, paths)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    changed: list[dict[str, Any]] = []
    with (Path(data_dir) / CHANGES_FILE).open("a", encoding="utf-8") as f:
        for _, path, text, log in todo:
            shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            for item in log:
                row = {"ts": now.isoformat(), "file": path.name, **item}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            changed += log
    return changed


def profile_info(paths: ConfigPaths) -> ProfileInfo:
    raw_app = _raw(paths.app)
    raw_slow = _raw(paths.slow)
    current = profiles.profile_of(raw_app)
    hold = profiles.hold_of(raw_app)
    raws: dict[str, Any] = {
        "app": raw_app,
        "fast_loop": raw_app.get("fast_loop", {}),
        "predictor": raw_app.get("predictor", {}),
        "schedule": raw_slow.get("schedule", {}),
        "budget": raw_slow.get("budget", {}),
    }
    tiers = {p: _tier_defaults(raw_app, p) for p in profiles.PROFILES}
    pinned: dict[str, dict[str, object]] = {}
    for section in _PROFILE_SECTION:
        raw = raws[section] if isinstance(raws[section], dict) else {}
        base = tiers[current][section]
        hit = {k: base[k] for k in base if k in raw}
        if hit:
            pinned[section] = hit
    return ProfileInfo(
        current=current,
        choices=list(profiles.PROFILES),
        paper_only=[],  # 开会间隔档没有只能 paper 的；最长持仓 30m 在 hold 里校验
        hold=hold,
        hold_custom=profiles.hold_is_custom(raw_app),
        hold_choices=list(profiles.HOLDS),
        hold_paper_only=sorted(profiles.PAPER_ONLY),
        hold_recommended=dict(profiles.RECOMMENDED_HOLD),
        hold_specs={
            h: HoldSpec(
                time_limit_s=v["fast_loop"]["time_limit_s"],
                atr_period_min=v["fast_loop"]["atr_period_min"],
            )
            for h, v in profiles.HOLD.items()
        },
        pinned=pinned,
        pins={p: profile_pins(paths, p) for p in profiles.PROFILES},
        defaults=tiers,
        stop_pct=dict(profiles.BTC_STOP_PCT),
    )


def _raw(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


Value = bool | int | float | str | list[str] | None
"""``None`` = 删掉这一行，回到默认值（只给“不写就用默认”的项）；``list`` 只给多选项。"""


@dataclass(frozen=True)
class FieldSpec:
    kind: type
    """bool / int / float / str"""
    choices: tuple[object, ...] = ()
    """``Literal`` 的可选值；空 = 不限。"""
    nullable: bool = False
    """留空 = 不写这一项（契约里本来就允许空）。"""
    profiled: bool = False
    """档位给默认值的键：留空 = 删掉这一行，回到档位默认。"""
    multi: bool = False
    """多选（``tuple[Literal, ...]``）：值是 ``choices`` 的子集，可以一个都不选。"""
    pattern: str | None = None
    """自由填写的字样（例如 ``08:00``）要符合的格式；契约里写了 pattern 才放开在网页上改。"""
    names: bool = False
    """币名名单（``list[str]``，例如主流币手动加入 / 排除）：自由填，可以一个都没有。"""


def update_config(
    paths: ConfigPaths,
    data_dir: str | Path,
    section: Section,
    changes: dict[str, Any],
    password: str,
    now: datetime,
    *,
    fields: dict[str, FieldSpec] | None = None,
) -> list[dict[str, Any]]:
    """改一组配置。先验密码；全部通过契约校验才写；返回实际改了哪些（key、old、new）。

    ``fields`` 只给首次设置向导用：放开平时不在网页上改的项（例如 jev_provider）。
    """
    check_password(data_dir, password)
    return _write_config(paths, data_dir, section, changes, now, fields)


def _write_config(
    paths: ConfigPaths,
    data_dir: str | Path,
    section: Section,
    changes: dict[str, Any],
    now: datetime,
    fields: dict[str, FieldSpec] | None,
    *,
    note: str | None = None,
) -> list[dict[str, Any]]:
    """``update_config`` 验完密码之后：校验、备份、写文件、记改动（``note`` 写进记录）。"""
    if not changes:
        raise ActionError("没有要改的项")
    if section == "app" and "profile" in changes:  # 旧面板写的 profile = 开会间隔档
        changes = {("meeting_interval" if k == "profile" else k): v for k, v in changes.items()}
    path, table, editable = _target(paths, section)
    if fields is None and section == "llm":
        # 军师团通道只在首次设置向导里改（向导会显式给 fields）；花费段设置页只能改日上限
        raise ActionError(f"{section} 段只能在首次设置里改")
    fields = editable if fields is None else fields
    exists = path.is_file()  # slow.toml 可以没有：没有就新建（其余默认值照旧）
    text = path.read_text(encoding="utf-8") if exists else ""
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ActionError(f"{path.name} 现在就不是合法 TOML，先在终端修好：{e}") from e
    olds = current.get(table, {}) if table else current
    log: list[dict[str, Any]] = []
    for key, raw in changes.items():
        if key not in fields:
            raise ActionError(f"{key} 不能在网页上改")
        if raw is None and isinstance(olds, dict) and key in olds:
            value = None  # 删掉这一行 = 回到默认（档位默认或程序默认）；必填项删了过不了下面的校验
        else:
            value = _coerce(key, raw, fields[key])
        on = section == "fast_loop" and key in PAPER_ONLY_SWITCHES and value is True
        if on and current_mode(paths) != "paper":
            raise ActionError(f"{PAPER_ONLY_SWITCHES[key]}只有模拟盘能开")
        old = olds.get(key) if isinstance(olds, dict) else None
        if old == value and isinstance(old, bool) == isinstance(value, bool):
            continue
        text = set_toml_value(text, table, key, value)
        log.append({"key": key, "old": old, "new": value})
    if not log:
        return []
    _validate(section, text, paths)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    if exists:
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    with (Path(data_dir) / CHANGES_FILE).open("a", encoding="utf-8") as f:
        for item in log:
            row = {"ts": now.isoformat(), "file": path.name, "section": section, **item}
            if note:
                row["note"] = note
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return log


# ---------------------------------------------------------------- 交易模式

MODES: tuple[str, ...] = ("paper", "testnet", "live")
MODE_NAME = {"paper": "模拟盘", "testnet": "测试网", "live": "实盘"}


class ModeSwitch(BaseModel):
    old: str
    new: str
    armed_at: datetime | None = None
    """切到实盘时的解锁时间。"""
    command_id: str
    """随后发给主程序的重启指令。"""
    notified: bool = False
    """Telegram 推出去了没有（面板接口填）。"""


def current_mode(paths: ConfigPaths) -> str:
    mode = _raw(paths.app).get("mode", "paper")
    return mode if isinstance(mode, str) else "paper"


def mode_cautions(
    target: str,
    risk: RiskConfig,
    keys: dict[str, bool],
    jev_provider: str,
    llm_provider: str,
    equity_usdt: float | None,
) -> list[str]:
    """切到 ``target`` 前要逐条看过的提示（只提醒，不拦；拦的只有 LiveGate 的确认词和密码）。"""
    out = [
        f"还没配 {k}，这个模式要用到"
        for k, need in key_needs(target, jev_provider, llm_provider).items()
        if need == "required" and not keys.get(k, False)
    ]
    if target == "live":
        out += [
            "实盘用真钱在币安主网下单，亏了就是真亏。",
            "解锁会绑定现在的 risk.toml：之后改硬上限，主程序会拒绝按实盘启动，要重新解锁。",
            *(f"硬上限比推荐值冒险：{x}" for x in risk.cautions(equity_usdt)),
        ]
    return out


def switch_mode(
    paths: ConfigPaths,
    data_dir: str | Path,
    target: str,
    password: str | None,
    confirm_word: str | None,
    now: datetime,
) -> ModeSwitch:
    """和命令行 go-live / go-paper 同一套门槛：切实盘 = LiveGate.unlock（确认词 + 面板密码，
    连错 5 次锁 15 分钟，绑定此刻 risk.toml）；切测试网要面板密码；切回模拟盘不要。
    离开实盘一律上锁。写完 app.toml 的 mode 再给主程序发重启指令（没在跑就下次启动生效）。"""
    if target not in MODES:
        raise ActionError(f"模式只能是：{' / '.join(MODES)}")
    old = current_mode(paths)
    gate = LiveGate(Path(data_dir) / "live_gate.json")
    armed_at = None
    if target == "live":
        try:
            fingerprint = config_fingerprint(load_risk_config(paths.risk))
        except ConfigError as e:
            raise ActionError(f"读不了 risk.toml，先修好再切实盘：{e}") from e
        if old == "live":
            with contextlib.suppress(LiveGateError):  # 解锁后改过硬上限的，允许重新解锁
                gate.require_armed(fingerprint)
                raise ActionError("现在就是实盘，而且已解锁")
        try:
            armed_at = gate.unlock(confirm_word or "", password or "", fingerprint)
        except LiveGateError as e:
            raise ActionError(str(e)) from e
    elif old == target:
        raise ActionError(f"现在就是{MODE_NAME[target]}")
    elif target == "testnet":
        check_password(data_dir, password or "")
    spec = {"mode": FieldSpec(str, choices=MODES)}
    try:
        _write_config(paths, data_dir, "app", {"mode": target}, now, spec)
    except ActionError:
        if armed_at is not None:  # 文件没写成：刚解的锁收回
            gate.disarm()
        raise
    if target != "paper":  # 只在模拟盘生效的开关顺手关掉，修改记录里写明原因
        fast = _raw(paths.app).get("fast_loop")
        on = [k for k in PAPER_ONLY_SWITCHES if isinstance(fast, dict) and fast.get(k) is True]
        if on:
            note = f"切到{MODE_NAME[target]}时自动关掉（只在模拟盘生效）"
            changes = dict.fromkeys(on, False)
            _write_config(paths, data_dir, "fast_loop", changes, now, None, note=note)
    if target != "live" and gate.armed:
        gate.disarm()
    cmd = PanelCommand(
        id=f"p{now.strftime('%Y%m%d%H%M%S')}{secrets.token_hex(4)}",
        ts=now,
        expires_at=now + COMMAND_TTL,
        kind="restart",
        params={},
        by="panel",
    )
    write_command(data_dir, cmd)
    return ModeSwitch(old=old, new=target, armed_at=armed_at, command_id=cmd.id)


def recent_changes(data_dir: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """最近的配置改动，新的在前。"""
    try:
        lines = (Path(data_dir) / CHANGES_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
        if len(out) >= limit:
            break
    return out


# 只有 paper 能打开的开关（testnet / live 写 true 拒绝启动，见 AppConfig._check）
PAPER_ONLY_SWITCHES = {
    "gate_record_only": "拍板只记不拦",
    "rule_gate_without_jev": "没有拍板人时用规则代替",
}

# 段 → (哪个文件, TOML 段名)
_WHERE: dict[Section, tuple[str, str | None]] = {
    "risk": ("risk", None),
    "category": ("risk", "category_exposure_x"),
    "app": ("app", None),
    "fast_loop": ("app", "fast_loop"),
    "predictor": ("app", "predictor"),
    "schedule": ("slow", "schedule"),
    "llm": ("slow", "llm"),
    "budget": ("slow", "budget"),
    "screener": ("slow", "screener"),
    "tiers": ("slow", "screener.tiers"),
    "market_desk": ("slow", "market_desk"),
    "meeting": ("slow", "meeting"),
}


def editable_fields(section: Section) -> dict[str, FieldSpec]:
    """这一段里能在网页上改的字段。设置页据此决定哪些项出现在“修改”弹窗里。"""
    fields = _base_fields(section)
    psec = _PROFILE_SECTION.get(section)
    if psec is None:
        return fields
    keys = set(_profile_keys(psec))
    if section == "app":
        keys.add("hold")  # 留空 = 删掉这一行，跟开会档位的推荐搭配
    return {k: replace(f, profiled=True) if k in keys else f for k, f in fields.items()}


def _base_fields(section: Section) -> dict[str, FieldSpec]:
    if section == "category":
        return {c.value: FieldSpec(float) for c in Category}
    if section == "risk":
        return scalar_fields(RiskConfig, {"day_reset_tz"})
    if section == "app":
        return scalar_fields(AppConfig, set(AppConfig.model_fields) - APP_EDITABLE)
    if section == "predictor":
        return scalar_fields(PredictorConfig, set())
    if section == "schedule":
        return scalar_fields(ScheduleConfig, set())
    if section == "llm":
        return scalar_fields(LlmConfig, set())
    if section == "budget":  # 设置页只放日上限；失败估价在首次设置之外不改
        return scalar_fields(BudgetConfig, set(BudgetConfig.model_fields) - {BUDGET_EDITABLE})
    if section == "screener":
        return scalar_fields(ScreenerSection, set())
    if section == "tiers":
        return scalar_fields(TiersSection, set())
    if section == "market_desk":
        return scalar_fields(MarketDeskSection, set(MarketDeskSection.model_fields) - DESK_EDITABLE)
    if section == "meeting":
        found = scalar_fields(
            MeetingConfig, set(MeetingConfig.model_fields) - set(MEETING_EDITABLE)
        )
        # 不写 = 按最长持仓档，留空就是删掉这一行
        found["snapshot_interval"] = FieldSpec(
            str, choices=("5m", "15m", "30m", "1h", "4h"), nullable=True
        )
        return {k: found[k] for k in MEETING_EDITABLE}
    # jev_provider / jev_model 换通道和模型版本要重新校准，不在网页上改
    return scalar_fields(FastLoopConfig, {"jev_provider", "jev_model"})


def _target(paths: ConfigPaths, section: Section) -> tuple[Path, str | None, dict[str, FieldSpec]]:
    """(文件, 段名, 可改字段)。"""
    file, table = _WHERE[section]
    return getattr(paths, file), table, editable_fields(section)


def scalar_fields(model: type[BaseModel], skip: set[str]) -> dict[str, FieldSpec]:
    """模型里能在网页上改的字段：开关、数字、可选值（Literal）、“不写就用默认”的数字。"""
    out: dict[str, FieldSpec] = {}
    for name, field in model.model_fields.items():
        if name in skip:
            continue
        spec = field_spec(field.annotation)
        pattern = next((m.pattern for m in field.metadata if getattr(m, "pattern", None)), None)
        if spec is None and pattern and field.annotation in (str, str | None):
            spec = FieldSpec(str, nullable=field.annotation is not str, pattern=pattern)
        if spec is not None:
            out[name] = spec
    return out


def field_spec(annotation: Any) -> FieldSpec | None:
    if annotation in (bool, int, float, bool | None):
        return FieldSpec(bool if annotation == bool | None else annotation)  # 开关“不写 = 关”
    if annotation == list[str]:
        return FieldSpec(str, names=True)
    args = get_args(annotation)
    if get_origin(annotation) is tuple and len(args) == 2 and args[1] is Ellipsis:
        inner = get_args(args[0])
        if get_origin(args[0]) is Literal and inner and all(isinstance(a, str) for a in inner):
            return FieldSpec(str, choices=inner, multi=True)
        return None
    if get_origin(annotation) is Literal and args:
        kinds = {type(a) for a in args}
        if len(kinds) == 1 and kinds <= {int, str}:
            return FieldSpec(kinds.pop(), choices=args)
        return None
    if get_origin(annotation) in (Union, UnionType) and type(None) in args:
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1 and rest[0] in (int, float):
            return FieldSpec(rest[0], nullable=True)
    return None


def _coerce(key: str, raw: Any, spec: FieldSpec) -> Value:
    if raw is None:
        if spec.nullable or spec.profiled:
            return None
        raise ActionError(f"{key} 不能空着")
    if spec.names:
        return _coerce_names(key, raw)
    if spec.multi:
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ActionError(f"{key} 要从选项里勾")
        bad = [x for x in raw if x not in spec.choices]
        if bad:
            raise ActionError(f"{key} 没有这些选项：{' / '.join(bad)}")
        return [str(c) for c in spec.choices if c in raw]  # 按选项顺序、去重
    if spec.kind is str and not spec.choices:
        if not isinstance(raw, str):
            raise ActionError(f"{key} 要填文字")
        text = raw.strip()
        if not text:
            if spec.nullable or spec.profiled:
                return None
            raise ActionError(f"{key} 不能空着")
        if spec.pattern and not re.fullmatch(spec.pattern, text):
            raise ActionError(f"{key} 格式不对：{text}")
        return text
    if spec.choices:
        for c in spec.choices:  # 1 和 True 不算相等
            if raw == c and isinstance(raw, bool) == isinstance(c, bool):
                return c  # type: ignore[return-value]
        raise ActionError(f"{key} 只能选：{' / '.join(str(c) for c in spec.choices)}")
    kind = spec.kind
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        raise ActionError(f"{key} 是开关，只能是 true / false")
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        raise ActionError(f"{key} 要填数字")
    if kind is int:
        if float(raw) != int(raw):
            raise ActionError(f"{key} 要填整数")
        return int(raw)
    return float(raw)


_NAME = re.compile(r"[A-Z0-9]{1,20}")


def _coerce_names(key: str, raw: Any) -> list[str]:
    """币名名单：列表或用空格 / 逗号隔开的一串；转大写、去掉 USDT 后缀、去重，顺序照填的。"""
    if isinstance(raw, str):
        raw = re.split(r"[\s,，、]+", raw)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ActionError(f"{key} 要填币名，例如 LINK")
    out: list[str] = []
    for x in raw:
        name = x.strip().upper()
        if name.endswith("USDT") and len(name) > 4:
            name = name.removesuffix("USDT")
        if not name or name in out:
            continue
        if not _NAME.fullmatch(name):
            raise ActionError(f"{key} 里的 {x.strip()} 不像币名（只要字母数字，例如 LINK）")
        out.append(name)
    return out


def _validate(section: Section, text: str, paths: ConfigPaths) -> None:
    """和启动时同一套校验：app / slow 都先垫上档位默认（core/profiles.py）。"""
    try:
        data = tomllib.loads(text)
        if section in ("risk", "category"):
            RiskConfig.model_validate(data)
        elif section in _SLOW_SECTIONS:
            raw_app = tomllib.loads(paths.app.read_text(encoding="utf-8"))
            SlowConfig.model_validate(slow_with_profile(data, profiles.profile_of(raw_app)))
        else:
            app = AppConfig.model_validate(with_profile(data))
            _validate_slow_for(profiles.profile_of(data), paths, app.fast_loop.regime_flip_target)
    except tomllib.TOMLDecodeError as e:  # set_toml_value 出错才会到这
        raise ActionError(f"改完不是合法 TOML，没有保存：{e}") from e
    except ValidationError as e:
        msgs = [
            f"{'.'.join(str(p) for p in err['loc']) or '(整体)'}："
            + err["msg"].removeprefix("Value error, ")
            for err in e.errors()
        ]
        raise ActionError("不合格，没有保存：" + "；".join(msgs)) from e


def _validate_slow_for(
    profile: str, paths: ConfigPaths, regime_flip_target: str = "flash_market_only"
) -> None:
    """切档位时 slow.toml 要按新档位再校验一遍，不然这里存成功、下次启动反被拒。
    大盘台接管时翻转目标也要对得上（``market_desk_conflict``）。"""
    if not paths.slow.is_file():
        return
    try:
        raw = tomllib.loads(paths.slow.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ActionError(f"{paths.slow.name} 不是合法 TOML，先在终端修好：{e}") from e
    try:
        slow = SlowConfig.model_validate(slow_with_profile(raw, profile))
    except ValidationError as e:
        msgs = [
            f"{'.'.join(str(p) for p in err['loc']) or '(整体)'}："
            + err["msg"].removeprefix("Value error, ")
            for err in e.errors()
        ]
        raise ActionError(
            f"换到档位 {profile} 后 {paths.slow.name} 不合格，没有保存：" + "；".join(msgs)
        ) from e
    if problem := market_desk_conflict(slow, regime_flip_target):
        raise ActionError(f"不合格，没有保存：{problem}")


_SECTION = re.compile(r"^\s*\[([A-Za-z0-9_.]+)\]\s*(#.*)?$")
_KEY = re.compile(r"^\s*[A-Za-z0-9_]+\s*=")


def _fmt(value: Value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | list):  # Literal 字样 / 多选列表；JSON 写法就是合法的 TOML
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def set_toml_value(text: str, table: str | None, key: str, value: Value) -> str:
    """把 ``[table]``（``None`` = 顶层）里 ``key = …`` 那一行的值换掉，行尾注释原样留着。

    没有这一行就加在该段最后一个键后面；段也没有就在文件末尾新开一段。
    ``value`` 为 ``None`` 时删掉这一行（回到默认值）。
    只对付我们自己的扁平配置（键 = 标量），不是通用 TOML 编辑器；改完由调用方整份重新校验。
    """
    lines = text.splitlines(keepends=True)
    current: str | None = None
    header: int | None = None
    last_key: int | None = None
    first_section: int | None = None
    own = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)(?:\"[^\"]*\"|[^#\n]*?)(\s*(?:#.*)?)$")
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            current = m.group(1)
            first_section = i if first_section is None else first_section
            if current == table:
                header = i
            continue
        if current != table:
            continue
        body = line.rstrip("\r\n")
        hit = own.match(body)
        if hit:
            if value is None:
                del lines[i]
                return "".join(lines)
            nl = line[len(body) :]
            lines[i] = f"{hit.group(1)}{key}{hit.group(2)}{_fmt(value)}{hit.group(3)}{nl}"
            return "".join(lines)
        if _KEY.match(line):
            last_key = i
    if value is None:
        return text
    new = f"{key} = {_fmt(value)}\n"
    if table is not None and header is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{sep}\n[{table}]\n{new}"
    if last_key is not None:
        at = last_key + 1
    elif table is not None:
        assert header is not None
        at = header + 1
    else:
        at = first_section if first_section is not None else len(lines)
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, new)
    return "".join(lines)
