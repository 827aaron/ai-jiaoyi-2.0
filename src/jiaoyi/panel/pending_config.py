"""待更新配置：新代码要的设置先写进 ``config/<名>.pending.toml``，点一次更新时和代码一起生效。

为什么要有：正在跑的旧代码按契约拒绝不认识的键（``extra="forbid"``），新设置提前写进
``app.toml`` / ``slow.toml`` 会让热生效失败、面板变只读，只能等更新完再写、再重启一次。

- 待更新文件和正式文件同样的段落写法，只写要设的键；正在跑的程序、面板、热生效都不读它
  （它们只认 ``ConfigPaths`` 里的三个正式路径）。
- ``make update`` 在装完依赖之后、``check-config`` 之前跑 ``jiaoyi apply-pending-config``：
  合进正式文件（行内注释、无关的键原样留着）→ 用新代码、和 ``check-config`` 完全同一套校验
  → 过了就把待更新文件挪到 ``data/config_pending_applied/``，每项记一行
  ``data/config_changes.jsonl``（``source`` = ``"pending"``）；不过就恢复备份、待更新文件留着，
  代码照常更新、按旧配置重启，原因写进 ``data/config_pending_status.json``（面板横幅）并发
  Telegram。之后的 ``check-config`` 不过，``--rollback`` 按状态文件里记的备份退回去，旧程序
  就不会面对它不认识的键；过了 ``--commit`` 定下来。
- 只收面板能改的键（``actions.editable_fields``）：模式、数据目录、拍板人通道 / 模型版本、
  军师团通道这类不在网页上改的，待更新文件里也不收。
- 没有待更新文件就什么也不做；重复跑结果一样（同样的值再写一遍不变），中断后再跑一次即可。
- 实盘时有 ``risk.pending.toml`` 整批都不合并：实盘解锁绑定 risk.toml 指纹，改了主程序会拒绝
  按实盘启动。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jiaoyi.core.config import ConfigError
from jiaoyi.panel.actions import _WHERE, CHANGES_FILE, ConfigPaths, editable_fields, set_toml_value

SUFFIX = ".pending.toml"
ARCHIVE_DIR = "config_pending_applied"
STATUS_FILE = "config_pending_status.json"
KINDS = ("risk", "app", "slow")
_SECTION_OF = {(f, t or ""): s for s, (f, t) in _WHERE.items()}
"""(文件, TOML 段) → 面板段名（修改记录、可改字段都按它）。"""
REFUSED_PREFIX = "这些项面板不让改"


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
    """``none`` 没有待更新 / ``applied`` 已合并 / ``failed`` 没合并（正式文件是原样的）/
    ``rolled_back`` 合并后 check-config 没过，已退回。"""
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
    """和 ``jiaoyi check-config`` 完全同一套校验（含配了军师团 key 时 tradingagents 装没装）。"""
    from jiaoyi.cli import config_problem  # cli 只在函数里导入面板，不会循环

    return config_problem(paths.risk, paths.app, paths.slow)


def _kind_of(paths: ConfigPaths, file: str) -> str:
    return next(k for k in KINDS if getattr(paths, k).name == file)


def _refused(paths: ConfigPaths, items: list[PendingItem]) -> list[str]:
    """面板不让改的项：不在 ``editable_fields`` 里，或军师团通道 ``[llm]``（只在首次设置里改）。"""
    out: list[str] = []
    for it in items:
        section = _SECTION_OF.get((_kind_of(paths, it.file), it.table))
        if section is None or section == "llm" or it.key not in editable_fields(section):
            out.append(_label(it))
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b) and isinstance(a, bool) == isinstance(b, bool)


def _lookup(raw: dict[str, Any], table: str, key: str) -> Any:
    node: Any = raw
    for part in table.split(".") if table else []:
        node = node.get(part) if isinstance(node, dict) else None
    return node.get(key) if isinstance(node, dict) else None


def _merged(paths: ConfigPaths, items: list[PendingItem]) -> dict[str, tuple[str | None, str]]:
    """正式文件名 → (合并前全文，没有文件 = None；合并后全文)，只含有待更新项的文件。

    合并后逐项读回来核对：``set_toml_value`` 改错了行就不写。"""
    out: dict[str, tuple[str | None, str]] = {}
    for kind in KINDS:
        real: Path = getattr(paths, kind)
        mine = [it for it in items if it.file == real.name]
        if not mine:
            continue
        src = real.read_text(encoding="utf-8") if real.is_file() else None
        text = src or ""
        for it in mine:
            text = set_toml_value(text, it.table or None, it.key, it.value)
        try:
            back = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:  # 例如正式文件里这一段是行内表写法
            raise ConfigError(f"{real.name} 合并后不是合法 TOML：{e}") from e
        wrong = [_label(it) for it in mine if not _same(_lookup(back, it.table, it.key), it.value)]
        if wrong:
            raise ConfigError(f"合并后读回来的值不对，没有写：{'、'.join(wrong)}")
        out[real.name] = (src, text)
    return out


def _old_values(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


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


def _restore(backups: dict[Path, Path | None]) -> list[str]:
    """按备份退回（原来没有的文件删掉），一个失败也接着退别的。返回没退回去的文件名。"""
    bad: list[str] = []
    for real, saved in backups.items():
        try:
            if saved is None:
                real.unlink(missing_ok=True)
            else:
                shutil.copy2(saved, real)
        except OSError:
            bad.append(real.name)
    return bad


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _append_rows(data: Path, rows: list[dict[str, Any]]) -> None:
    data.mkdir(parents=True, exist_ok=True)
    with (data / CHANGES_FILE).open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class _Changed(Exception):
    """合并之后、写之前正式文件被改了（面板正好在保存）。"""


@dataclass
class _Writes:
    backups: dict[Path, Path | None] = field(default_factory=dict)
    restore_failed: list[str] | None = None
    """``None`` = 没退过；``[]`` = 都退回原样了；有名字 = 这些文件没退回去。"""


def _write_all(
    paths: ConfigPaths,
    merged: dict[str, tuple[str | None, str]],
    real_of: dict[str, Path],
    stamp: str,
    check: Callable[[ConfigPaths], str | None],
    w: _Writes,
) -> None:
    """写正式文件并对正式文件再校验一次；任何异常（含中断）先把写过的退回去再抛。

    写每个文件前再读一次，和合并时读到的不一样（面板刚存过）就抛 ``_Changed`` 让调用方重来。"""
    w.backups = {}
    try:
        for name, (src, text) in merged.items():
            real = real_of[name]
            cur = real.read_text(encoding="utf-8") if real.is_file() else None
            if cur != src:
                raise _Changed(name)
            if cur is None:
                w.backups[real] = None
            else:
                bak = real.with_name(f"{name}.bak-{stamp}-pending")
                bak.write_text(cur, encoding="utf-8")
                w.backups[real] = bak
            _atomic_write(real, text)
        if problem := check(paths):
            raise ConfigError(problem)
    except BaseException:
        w.restore_failed = _restore(w.backups)
        raise


def _fail_body(res: ApplyResult, restore_failed: list[str] | None) -> str:
    if restore_failed:
        how = (
            f"注意：{'、'.join(restore_failed)} 没能退回原样，请在终端从 "
            "config/*.bak-*-pending 手动恢复。"
        )
    else:
        how = "正式配置是原样的，代码照常更新、按原配置重启。"
    return (
        f"有问题的项：{'、'.join(res.offending) or '（读不出待更新文件）'}\n{res.error[:600]}\n"
        f"{how}待更新文件留着，改好后再点一次更新。"
    )


def apply_pending(
    paths: ConfigPaths,
    data_dir: str | Path,
    now: datetime,
    check: Callable[[ConfigPaths], str | None] = check_configs,
    notify: Callable[[str, str], None] | None = None,
) -> ApplyResult:
    """合并待更新配置。普通错误不抛（更新流程不能被它挡住），结果看返回值；
    中断（``KeyboardInterrupt`` 这类）先退回、记下状态再抛。"""
    data = Path(data_dir)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    items: list[PendingItem] = []
    files: list[Path] = []
    olds: dict[str, dict[str, Any]] = {}
    real_of = {getattr(paths, k).name: getattr(paths, k) for k in KINDS}
    w = _Writes()
    try:
        items, files = read_pending(paths)
        if not files:
            return ApplyResult("none")
        if refused := _refused(paths, items):
            raise ConfigError(
                f"{REFUSED_PREFIX}，待更新文件里也不收：{'、'.join(refused)}"
                "（模式、数据目录、通道和模型版本这类在终端手改）"
            )
        has_risk = any(it.file == paths.risk.name for it in items)
        if has_risk and _old_values(paths.app).get("mode") == "live":
            raise ConfigError(
                "实盘时整批不合并（有 risk.pending.toml）：实盘解锁绑定 risk.toml，改了主程序会"
                "拒绝按实盘启动。先切回模拟盘，或在面板里用密码改硬上限、把它从待更新里删掉"
            )
        for attempt in range(3):
            merged = _merged(paths, items)
            # 先在临时目录里校验，不合格就不碰正式文件（旧程序热生效看得到正式文件）
            with tempfile.TemporaryDirectory() as tmp:
                tdir = Path(tmp)
                trial: dict[str, Path] = {}
                for kind in KINDS:
                    real: Path = getattr(paths, kind)
                    dst = tdir / real.name
                    if real.name in merged:
                        dst.write_text(merged[real.name][1], encoding="utf-8")
                    elif real.is_file():
                        shutil.copy2(real, dst)
                    trial[kind] = dst
                if problem := check(ConfigPaths(**trial)):
                    raise ConfigError(problem.replace(f"{tdir}/", "config/"))
            olds = {name: tomllib.loads(src or "") for name, (src, _) in merged.items()}
            try:
                _write_all(paths, merged, real_of, stamp, check, w)
                break
            except _Changed as e:
                if attempt == 2:
                    raise ConfigError(f"{e} 合并时一直在被改（面板正在保存？），这次没合并") from e
    except BaseException as e:
        err = str(e) or type(e).__name__
        offending = (
            _refused(paths, items) if err.startswith(REFUSED_PREFIX) else _offending(items, err)
        )
        res = ApplyResult("failed", items, err, offending)
        _write_status(
            data,
            {
                "status": "failed",
                "ts": now.isoformat(),
                "error": err,
                "offending": res.offending,
                "restored": w.restore_failed is not None,
                "restore_failed": w.restore_failed or [],
                "items": [asdict(it) for it in items],
            },
        )
        if notify is not None:
            with contextlib.suppress(Exception):
                notify("待更新配置没生效", _fail_body(res, w.restore_failed))
        if not isinstance(e, Exception):
            raise
        return res
    archive = data / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    archived: dict[str, str] = {}
    for p in files:
        dst = archive / f"{stamp}-{p.name}"
        shutil.move(str(p), dst)
        archived[str(dst)] = str(p)
    rows: list[dict[str, Any]] = []
    for it in items:
        kind = _kind_of(paths, it.file)
        rows.append(
            {
                "ts": now.isoformat(),
                "file": it.file,
                "section": _SECTION_OF.get((kind, it.table), it.table or kind),
                "key": it.key,
                "old": _lookup(olds.get(it.file, {}), it.table, it.key),
                "new": it.value,
                "source": "pending",
                "note": "待更新配置，随代码更新生效",
            }
        )
    _append_rows(data, rows)
    _write_status(
        data,
        {
            "status": "applied",
            "ts": now.isoformat(),
            "items": [asdict(it) for it in items],
            "backups": {str(k): str(v) if v else None for k, v in w.backups.items()},
            "written": {str(k): _sha(k) for k in w.backups},
            "archived": archived,
            "rows": rows,
        },
    )
    return ApplyResult("applied", items)


def commit(data_dir: str | Path, now: datetime) -> bool:
    """``check-config`` 过了：这次合并定下来，之后 ``rollback`` 不再动它。"""
    data = Path(data_dir)
    rec = read_status(data)
    if not rec or rec.get("status") != "applied":
        return False
    _write_status(data, {**rec, "status": "committed", "committed_at": now.isoformat()})
    return True


def rollback(
    data_dir: str | Path,
    now: datetime,
    reason: str,
    notify: Callable[[str, str], None] | None = None,
) -> ApplyResult | None:
    """合并之后 ``check-config`` 没过：按状态文件记的备份退回正式文件，待更新文件放回原处。

    没有待定的合并（没合并过、已经定下或已经退过）返回 None。"""
    data = Path(data_dir)
    rec = read_status(data)
    if not rec or rec.get("status") != "applied":
        return None
    raw_backups = rec.get("backups") or {}
    written = rec.get("written") or {}
    # 合并之后又被改过的文件（例如上次更新中断、之后在面板上存过）不退，免得冲掉那次修改
    touched = [Path(k).name for k in raw_backups if _sha(Path(k)) != written.get(k)]
    bad = _restore(
        {
            Path(k): Path(v) if v else None
            for k, v in raw_backups.items()
            if Path(k).name not in touched
        }
    )
    bad += [f"{name}（合并后又被改过）" for name in touched]
    for dst, src in (rec.get("archived") or {}).items():
        with contextlib.suppress(OSError):
            if not Path(src).exists() and Path(dst).is_file():
                shutil.move(dst, src)
    note = "更新时 check-config 没过，退回待更新之前的值"
    rows = [
        {**r, "ts": now.isoformat(), "old": r.get("new"), "new": r.get("old"), "note": note}
        for r in rec.get("rows") or []
        if isinstance(r, dict) and r.get("file") not in touched
    ]
    _append_rows(data, rows)
    items = [PendingItem(**it) for it in rec.get("items") or [] if isinstance(it, dict)]
    res = ApplyResult("rolled_back", items, reason, _offending(items, reason))
    _write_status(
        data,
        {
            "status": "rolled_back",
            "ts": now.isoformat(),
            "error": reason,
            "offending": res.offending,
            "restored": True,
            "restore_failed": bad,
            "items": rec.get("items") or [],
        },
    )
    if notify is not None:
        with contextlib.suppress(Exception):
            notify("待更新配置已退回", _fail_body(res, bad))
    return res


def notice(paths: ConfigPaths, data_dir: str | Path) -> dict[str, Any]:
    """面板设置页用：还在等更新的项，以及上次没生效的原因（待更新文件还在时才给）。"""
    try:
        items, _ = read_pending(paths)
        error = None
    except ConfigError as e:
        items, error = [], str(e)
    status = read_status(data_dir)
    exists = any(pending_path(getattr(paths, k)).is_file() for k in KINDS)
    bad = status is not None and status.get("status") in ("failed", "rolled_back")
    return {
        "items": [{**asdict(it), "label": _label(it)} for it in items],
        "error": error,
        "failed": status if exists and bad else None,
    }
