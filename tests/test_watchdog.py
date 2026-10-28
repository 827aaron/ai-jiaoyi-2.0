from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.watchdog import CONFIG_STATE_FILE, STATE_FILE, STATUS_FILE, check, run_once

ROOT = Path(__file__).parent.parent


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


def _telegram(sent: list[str], ok: bool = True) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200 if ok else 502)

    return httpx.MockTransport(handle)


def test_bad_config_alerts_once_keeps_checking_heartbeat_and_recovers(
    tmp_path: Path, now: datetime
) -> None:
    _started(tmp_path)
    _beat(tmp_path, now - timedelta(seconds=30))
    state = tmp_path / "config" / CONFIG_STATE_FILE
    sent: list[str] = []
    tg = _telegram(sent)
    err = "app.toml: predictor.keeper_snapshot: Extra inputs are not permitted"

    def run(at: datetime, error: str | None = err) -> int:
        return run_once(tmp_path, "tok", "chat", at, tg, config_error=error, config_state=state)

    assert run(now) == 0
    assert len(sent) == 1 and "配置不合格" in sent[0] and "keeper_snapshot" in sent[0]
    assert run(now + timedelta(minutes=1)) == 0
    assert len(sent) == 1

    later = now + timedelta(minutes=3)  # main loop died while the config is still bad
    assert run(later) == 0
    assert len(sent) == 2 and "主进程停了" in sent[1]

    _beat(tmp_path, later + timedelta(minutes=1))
    assert run(later + timedelta(minutes=1), None) == 0
    assert len(sent) == 4 and "配置已恢复合格" in sent[2] and "恢复" in sent[3]
    assert json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["down"] is False
    assert run(later + timedelta(minutes=2), None) == 0 and len(sent) == 4


def test_unsent_config_alert_is_retried(tmp_path: Path, now: datetime) -> None:
    sent: list[str] = []
    state = tmp_path / CONFIG_STATE_FILE
    bad, ok = _telegram(sent, ok=False), _telegram(sent)
    assert run_once(tmp_path, "t", "c", now, bad, config_error="x", config_state=state) == 1
    assert not state.exists()
    assert run_once(tmp_path, "t", "c", now, ok, config_error="x", config_state=state) == 0
    assert len(sent) == 2 and all("配置不合格" in s for s in sent)


def test_unwritable_config_state_is_logged_not_raised(tmp_path: Path, now: datetime) -> None:
    blocker = tmp_path / "config"
    blocker.write_text("", encoding="utf-8")  # a file where the state's directory should be
    sent: list[str] = []
    state = blocker / CONFIG_STATE_FILE
    tg = _telegram(sent)
    assert run_once(tmp_path, "t", "c", now, tg, config_error="x", config_state=state) == 0
    assert len(sent) == 1


def _cli_env(monkeypatch: pytest.MonkeyPatch, work: Path) -> list[str]:
    from jiaoyi import cli, watchdog

    monkeypatch.chdir(work)
    sent: list[str] = []
    monkeypatch.setattr(
        watchdog,
        "send",
        lambda token, chat, alert, transport=None: sent.append(alert.title) or True,
    )
    monkeypatch.setattr(
        cli.ChainedSecretStore, "get", lambda self, k: {"TELEGRAM_BOT_TOKEN": "t"}.get(k, "c")
    )
    return sent


@pytest.mark.parametrize("named_dir", [True, False])
def test_cli_watchdog_with_bad_config_still_checks_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named_dir: bool
) -> None:
    from jiaoyi import cli

    work = tmp_path / "work"
    data = work / "data" if not named_dir else tmp_path / "elsewhere"
    data.mkdir(parents=True)
    work.mkdir(exist_ok=True)
    _started(data)
    _beat(data, datetime.now(UTC) - timedelta(minutes=10))
    app = work / "app.toml"
    head = f'data_dir = "{data}"\n' if named_dir else "data_dir = [\n"  # [ = unparseable
    app.write_text(head + "not_a_key = 1\n", encoding="utf-8")
    sent = _cli_env(monkeypatch, work)
    assert cli.main(["watchdog", "--app", str(app)]) == 2
    assert sent == ["配置不合格", "主进程停了"]
    assert (data / STATE_FILE).is_file()
    assert (work / CONFIG_STATE_FILE).is_file()


def test_cli_config_episode_survives_fallback_dir_differing_from_real_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiaoyi import cli

    work = tmp_path / "work"
    real = tmp_path / "real"  # what the valid config names; the fallback work/data is missing
    work.mkdir()
    real.mkdir()
    app = work / "app.toml"
    shutil.copy(ROOT / "config" / "app.example.toml", app)
    good = app.read_text(encoding="utf-8").replace('data_dir = "data"', f'data_dir = "{real}"', 1)
    assert f'data_dir = "{real}"' in good
    app.write_text(good, encoding="utf-8")
    sent = _cli_env(monkeypatch, work)
    assert cli.main(["watchdog", "--app", str(app)]) == 0 and sent == []

    app.write_text("data_dir = [\n", encoding="utf-8")  # unparseable -> fallback "data"
    assert cli.main(["watchdog", "--app", str(app)]) == 2
    assert cli.main(["watchdog", "--app", str(app)]) == 2
    assert sent == ["配置不合格"]

    app.write_text(good, encoding="utf-8")
    assert cli.main(["watchdog", "--app", str(app)]) == 0
    assert cli.main(["watchdog", "--app", str(app)]) == 0
    assert sent == ["配置不合格", "配置已恢复合格"]
