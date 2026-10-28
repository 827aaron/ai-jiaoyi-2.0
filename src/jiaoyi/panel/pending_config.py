"""待更新配置：新代码要的设置先写进 ``config/<名>.pending.toml``，点一次更新时和代码一起生效。

为什么要有：正在跑的旧代码按契约拒绝不认识的键（``extra="forbid"``），新设置提前写进
``app.toml`` / ``slow.toml`` 会让热生效失败、面板变只读，只能等更新完再写、再重启一次。

- 待更新文件和正式文件同样的段落写法，只写要设的键；正在跑的程序、面板、热生效都不读它
  （它们只认 ``ConfigPaths`` 里的三个正式路径）。
- ``make update`` 在装完依赖之后、``check-config`` 之前跑 ``jiaoyi apply-pending-config``：
  合进正式文件（行内注释、无关的键原样留着）→ 用新代码的同一套读法校验 → 过了就把待更新文件
  挪到 ``data/config_pending_applied/``，每项记一行 ``data/config_changes.jsonl``（``source``
  = ``"pending"``）；不过就恢复备份、待更新文件留着，代码照常更新、按旧配置重启，原因写进
  ``data/config_pending_status.json``（面板横幅）并发 Telegram。
- 没有待更新文件就什么也不做；重复跑结果一样（同样的值再写一遍不变）。
- 实盘时不合并 ``risk.pending.toml``：实盘解锁绑定 risk.toml 指纹，改了主程序会拒绝按实盘启动。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jiaoyi.core.config import ConfigError, load_app_config, load_risk_config
from jiaoyi.panel.actions import _WHERE, CHANGES_FILE, ConfigPaths, set_toml_value
from jiaoyi.slow.settings import load_slow_config, market_desk_conflict

SUFFIX = ".pending.toml"
ARCHIVE_DIR = "config_pending_applied"
STATUS_FILE = "config_pending_status.json"
KINDS = ("risk", "app", "slow")
_SECTION_OF = {(f, t or ""): s for s, (f, t) in _WHERE.items()}
"""(文件, TOML 段) → 面板修改记录里用的段名；面板按它找中文名。"""


@dataclass(frozen=True)
class PendingItem:
    file: str
    """正式文件名，如 ``app.toml``。"""
    table: str
    """TOML 段，``""`` = 顶层，``screener.tiers`` 这种点号写法 = 子段。"""
    key: str
    value: Any


@dataclass
class ApplyResult:
    status: str
    """``none`` 没有待更新 / ``applied`` 已合并 / ``failed`` 没合并（正式文件没动）。"""
    items: list[PendingItem] = field(default_factory=list)
    error: str = ""
    offending: list[str] = field(default_factory=list)
    """出问题的项（``文件 段.键``）；从报错里认不出是哪项时列出全部。"""


def pending_path(real: Path) -> Path:
    return real.with_name(real.name.removesuffix(".toml") + SUFFIX)


def _label(it: PendingItem) -> str:
    return f"{it.file} {it.table + '.' if it.table else ''}{it.key}"


def _flatten(file: str, table: str, raw: dict[str, Any], out: list[PendingItem]) -> None:
    for key, value in raw.items():
        if isinstance(value, dict):
            _flatten(file, f"{table}.{key}" if table else key, value, out)
            continue
        ok = isinstance(value, bool | int | float | str) or (
            isinstance(value, list) and all(isinstance(x, bool | int | float | str) for x in value)
        )
        if not ok:
            where = f"{table}.{key}" if table else key
            raise ConfigError(f"{file} 的待更新项 {where} 只能是数字、开关、文字或它们的列表")
        out.append(PendingItem(file, table, key, value))


def read_pending(paths: ConfigPaths) -> tuple[list[PendingItem], list[Path]]:
    """待更新的项和待更新文件。待更新文件不是合法 TOML 抛 ``ConfigError``。"""
    items: list[PendingItem] = []
    files: list[Path] = []
    for kind in KINDS:
        real: Path = getattr(paths, kind)
        p = pending_path(real)
        if not p.is_file():
            continue
        files.append(p)
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{p.name} 读不出来：{e}") from e
        _flatten(real.name, "", raw, items)
    return items, files


def check_configs(paths: ConfigPaths) -> str | None:
    """和 ``jiaoyi check-config`` 同一套读法（密钥齐不齐之后由 check-config 本身再查）。"""
    try:
        app = load_app_config(paths.app)
        slow = load_slow_config(paths.slow, app.profile)
        if problem := market_desk_conflict(slow, app.fast_loop.regime_flip_target):
            return f"配置互相冲突：{problem}"
        load_risk_config(paths.risk)
    except ConfigError as e:
        return str(e)
    return None


def _merged(paths: ConfigPaths, items: list[PendingItem]) -> dict[str, str]:
    """正式文件名 → 合并后的全文（只含有待更新项的文件）。"""
    out: dict[str, str] = {}
    for kind in KINDS:
        real: Path = getattr(paths, kind)
        mine = [it for it in items if it.file == real.name]
        if not mine:
            continue
        text = real.read_text(encoding="utf-8") if real.is_file() else ""
        for it in mine:
            text = set_toml_value(text, it.table or None, it.key, it.value)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:  # 例如正式文件里这一段是行内表写法
            raise ConfigError(f"{real.name} 合并后不是合法 TOML：{e}") from e
        out[real.name] = text
    return out


def _old_values(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _lookup(raw: dict[str, Any], table: str, key: str) -> Any:
    node: Any = raw
    for part in table.split(".") if table else []:
        node = node.get(part) if isinstance(node, dict) else None
    return node.get(key) if isinstance(node, dict) else None


def _offending(items: list[PendingItem], error: str) -> list[str]:
    hit = [_label(it) for it in items if it.key in error]
    return hit or [_label(it) for it in items]


def _write_status(data: Path, rec: dict[str, Any]) -> None:
    data.mkdir(parents=True, exist_ok=True)
    tmp = data / f".{STATUS_FILE}.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(data / STATUS_FILE)


def read_status(data_dir: str | Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((Path(data_dir) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def apply_pending(
    paths: ConfigPaths,
    data_dir: str | Path,
    now: datetime,
    check: Callable[[ConfigPaths], str | None] = check_configs,
    notify: Callable[[str, str], None] | None = None,
) -> ApplyResult:
    """合并待更新配置。从不抛错（更新流程不能被它挡住），结果看返回值。"""
    data = Path(data_dir)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    items: list[PendingItem] = []
    files: list[Path] = []
    olds: dict[str, dict[str, Any]] = {}
    real_of = {getattr(paths, k).name: getattr(paths, k) for k in KINDS}
    try:
        items, files = read_pending(paths)
        if not files:
            return ApplyResult("none")
        if any(it.file == paths.risk.name for it in items):
            mode = _old_values(paths.app).get("mode")
            if mode == "live":
                raise ConfigError(
                    "实盘时不合并 risk.pending.toml：实盘解锁绑定 risk.toml，改了主程序会拒绝"
                    "按实盘启动。先切回模拟盘或在面板里用密码改硬上限"
                )
        merged = _merged(paths, items)
        # 先在临时目录里校验，不合格就不碰正式文件（旧程序热生效看得到正式文件）
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            trial: dict[str, Path] = {}
            for kind in KINDS:
                real: Path = getattr(paths, kind)
                dst = tdir / real.name
                if real.name in merged:
                    dst.write_text(merged[real.name], encoding="utf-8")
                elif real.is_file():
                    shutil.copy2(real, dst)
                trial[kind] = dst
            if problem := check(ConfigPaths(**trial)):
                raise ConfigError(problem.replace(f"{tdir}/", "config/"))
        olds = {name: _old_values(real_of[name]) for name in merged}
        backups: dict[Path, Path | None] = {}
        for name, text in merged.items():
            real = real_of[name]
            if real.is_file():
                bak = real.with_name(f"{name}.bak-{stamp}-pending")
                shutil.copy2(real, bak)
                backups[real] = bak
            else:
                backups[real] = None
            _atomic_write(real, text)
        if problem := check(paths):
            for written, saved in backups.items():
                if saved is None:
                    written.unlink(missing_ok=True)
                else:
                    shutil.copy2(saved, written)
            raise ConfigError(problem)
    except Exception as e:  # 任何意外都不能挡住代码更新
        err = str(e) or type(e).__name__
        res = ApplyResult("failed", items, err, _offending(items, err))
        _write_status(
            data,
            {
                "status": "failed",
                "ts": now.isoformat(),
                "error": err,
                "offending": res.offending,
                "items": [asdict(it) for it in items],
            },
        )
        if notify is not None:
            body = (
                f"有问题的项：{'、'.join(res.offending) or '（读不出待更新文件）'}\n{err[:600]}\n"
                "代码已照常更新、按原配置重启；待更新文件留着，改好后再点一次更新。"
            )
            notify("待更新配置没生效", body)
        return res
    archive = data / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    for p in files:
        shutil.move(str(p), archive / f"{stamp}-{p.name}")
    with (data / CHANGES_FILE).open("a", encoding="utf-8") as f:
        for it in items:
            kind = next(k for k in KINDS if getattr(paths, k).name == it.file)
            row = {
                "ts": now.isoformat(),
                "file": it.file,
                "section": _SECTION_OF.get((kind, it.table), it.table or kind),
                "key": it.key,
                "old": _lookup(olds.get(it.file, {}), it.table, it.key),
                "new": it.value,
                "source": "pending",
                "note": "待更新配置，随代码更新生效",
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _write_status(
        data,
        {"status": "applied", "ts": now.isoformat(), "items": [asdict(it) for it in items]},
    )
    return ApplyResult("applied", items)


def notice(paths: ConfigPaths, data_dir: str | Path) -> dict[str, Any]:
    """面板设置页用：还在等更新的项，以及上次合并没成功的原因（待更新文件还在时才给）。"""
    try:
        items, _ = read_pending(paths)
        error = None
    except ConfigError as e:
        items, error = [], str(e)
    status = read_status(data_dir)
    exists = any(pending_path(getattr(paths, k)).is_file() for k in KINDS)
    failed = status if exists and status and status.get("status") == "failed" else None
    return {
        "items": [{**asdict(it), "label": _label(it)} for it in items],
        "error": error,
        "failed": failed,
    }
