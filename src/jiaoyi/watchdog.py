"""看门狗：主进程外面的心跳检查。主进程挂了，它自己发不出告警，所以由这里发。

由 launchd 每分钟跑一次 ``jiaoyi watchdog``（scripts/install_launchd.sh 的 com.jiaoyi.watchdog），
不导入 app / 执行层，只读 ``data/``：

- ``runtime.json`` 不存在 → 主循环从没启动过，不用看。
- ``module_status.json`` 超过 ``stale_after``（默认 2 分钟）没更新 → 发一条 CRITICAL Telegram。
  主循环每 30 秒写一次心跳，所以 2 分钟没动就是挂了、卡死了或被停了。
- 每次掉线只发一次，恢复后再发一条 INFO。状态记在 ``data/watchdog_state.json``。
- ``app.toml`` 校验不过（例如待更新配置比程序新）时照样查心跳：按文件里写的 ``data_dir``
  或默认 ``data``，另发一条 WARN「配置不合格」，每次不合格只发一次，恢复后发一条 INFO；
  这段记在 app.toml 旁边的 ``.watchdog_config_state.json``（读哪个数据目录取决于配置能不能读）。

整台 Mac mini 断电或断网时这里也一起停了，那种情况要靠机器外面的检查（见 docs/M0-setup.md）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import Alert
from jiaoyi.contracts.common import Role
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.core.commands import RUNTIME_FILE

log = logging.getLogger(__name__)

STATUS_FILE = "module_status.json"
STATE_FILE = "watchdog_state.json"
CONFIG_STATE_FILE = ".watchdog_config_state.json"
STALE_AFTER = timedelta(minutes=2)


@dataclass(frozen=True)
class Check:
    alert: Alert | None
    down: bool


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _was_down(data_dir: Path) -> bool:
    try:
        raw = json.loads((data_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("down") is True


def check(data_dir: str | Path, now: datetime, stale_after: timedelta = STALE_AFTER) -> Check:
    """看一次心跳，返回要发的告警（没有就是 None）和现在算不算掉线。不写文件。"""
    root = Path(data_dir)
    was_down = _was_down(root)
    if not (root / RUNTIME_FILE).is_file():
        return Check(None, was_down)
    beat = _mtime(root / STATUS_FILE)
    down = beat is None or now - beat > stale_after
    if down and not was_down:
        ago = (
            "从没写过心跳"
            if beat is None
            else f"已经 {int((now - beat).total_seconds() // 60)} 分钟没心跳"
        )
        body = f"{ago}。已有持仓靠币安上的保命止损；请检查 jiaoyi run 和 Mac mini。"
        return Check(_alert(now, AlertLevel.CRITICAL, "主进程停了", body), True)
    if not down and was_down:
        return Check(_alert(now, AlertLevel.INFO, "主进程心跳已恢复", ""), False)
    return Check(None, down)


def _alert(now: datetime, level: AlertLevel, title: str, body: str) -> Alert:
    return Alert(ts=now, level=level, role=Role.RECONCILER, title=title, body=body)


def _write_state(path: Path, state: dict[str, object]) -> bool:
    """Atomic write; a failure is logged, not raised, so the other state and alerts still go."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.error("看门狗：状态写不进 %s（%s）", path, type(e).__name__)
        return False
    return True


def save(data_dir: str | Path, down: bool, now: datetime) -> None:
    _write_state(Path(data_dir) / STATE_FILE, {"down": down, "checked_at": now.isoformat()})


def send(
    token: str, chat_id: str, alert: Alert, transport: httpx.BaseTransport | None = None
) -> bool:
    """同步发一条 Telegram，成功返回 True。失败下一分钟再试（不记掉线状态）。

    token 在 URL 里，日志只打异常类型。
    """
    try:
        with httpx.Client(timeout=10.0, transport=transport) as http:
            resp = http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": TelegramAlerter.format(alert)},
            )
    except Exception as e:
        log.error("看门狗 Telegram 发送失败（%s）", type(e).__name__)
        return False
    if resp.status_code >= 400:
        log.error("看门狗 Telegram 被拒：HTTP %d", resp.status_code)
        return False
    return True


def config_state_path(app_path: str | Path) -> Path:
    """Where the "config is bad" episode is remembered: beside app.toml, not in the data dir,
    because which data dir is read depends on whether the config parses."""
    return Path(app_path).with_name(CONFIG_STATE_FILE)


def _config_was_bad(path: Path) -> bool:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("bad") is True


def config_alert(
    state_path: Path, config_error: str | None, data_dir: str | Path, now: datetime
) -> Alert | None:
    """Alert for a config that stopped (or resumed) passing validation, once per episode."""
    was_bad = _config_was_bad(state_path)
    if config_error is not None and not was_bad:
        body = (
            f"{config_error[:600]}\n看门狗改按 {data_dir} 继续查心跳。"
            "主程序重启会被拒绝：请改好配置（make check-config）或更新到最新版。"
        )
        return _alert(now, AlertLevel.WARN, "配置不合格", body)
    if config_error is None and was_bad:
        return _alert(now, AlertLevel.INFO, "配置已恢复合格", "")
    return None


def run_once(
    data_dir: str | Path,
    token: str | None,
    chat_id: str | None,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
    config_error: str | None = None,
    config_state: Path | None = None,
) -> int:
    """``jiaoyi watchdog`` 的主体。返回进程退出码：0 正常，1 没配 Telegram 或发送失败。

    ``config_error`` is why app.toml failed validation (None = valid); ``data_dir`` is then the
    fallback directory, so the heartbeat is still checked. ``config_state`` (see
    ``config_state_path``) remembers the bad-config episode; None = config alerts are off.
    Each state is saved only after its own alert went out, so an unsent one is retried."""
    result = check(data_dir, now)
    cfg = (
        config_alert(config_state, config_error, data_dir, now)
        if config_state is not None
        else None
    )
    pending = [a for a in (cfg, result.alert) if a is not None]
    if not pending:
        return 0
    if not (token and chat_id):
        for a in pending:
            log.error(
                "看门狗：没配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，告警发不出去：%s", a.title
            )
        return 1
    rc = 0
    if cfg is not None and config_state is not None:
        if send(token, chat_id, cfg, transport):
            state = {"bad": config_error is not None, "checked_at": now.isoformat()}
            _write_state(config_state, state)
        else:
            rc = 1
    if result.alert is not None:
        if send(token, chat_id, result.alert, transport):
            save(data_dir, result.down, now)
        else:
            rc = 1
    return rc
