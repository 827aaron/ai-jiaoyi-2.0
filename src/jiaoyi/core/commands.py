"""面板 → 主循环的指令通道，以及主循环写给面板看的运行时信息。

面板是独立进程，不导入 ``jiaoyi.app`` / 执行层，只能读写 ``data/`` 下的文件：

- 面板把一条指令原子写到 ``data/panel_commands/<id>.json``（用 ``write_command``）。
- 主循环每秒扫一次（``CommandInbox.pending``），执行后把结果写到
  ``data/panel_commands/done/<id>.json`` 并删掉原文件（结果保留 7 天）。
  同一个 id 只执行一次（防重放）。
- 过期（``expires_at`` 已过）、时间戳在未来、有效期超过 5 分钟、格式不对的指令一律不执行。
- 面板密码在面板侧校验（``live_gate.json`` 的哈希）；这里只认文件。面板和 API 只在本机或内网。
- 安全边界：谁能写 ``data/panel_commands/`` 谁就能暂停和平仓。面板写指令前必须校验面板密码
  （复用 LiveGate 的 scrypt 和锁定）、CSRF/Origin 和 Host 白名单；本机文件权限是最后一道防线，
  ``data/`` 只给运行用户读写。

``data/paused.json``：人工暂停开新仓的状态，由主循环在执行 pause / resume / close_all
时写，重启后仍生效；文件损坏按已暂停处理。
``data/runtime.json``：主循环启动时写，含已加载配置的指纹（``config_fingerprint``）。面板用同一个函数
算当前 ``config/*.toml`` 的指纹，不一致就在总览上提示「配置已改、未生效（重启后生效）」。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

log = logging.getLogger(__name__)

COMMANDS_DIR = "panel_commands"
PAUSED_FILE = "paused.json"
RUNTIME_FILE = "runtime.json"
MAX_TTL = timedelta(minutes=5)
CLOCK_SKEW = timedelta(seconds=30)
DONE_KEEP = timedelta(days=7)

CommandKind = Literal[
    "pause",
    "resume",
    "close_all",
    "close_position",
    "unblock_symbol",
    "refresh_cards",
    "restart",
    # 选币页（core/pool_overrides.py），都要 params.symbol
    "pin_symbol",
    "unpin_symbol",
    "blacklist_symbol",
    "unblacklist_symbol",
    "add_symbol",
    "remove_symbol",
    # 类别开关和每类进池上限（core/pool_overrides.py ``CATEGORY_ACTIONS``）
    "set_category",
    "set_category_cap",
]


class PanelCommand(BaseModel):
    """一条面板指令。

    ``params``：close_position 要 ``position_id``，unblock_symbol 和选币页指令
    （pin_symbol / unpin_symbol / blacklist_symbol / unblacklist_symbol / add_symbol /
    remove_symbol）要 ``symbol``，pause 可带 ``reason``。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    ts: AwareDatetime
    expires_at: AwareDatetime
    kind: CommandKind
    params: dict[str, str] = Field(default_factory=dict)
    by: str = Field(default="panel", max_length=64)

    @model_validator(mode="after")
    def _ttl(self) -> PanelCommand:
        if not self.ts < self.expires_at <= self.ts + MAX_TTL:
            raise ValueError("expires_at 必须晚于 ts，且有效期不超过 5 分钟")
        return self


class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    status: Literal["done", "rejected", "expired", "failed"]
    detail: str = ""
    ts: AwareDatetime


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_command(data_dir: str | Path, cmd: PanelCommand) -> Path:
    """面板用：原子写一条指令。"""
    path = Path(data_dir) / COMMANDS_DIR / f"{cmd.id}.json"
    _write_atomic(path, cmd.model_dump_json())
    return path


class CommandInbox:
    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir) / COMMANDS_DIR
        self._done = self._dir / "done"

    def pending(self) -> list[tuple[Path, PanelCommand | None, str]]:
        """待处理的指令文件：(路径, 指令或 None, 解析错误)。按文件名排序。"""
        if not self._dir.is_dir():
            return []
        out: list[tuple[Path, PanelCommand | None, str]] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                out.append((path, PanelCommand.model_validate_json(path.read_bytes()), ""))
            except (OSError, ValidationError, ValueError) as e:
                out.append((path, None, str(e).splitlines()[0][:200]))
        return out

    def is_done(self, command_id: str) -> bool:
        return (self._done / f"{command_id}.json").is_file()

    def finish(self, path: Path, result: CommandResult) -> None:
        """写结果再删原文件。已有结果（重放）时不覆盖，只删原文件。"""
        done = self._done / f"{result.id}.json"
        if not done.is_file():
            _write_atomic(done, result.model_dump_json())
        path.unlink(missing_ok=True)

    def prune(self, now: datetime, keep: timedelta = DONE_KEEP) -> int:
        """删掉超过 ``keep`` 的结果文件，返回删了几个。"""
        if not self._done.is_dir():
            return 0
        cutoff = (now - keep).timestamp()
        n = 0
        for path in self._done.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    n += 1
            except OSError:
                log.warning("清理指令结果 %s 失败", path)
        return n

    def check(self, cmd: PanelCommand, now: datetime) -> str | None:
        """不该执行的原因（过期 / 时间戳在未来），可以执行返回 None。"""
        if now > cmd.expires_at:
            return "expired"
        if cmd.ts > now + CLOCK_SKEW:
            return "expired"
        return None


def read_paused(data_dir: str | Path) -> dict[str, Any] | None:
    """暂停中返回 ``{"paused", "since", "reason", "command_id"}``，没暂停返回 None。

    失败即关闭：文件存在但读不出或损坏，按“已暂停”处理（带 ``corrupt: True``），
    由调用方发 CRITICAL。只有文件不存在或明确写着没暂停才算没暂停。
    """
    path = Path(data_dir) / PAUSED_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("paused"), bool):
            raise ValueError("格式不对")
    except (OSError, ValueError) as e:
        log.critical("暂停状态文件 %s 读不出或损坏（%s），按已暂停处理", path, e)
        return {
            "paused": True,
            "reason": f"{PAUSED_FILE} 读不出或损坏，按已暂停处理",
            "corrupt": True,
        }
    return raw if raw["paused"] else None


def write_paused(data_dir: str | Path, paused: bool, **info: str) -> None:
    _write_atomic(Path(data_dir) / PAUSED_FILE, json.dumps({"paused": paused, **info}))


def config_fingerprint(*configs: BaseModel) -> str:
    """配置指纹。主循环和面板必须按同样顺序传（risk, app, slow）。"""
    blob = json.dumps([c.model_dump(mode="json") for c in configs], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def write_runtime(data_dir: str | Path, **info: Any) -> None:
    _write_atomic(Path(data_dir) / RUNTIME_FILE, json.dumps(info, ensure_ascii=False))
