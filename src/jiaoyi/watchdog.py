"""看门狗：主进程外面的心跳检查。主进程挂了，它自己发不出告警，所以由这里发。

由 launchd 每分钟跑一次 ``jiaoyi watchdog``（scripts/install_launchd.sh 的 com.jiaoyi.watchdog），
不导入 app / 执行层，只读 ``data/``：

- ``runtime.json`` 不存在 → 主循环从没启动过，不用看。
- ``module_status.json`` 超过 ``stale_after``（默认 2 分钟）没更新 → 发一条 CRITICAL Telegram。
  主循环每 30 秒写一次心跳，所以 2 分钟没动就是挂了、卡死了或被停了。
- 每次掉线只发一次，恢复后再发一条 INFO。状态记在 ``data/watchdog_state.json``。

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


def save(data_dir: str | Path, down: bool, now: datetime) -> None:
    path = Path(data_dir) / STATE_FILE
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"down": down, "checked_at": now.isoformat()}), encoding="utf-8")
    tmp.replace(path)


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


def run_once(
    data_dir: str | Path,
    token: str | None,
    chat_id: str | None,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """``jiaoyi watchdog`` 的主体。返回进程退出码：0 正常，1 没配 Telegram 或发送失败。"""
    result = check(data_dir, now)
    if result.alert is None:
        return 0
    if not (token and chat_id):
        log.error(
            "看门狗：没配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，告警发不出去：%s",
            result.alert.title,
        )
        return 1
    if not send(token, chat_id, result.alert, transport):
        return 1
    save(data_dir, result.down, now)
    return 0
