"""待更新配置（config/*.pending.toml）：随「更新到最新版」合并、校验、失败回滚、成功归档。"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jiaoyi.core.config import load_app_config
from jiaoyi.core.hotconfig import ConfigFiles
from jiaoyi.panel import actions
from jiaoyi.panel import pending_config as pc
from jiaoyi.panel.actions import CHANGES_FILE, ConfigPaths
from jiaoyi.slow.settings import load_slow_config
from tests.test_panel_actions import _client, _setup

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)


def _with_slow(tmp_path: Path) -> tuple[ConfigPaths, Path]:
    paths, data = _setup(tmp_path)
    shutil.copy(ROOT / "config" / "slow.example.toml", paths.slow)
    return paths, data


def _pending(paths: ConfigPaths, kind: str, text: str) -> Path:
    p = pc.pending_path(getattr(paths, kind))
    p.write_text(text, encoding="utf-8")
    return p


def _rows(data: Path) -> list[dict[str, object]]:
    f = data / CHANGES_FILE
    if not f.is_file():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]


def test_pending_path_names() -> None:
    assert pc.pending_path(Path("config/app.toml")) == Path("config/app.pending.toml")
    assert pc.pending_path(Path("config/slow.toml")) == Path("config/slow.pending.toml")


def test_merge_keeps_comments_and_unrelated_keys(tmp_path: Path) -> None:
    paths, data = _with_slow(tmp_path)
    app_before = paths.app.read_text(encoding="utf-8")
    slow_before = paths.slow.read_text(encoding="utf-8")
    _pending(
        paths,
        "app",
        "# 新代码要的设置\npool_size = 45\n\n[fast_loop]\nsurge_vol_ratio = 6.0  # 注释也行\n",
    )
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    res = pc.apply_pending(paths, data, NOW)
    assert res.status == "applied", res.error
    app_after = paths.app.read_text(encoding="utf-8")
    old_lines = app_before.splitlines()
    new_lines = app_after.splitlines()
    changed = [x for x in new_lines if x not in old_lines]
    surge = "             # 哨兵：量能突增 1 分钟量 ≥ 5 倍"
    assert changed == ["pool_size = 45", f"surge_vol_ratio = 6.0{surge}"]
    assert [x for x in old_lines if x not in new_lines] == [f"surge_vol_ratio = 5.0{surge}"]
    cfg = load_app_config(paths.app)
    assert (cfg.pool_size, cfg.fast_loop.surge_vol_ratio) == (45, 6.0)
    slow_after = paths.slow.read_text(encoding="utf-8")
    assert slow_after.replace("min_listed_days = 10", "min_listed_days = 7") == slow_before
    assert load_slow_config(paths.slow, cfg.profile).screener.min_listed_days == 10


def test_success_archives_pending_backs_up_and_logs(tmp_path: Path) -> None:
    paths, data = _with_slow(tmp_path)
    p_app = _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    p_slow = _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    res = pc.apply_pending(paths, data, NOW)
    assert res.status == "applied"
    assert not p_app.exists() and not p_slow.exists()
    archived = sorted(x.name for x in (data / pc.ARCHIVE_DIR).iterdir())
    assert archived == ["20261028T093000-app.pending.toml", "20261028T093000-slow.pending.toml"]
    assert (paths.app.parent / "app.toml.bak-20261028T093000-pending").is_file()
    assert (paths.app.parent / "slow.toml.bak-20261028T093000-pending").is_file()
    rows = _rows(data)
    assert [(r["file"], r["section"], r["key"], r["old"], r["new"], r["source"]) for r in rows] == [
        ("app.toml", "fast_loop", "surge_vol_ratio", 5.0, 6.0, "pending"),
        ("slow.toml", "screener", "min_listed_days", 7, 10, "pending"),
    ]
    assert pc.read_status(data) is not None
    assert pc.read_status(data)["status"] == "applied"  # type: ignore[index]
    # 再跑一次（例如更新重跑）：没有待更新文件，什么都不做
    text = paths.app.read_text(encoding="utf-8")
    assert pc.apply_pending(paths, data, NOW).status == "none"
    assert paths.app.read_text(encoding="utf-8") == text
    assert len(_rows(data)) == 2


def test_no_pending_is_noop(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    text = paths.app.read_text(encoding="utf-8")
    assert pc.apply_pending(paths, data, NOW).status == "none"
    assert paths.app.read_text(encoding="utf-8") == text
    assert not (data / pc.STATUS_FILE).exists()
    assert not paths.slow.exists()


def test_failing_check_keeps_real_files_and_pending(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    before = paths.app.read_bytes()
    p = _pending(paths, "app", "pool_size = 45\n\n[fast_loop]\nnot_a_real_key = 1\n")
    p_slow = _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    sent: list[tuple[str, str]] = []
    res = pc.apply_pending(paths, data, NOW, notify=lambda t, b: sent.append((t, b)))
    assert res.status == "failed"
    assert res.offending == ["app.toml fast_loop.not_a_real_key"]
    assert "config/app.toml" in res.error and str(tmp_path) not in res.error
    assert paths.app.read_bytes() == before
    assert not paths.slow.exists()  # 没有的正式文件也不会被建出来
    assert p.is_file() and p_slow.is_file()
    assert not (data / pc.ARCHIVE_DIR).exists()
    assert _rows(data) == []
    assert not list(paths.app.parent.glob("*.bak-*"))
    status = pc.read_status(data)
    assert status is not None and status["status"] == "failed"
    assert status["offending"] == ["app.toml fast_loop.not_a_real_key"]
    assert len(sent) == 1 and "fast_loop.not_a_real_key" in sent[0][1]


def test_check_failing_on_real_files_restores_backups(tmp_path: Path) -> None:
    """临时目录里校验过了、写进正式文件后再校验不过：恢复备份，新建的文件删掉。"""
    paths, data = _setup(tmp_path)
    before = paths.app.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    calls: list[Path] = []

    def check(p: ConfigPaths) -> str | None:
        calls.append(p.app)
        return None if len(calls) == 1 else "pool_size: 不合格"

    res = pc.apply_pending(paths, data, NOW, check=check)
    assert res.status == "failed" and res.offending == ["app.toml pool_size"]
    assert calls[1] == paths.app and calls[0] != paths.app
    assert paths.app.read_bytes() == before
    assert not paths.slow.exists()
    assert pc.pending_path(paths.app).is_file()


def test_broken_pending_file_fails_without_touching_config(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    before = paths.app.read_bytes()
    _pending(paths, "app", "pool_size = \n")
    res = pc.apply_pending(paths, data, NOW)
    assert res.status == "failed" and "app.pending.toml" in res.error
    assert paths.app.read_bytes() == before


def test_risk_pending_refused_in_live_mode(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    paths.app.write_text(
        paths.app.read_text(encoding="utf-8").replace('mode = "paper"', 'mode = "live"', 1),
        encoding="utf-8",
    )
    before = paths.risk.read_bytes()
    _pending(paths, "risk", "max_leverage = 3\n")
    res = pc.apply_pending(paths, data, NOW, check=lambda p: None)
    assert res.status == "failed" and "实盘" in res.error
    assert paths.risk.read_bytes() == before


def test_loaders_and_hot_reload_ignore_pending_files(tmp_path: Path) -> None:
    paths, _ = _with_slow(tmp_path)
    files = ConfigFiles(paths.risk, paths.app, paths.slow)
    stamp = files.stamp()
    _pending(paths, "app", "[fast_loop]\nnot_a_real_key = 1\n")
    _pending(paths, "slow", "[screener]\nnot_a_real_key = 1\n")
    assert files.stamp() == stamp
    actions.load_configs(paths)
    assert pc.check_configs(paths) is None


def test_settings_page_lists_pending_and_last_failure(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        assert client.get("/api/settings").json()["pending_config"] == {
            "items": [],
            "error": None,
            "failed": None,
        }
    _pending(paths, "app", "[fast_loop]\nnot_a_real_key = 1\n")
    with _client(paths) as client:
        page = client.get("/api/settings").json()
    assert page["writable"] is True and page["file_error"] is None
    assert page["pending_config"]["items"] == [
        {
            "file": "app.toml",
            "table": "fast_loop",
            "key": "not_a_real_key",
            "value": 1,
            "label": "app.toml fast_loop.not_a_real_key",
        }
    ]
    assert pc.apply_pending(paths, data, NOW).status == "failed"
    with _client(paths) as client:
        failed = client.get("/api/settings").json()["pending_config"]["failed"]
    assert failed["offending"] == ["app.toml fast_loop.not_a_real_key"]
    # 待更新文件删掉了：旧的失败不再提示
    pc.pending_path(paths.app).unlink()
    with _client(paths) as client:
        assert client.get("/api/settings").json()["pending_config"]["failed"] is None


def test_makefile_merges_pending_after_install_before_check_config() -> None:
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    body = mk[mk.index("\nupdate:") : mk.index("\ninit-config:")]
    assert "$(MAKE) update-after-pull" in body
    step = "-$(BIN)/jiaoyi apply-pending-config"
    assert body.index("$(MAKE) install") < body.index(step) < body.index("$(MAKE) check-config")


def test_cli_never_blocks_the_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiaoyi import cli

    sent: list[str] = []
    monkeypatch.setattr(cli, "_notify_telegram", lambda title, body: sent.append(body))
    paths, data = _setup(tmp_path)
    _pending(paths, "app", "[fast_loop]\nnot_a_real_key = 1\n")
    args = ["--risk", str(paths.risk), "--app", str(paths.app), "--slow", str(paths.slow)]
    assert cli.main(["apply-pending-config", *args]) == 0
    out = capsys.readouterr().out
    assert "待更新配置没生效" in out and "app.toml fast_loop.not_a_real_key" in out
    assert pc.read_status(data)["status"] == "failed"  # type: ignore[index]
    assert len(sent) == 1 and "fast_loop.not_a_real_key" in sent[0]
    pc.pending_path(paths.app).write_text("[fast_loop]\nsurge_vol_ratio = 6.0\n", encoding="utf-8")
    assert cli.main(["apply-pending-config", *args]) == 0
    assert "已合并 1 项" in capsys.readouterr().out
    assert cli.main(["apply-pending-config", *args]) == 0
    assert "没有待更新配置" in capsys.readouterr().out
