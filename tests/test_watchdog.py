from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.watchdog import STATE_FILE, STATUS_FILE, check, run_once


def _beat(root: Path, at: datetime) -> None:
    path = root / STATUS_FILE
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (at.timestamp(), at.timestamp()))


def _started(root: Path) -> None:
    (root / "runtime.json").write_text("{}", encoding="utf-8")


def test_never_started_is_silent(tmp_path: Path, now: datetime) -> None:
    assert check(tmp_path, now).alert is None


def test_stale_heartbeat_alerts_once_then_recovers(tmp_path: Path, now: datetime) -> None:
    _started(tmp_path)
    _beat(tmp_path, now - timedelta(seconds=30))
    sent: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200)

    transport = httpx.MockTransport(telegram)
    assert run_once(tmp_path, "tok", "chat", now, transport) == 0 and sent == []

    later = now + timedelta(minutes=3)  # 主进程被杀，心跳停在 now-30s
    first = check(tmp_path, later)
    assert first.alert is not None and first.alert.level is AlertLevel.CRITICAL
    assert run_once(tmp_path, "tok", "chat", later, transport) == 0
    assert run_once(tmp_path, "tok", "chat", later + timedelta(minutes=1), transport) == 0
    assert len(sent) == 1 and "主进程停了" in sent[0]  # 同一次掉线只发一次

    _beat(tmp_path, later + timedelta(minutes=2))
    assert run_once(tmp_path, "tok", "chat", later + timedelta(minutes=2), transport) == 0
    assert len(sent) == 2 and "恢复" in sent[1]


def test_failed_send_retries_next_minute(tmp_path: Path, now: datetime) -> None:
    _started(tmp_path)
    _beat(tmp_path, now - timedelta(minutes=5))
    down = httpx.MockTransport(lambda r: httpx.Response(502))
    assert run_once(tmp_path, "tok", "chat", now, down) == 1
    assert not (tmp_path / STATE_FILE).exists()  # 没发出去就不记，下一分钟再发
    assert run_once(tmp_path, None, None, now) == 1  # 没配 Telegram 也算失败
    assert check(tmp_path, now).alert is not None
