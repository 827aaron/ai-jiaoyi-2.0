"""面板的写操作：给主循环发指令、改配置文件。面板自己不执行任何交易动作。

- 指令走 ``jiaoyi.core.commands``：面板写 ``data/panel_commands/<id>.json``，主循环每秒扫一次执行，
  结果写到 ``done/<id>.json``。暂停、全部平仓、平单笔这类“收手”的动作不要密码；
  恢复开仓、人工解封会重新放开风险，要面板密码。
- 改配置一律要面板密码（硬上限是 Aaron 的规定，其余顺带）。先在内存里改 TOML 文本、
  用契约整份校验，过了才写；写前留备份，每项改动记一行 ``data/config_changes.jsonl``。
  主循环不热加载，重启后生效（总览按 ``data/runtime.json`` 的配置指纹提示）。
- 面板密码复用实盘门槛的同一份哈希（``live_gate.json``），连错 5 次锁 15 分钟，状态落盘。
"""

from __future__ import annotations

import json
import math
import re
import secrets
import shutil
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from jiaoyi.contracts import AppConfig, Category, RiskConfig
from jiaoyi.contracts.risk_config import FastLoopConfig
from jiaoyi.core.commands import (
    RUNTIME_FILE,
    CommandKind,
    CommandResult,
    PanelCommand,
    config_fingerprint,
    read_paused,
    write_command,
)
from jiaoyi.core.config import ConfigError, load_app_config, load_risk_config
from jiaoyi.execution.live_gate import LiveGate, LiveGateError  # 只用密码校验；不碰交易所
from jiaoyi.panel.files import read_live_gate
from jiaoyi.panel.overview import Control
from jiaoyi.slow.settings import SlowConfig, load_slow_config

CHANGES_FILE = "config_changes.jsonl"
COMMAND_TTL = timedelta(seconds=60)
NEEDS_PASSWORD: frozenset[CommandKind] = frozenset({"resume", "unblock_symbol"})

Section = Literal["risk", "category", "app", "fast_loop"]
# 网页上能改的运行配置；mode（走 go-live）、data_dir、模拟环境、Jev 通道和模型版本不在网页改
APP_EDITABLE = frozenset(
    {
        "predictor_enabled",
        "signal_dedup_s",
        "jev_timeout_ms",
        "keeper_interval_s",
        "reconcile_interval_s",
        "slow_loop_interval_min",
        "pool_size",
        "card_pick_max",
        "reconciler_close_unknown_positions",
    }
)


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
    )


# ---------------------------------------------------------------- 配置


class ConfigPaths(BaseModel):
    risk: Path
    app: Path
    slow: Path


def load_configs(paths: ConfigPaths) -> tuple[RiskConfig, AppConfig, SlowConfig]:
    """和主循环同一套读法（slow.toml 缺了用默认值），指纹才对得上。"""
    return (
        load_risk_config(paths.risk),
        load_app_config(paths.app),
        load_slow_config(paths.slow),
    )


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
    return loaded != config_fingerprint(risk, app, slow)


Value = bool | int | float


def update_config(
    paths: ConfigPaths,
    data_dir: str | Path,
    section: Section,
    changes: dict[str, Any],
    password: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """改一组配置。先验密码；全部通过契约校验才写；返回实际改了哪些（key、old、new）。"""
    check_password(data_dir, password)
    if not changes:
        raise ActionError("没有要改的项")
    path, table, fields = _target(paths, section)
    text = path.read_text(encoding="utf-8")
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ActionError(f"{path.name} 现在就不是合法 TOML，先在终端修好：{e}") from e
    olds = current.get(table, {}) if table else current
    log: list[dict[str, Any]] = []
    for key, raw in changes.items():
        if key not in fields:
            raise ActionError(f"{key} 不能在网页上改")
        value = _coerce(key, raw, fields[key])
        old = olds.get(key) if isinstance(olds, dict) else None
        if old is not None and old == value and isinstance(old, bool) == isinstance(value, bool):
            continue
        text = set_toml_value(text, table, key, value)
        log.append({"key": key, "old": old, "new": value})
    if not log:
        return []
    _validate(section, text)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    with (Path(data_dir) / CHANGES_FILE).open("a", encoding="utf-8") as f:
        for item in log:
            row = {"ts": now.isoformat(), "file": path.name, "section": section, **item}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return log


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


def _target(paths: ConfigPaths, section: Section) -> tuple[Path, str | None, dict[str, type]]:
    """(文件, 段名, 可改字段 → 类型)。"""
    if section == "category":
        return paths.risk, "category_exposure_x", {c.value: float for c in Category}
    if section == "risk":
        return paths.risk, None, _scalar_fields(RiskConfig, set())
    if section == "app":
        skip = set(AppConfig.model_fields) - APP_EDITABLE
        return paths.app, None, _scalar_fields(AppConfig, skip)
    return paths.app, "fast_loop", _scalar_fields(FastLoopConfig, set())


def _scalar_fields(model: type[BaseModel], skip: set[str]) -> dict[str, type]:
    out: dict[str, type] = {}
    for name, field in model.model_fields.items():
        if name not in skip and field.annotation in (bool, int, float):
            out[name] = field.annotation  # type: ignore[assignment]
    return out


def _coerce(key: str, raw: Any, kind: type) -> Value:
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


def _validate(section: Section, text: str) -> None:
    try:
        data = tomllib.loads(text)
        if section in ("risk", "category"):
            RiskConfig.model_validate(data)
        else:
            AppConfig.model_validate(data)
    except tomllib.TOMLDecodeError as e:  # set_toml_value 出错才会到这
        raise ActionError(f"改完不是合法 TOML，没有保存：{e}") from e
    except ValidationError as e:
        msgs = [
            f"{'.'.join(str(p) for p in err['loc']) or '(整体)'}："
            + err["msg"].removeprefix("Value error, ")
            for err in e.errors()
        ]
        raise ActionError("不合格，没有保存：" + "；".join(msgs)) from e


_SECTION = re.compile(r"^\s*\[([A-Za-z0-9_.]+)\]\s*(#.*)?$")
_KEY = re.compile(r"^\s*[A-Za-z0-9_]+\s*=")


def _fmt(value: Value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


def set_toml_value(text: str, table: str | None, key: str, value: Value) -> str:
    """把 ``[table]``（``None`` = 顶层）里 ``key = …`` 那一行的值换掉，行尾注释原样留着。

    没有这一行就加在该段最后一个键后面；段也没有就在文件末尾新开一段。
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
            nl = line[len(body) :]
            lines[i] = f"{hit.group(1)}{key}{hit.group(2)}{_fmt(value)}{hit.group(3)}{nl}"
            return "".join(lines)
        if _KEY.match(line):
            last_key = i
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
