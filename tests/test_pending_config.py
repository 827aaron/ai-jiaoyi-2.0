"""待更新配置（config/*.pending.toml）：随「更新到最新版」合并、校验、失败回滚、成功归档。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import time
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
    p = _pending(paths, "app", "pool_size = 1000\n")
    p_slow = _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    sent: list[tuple[str, str]] = []
    res = pc.apply_pending(paths, data, NOW, notify=lambda t, b: sent.append((t, b)))
    assert res.status == "failed"
    assert res.offending == ["app.toml pool_size"]
    assert "config/app.toml" in res.error and str(tmp_path) not in res.error
    assert paths.app.read_bytes() == before
    assert not paths.slow.exists()  # 没有的正式文件也不会被建出来
    assert p.is_file() and p_slow.is_file()
    assert not (data / pc.ARCHIVE_DIR).exists()
    assert _rows(data) == []
    assert not list(paths.app.parent.glob("*.bak-*"))
    status = pc.read_status(data)
    assert status is not None and status["status"] == "failed"
    assert status["offending"] == ["app.toml pool_size"] and status["restored"] is False
    assert len(sent) == 1 and "app.toml pool_size" in sent[0][1]


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
    step = "-$(BIN)/jiaoyi apply-pending-config\n"
    assert body.index("$(MAKE) install") < body.index(step) < body.index("$(MAKE) check-config")
    gate = (
        "@if $(MAKE) check-config; then $(BIN)/jiaoyi apply-pending-config --commit || true; \\\n"
        "\telif $(BIN)/jiaoyi apply-pending-config --rollback; then $(MAKE) check-config; \\\n"
        "\telse exit 1; fi\n"
    )
    assert gate in body
    assert body.index(gate) < body.index("com.jiaoyi.run")


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


def test_refuses_keys_the_panel_cannot_edit(tmp_path: Path) -> None:
    paths, data = _with_slow(tmp_path)
    app_before, slow_before = paths.app.read_bytes(), paths.slow.read_bytes()
    _pending(
        paths,
        "app",
        'mode = "live"\ndata_dir = "/tmp/x"\npool_size = 45\n\n'
        '[fast_loop]\njev_model = "x"\nnot_a_real_key = 1\n',
    )
    _pending(paths, "slow", '[llm]\nprovider = "openrouter"\n\n[levels]\nspan_sigma = 2.0\n')
    res = pc.apply_pending(paths, data, NOW, check=lambda p: None)
    assert res.status == "failed" and res.error.startswith(pc.REFUSED_PREFIX)
    assert res.offending == [
        "app.toml mode",
        "app.toml data_dir",
        "app.toml fast_loop.jev_model",
        "app.toml fast_loop.not_a_real_key",
        "slow.toml llm.provider",
        "slow.toml levels.span_sigma",
    ]
    assert (paths.app.read_bytes(), paths.slow.read_bytes()) == (app_before, slow_before)


def test_check_is_the_full_check_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiaoyi import cli

    paths, data = _setup(tmp_path)
    assert pc.check_configs(paths) is None
    monkeypatch.setattr(cli, "missing_slow_stack", lambda slow, store: "没装 tradingagents")
    assert pc.check_configs(paths) == "没装 tradingagents"
    before = paths.app.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    assert pc.apply_pending(paths, data, NOW).error == "没装 tradingagents"
    assert paths.app.read_bytes() == before


def test_interrupt_during_second_check_restores_and_reraises(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    before = paths.app.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    calls: list[int] = []

    def check(p: ConfigPaths) -> str | None:
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return None

    sent: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        pc.apply_pending(paths, data, NOW, check=check, notify=lambda t, b: sent.append(b))
    assert paths.app.read_bytes() == before and not paths.slow.exists()
    assert pc.pending_path(paths.app).is_file()
    status = pc.read_status(data)
    assert status is not None and status["status"] == "failed"
    assert status["restored"] is True and status["restore_failed"] == []
    assert len(sent) == 1 and "正式配置是原样的" in sent[0]


def test_write_error_on_second_file_restores_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, data = _with_slow(tmp_path)
    app_before, slow_before = paths.app.read_bytes(), paths.slow.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    real_write = pc._atomic_write

    def flaky(path: Path, text: str) -> None:
        if path.name == "slow.toml":
            raise OSError("disk full")
        real_write(path, text)

    monkeypatch.setattr(pc, "_atomic_write", flaky)
    res = pc.apply_pending(paths, data, NOW, check=lambda p: None)
    assert res.status == "failed" and "disk full" in res.error
    assert (paths.app.read_bytes(), paths.slow.read_bytes()) == (app_before, slow_before)
    assert pc.read_status(data)["restored"] is True  # type: ignore[index]


def test_wrong_line_edit_is_caught_by_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, data = _setup(tmp_path)
    before = paths.app.read_bytes()
    _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    monkeypatch.setattr(pc, "set_toml_value", lambda text, table, key, value: text)
    res = pc.apply_pending(paths, data, NOW, check=lambda p: None)
    assert res.status == "failed" and "读回来的值不对" in res.error
    assert res.offending == ["app.toml fast_loop.surge_vol_ratio"]
    assert paths.app.read_bytes() == before


def test_file_saved_between_merge_and_write_is_merged_again(tmp_path: Path) -> None:
    """合并后、写之前面板存了一次：重读重合并，两边的改动都在。"""
    paths, data = _setup(tmp_path)
    _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    calls: list[int] = []

    def check(p: ConfigPaths) -> str | None:
        calls.append(1)
        if len(calls) == 1:
            text = actions.set_toml_value(
                paths.app.read_text(encoding="utf-8"), None, "pool_size", 45
            )
            paths.app.write_text(text, encoding="utf-8")
        return None

    assert pc.apply_pending(paths, data, NOW, check=check).status == "applied"
    cfg = load_app_config(paths.app)
    assert (cfg.pool_size, cfg.fast_loop.surge_vol_ratio) == (45, 6.0)
    assert len(calls) == 3  # 临时校验 → 写前发现被改 → 重合并后临时校验 + 正式校验


def test_rollback_after_failed_check_config_restores_everything(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    before = paths.app.read_bytes()
    p_app = _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    p_slow = _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    assert pc.apply_pending(paths, data, NOW, check=lambda p: None).status == "applied"
    assert paths.slow.exists() and not p_app.exists()
    sent: list[str] = []
    res = pc.rollback(data, NOW, "check-config 没过", notify=lambda t, b: sent.append(t))
    assert res is not None and res.status == "rolled_back"
    assert paths.app.read_bytes() == before and not paths.slow.exists()
    assert p_app.is_file() and p_slow.is_file()
    rows = _rows(data)
    assert [(r["key"], r["old"], r["new"]) for r in rows[-2:]] == [
        ("surge_vol_ratio", 6.0, 5.0),
        ("min_listed_days", 10, None),
    ]
    assert sent == ["待更新配置已退回"]
    assert pc.notice(paths, data)["failed"]["status"] == "rolled_back"
    assert pc.rollback(data, NOW, "again") is None  # 退过一次就不再退


def test_commit_stops_a_later_rollback(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    assert pc.apply_pending(paths, data, NOW, check=lambda p: None).status == "applied"
    assert pc.commit(data, NOW) is True
    assert pc.rollback(data, NOW, "x") is None
    assert load_app_config(paths.app).fast_loop.surge_vol_ratio == 6.0
    assert pc.commit(data, NOW) is False


def test_rollback_skips_a_file_edited_after_the_merge(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    assert pc.apply_pending(paths, data, NOW, check=lambda p: None).status == "applied"
    text = actions.set_toml_value(paths.app.read_text(encoding="utf-8"), None, "pool_size", 45)
    paths.app.write_text(text, encoding="utf-8")
    res = pc.rollback(data, NOW, "x")
    assert res is not None
    assert paths.app.read_text(encoding="utf-8") == text
    status = pc.read_status(data)
    assert status is not None and status["restore_failed"] == ["app.toml（合并后又被改过）"]


def test_cli_commit_and_rollback_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiaoyi import cli

    monkeypatch.setattr(cli, "_notify_telegram", lambda title, body: None)
    paths, data = _setup(tmp_path)
    before = paths.app.read_bytes()
    args = ["--risk", str(paths.risk), "--app", str(paths.app), "--slow", str(paths.slow)]
    assert cli.main(["apply-pending-config", "--rollback", *args]) == 1
    _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    assert cli.main(["apply-pending-config", *args]) == 0
    assert cli.main(["apply-pending-config", "--rollback", *args]) == 0
    assert paths.app.read_bytes() == before
    assert cli.main(["apply-pending-config", *args]) == 0
    assert cli.main(["apply-pending-config", "--commit", *args]) == 0
    assert cli.main(["apply-pending-config", "--rollback", *args]) == 1
    assert pc.read_status(data)["status"] == "committed"  # type: ignore[index]


class _Killed(BaseException):
    pass


def _kill_after_first_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟进程在两次写之间被 SIGKILL：第二次写时死掉，退回和写状态都来不及跑。"""
    real_write, real_status = pc._atomic_write, pc._write_status
    writes: list[Path] = []

    def write(path: Path, text: str) -> None:
        if writes:
            raise _Killed
        writes.append(path)
        real_write(path, text)

    def dead(*_: object) -> list[str]:
        raise _Killed

    def status(data: Path, rec: dict[str, object]) -> None:
        if rec.get("status") != "failed":  # 进程已死，失败状态写不出来
            real_status(data, rec)

    monkeypatch.setattr(pc, "_atomic_write", write)
    monkeypatch.setattr(pc, "_restore_entries", dead)
    monkeypatch.setattr(pc, "_write_status", status)


def test_status_applying_is_on_disk_before_any_real_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, data = _with_slow(tmp_path)
    app_before = paths.app.read_text(encoding="utf-8")
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    seen: list[dict[str, object]] = []
    real_write = pc._atomic_write

    def write(path: Path, text: str) -> None:
        seen.append(pc.read_status(data) or {})
        real_write(path, text)

    monkeypatch.setattr(pc, "_atomic_write", write)
    assert pc.apply_pending(paths, data, NOW, check=lambda p: None).status == "applied"
    first = seen[0]
    assert first["status"] == "applying" and first["rows_logged"] is False
    files = first["files"]
    assert isinstance(files, dict) and set(files) == {str(paths.app), str(paths.slow)}
    app = files[str(paths.app)]
    assert Path(app["backup"]).read_text(encoding="utf-8") == app_before
    assert app["pre"] == pc._text_sha(app_before)
    assert app["post"] == pc._sha(paths.app)
    assert len(first["archived"]) == 2 and len(first["rows"]) == 2  # type: ignore[arg-type]


def test_kill_between_writes_then_rollback_restores_exactly_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, data = _with_slow(tmp_path)
    app_before, slow_before = paths.app.read_bytes(), paths.slow.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    _kill_after_first_write(monkeypatch)
    with pytest.raises(_Killed):
        pc.apply_pending(paths, data, NOW, check=lambda p: None)
    monkeypatch.undo()
    assert pc.read_status(data)["status"] == "applying"  # type: ignore[index]
    assert paths.app.read_bytes() != app_before and paths.slow.read_bytes() == slow_before
    res = pc.rollback(data, NOW, "check-config 没过")
    assert res is not None and res.status == "rolled_back"
    assert (paths.app.read_bytes(), paths.slow.read_bytes()) == (app_before, slow_before)
    assert pc.read_status(data)["restore_failed"] == []  # type: ignore[index]
    assert pc.pending_path(paths.app).is_file() and pc.pending_path(paths.slow).is_file()
    assert _rows(data) == []  # 被杀时还没记修改记录，退回也不补


def test_rerun_after_kill_backs_up_the_original_not_the_merged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, data = _with_slow(tmp_path)
    app_before = paths.app.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    _kill_after_first_write(monkeypatch)
    with pytest.raises(_Killed):
        pc.apply_pending(paths, data, NOW, check=lambda p: None)
    monkeypatch.undo()
    later = NOW.replace(minute=45)
    assert pc.apply_pending(paths, data, later, check=lambda p: None).status == "applied"
    rec = pc.read_status(data)
    assert rec is not None
    assert Path(rec["files"][str(paths.app)]["backup"]).read_bytes() == app_before
    assert [r["old"] for r in _rows(data)] == [None, 7]  # 修改记录的旧值也是合并前的
    assert pc.rollback(data, later, "x") is not None
    assert paths.app.read_bytes() == app_before


def test_applying_left_without_pending_files_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, data = _with_slow(tmp_path)
    app_before = paths.app.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    _kill_after_first_write(monkeypatch)
    calls: list[int] = []

    def check(p: ConfigPaths) -> str | None:  # 只有一个文件：死在写完后的正式校验里
        calls.append(1)
        if len(calls) == 2:
            raise _Killed
        return None

    with pytest.raises(_Killed):
        pc.apply_pending(paths, data, NOW, check=check)
    monkeypatch.undo()
    assert paths.app.read_bytes() != app_before
    pc.pending_path(paths.app).unlink()
    assert pc.apply_pending(paths, data, NOW, check=lambda p: None).status == "none"
    assert paths.app.read_bytes() == app_before
    assert pc.read_status(data)["status"] == "rolled_back"  # type: ignore[index]


def test_old_uncommitted_merge_is_committed_before_the_next_update(tmp_path: Path) -> None:
    """上次合并过、正式文件也校验过，但没走到 --commit：这次更新先定下它，与它无关的
    check-config 失败不会把它退掉。"""
    paths, data = _setup(tmp_path)
    _pending(paths, "app", "[fast_loop]\nsurge_vol_ratio = 6.0\n")
    assert pc.apply_pending(paths, data, NOW, check=lambda p: None).status == "applied"
    assert pc.apply_pending(paths, data, NOW, check=lambda p: None).status == "none"
    assert pc.read_status(data)["status"] == "committed"  # type: ignore[index]
    assert pc.rollback(data, NOW, "x") is None
    assert load_app_config(paths.app).fast_loop.surge_vol_ratio == 6.0


def test_commit_accepts_an_applying_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, data = _with_slow(tmp_path)
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    _kill_after_first_write(monkeypatch)
    with pytest.raises(_Killed):
        pc.apply_pending(paths, data, NOW, check=lambda p: None)
    monkeypatch.undo()
    assert pc.commit(data, NOW) is True
    assert pc.rollback(data, NOW, "x") is None


def test_sigterm_during_write_phase_restores_then_exits(tmp_path: Path) -> None:
    paths, data = _with_slow(tmp_path)
    app_before, slow_before = paths.app.read_bytes(), paths.slow.read_bytes()
    _pending(paths, "app", "pool_size = 45\n")
    _pending(paths, "slow", "[screener]\nmin_listed_days = 10\n")
    calls: list[int] = []
    before_handler = signal.getsignal(signal.SIGTERM)

    def check(p: ConfigPaths) -> str | None:
        calls.append(1)
        if len(calls) == 2:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)  # 信号在这里送达，处理函数抛出
        return None

    with pytest.raises(BaseException, match="SIGTERM"):
        pc.apply_pending(paths, data, NOW, check=check)
    assert (paths.app.read_bytes(), paths.slow.read_bytes()) == (app_before, slow_before)
    status = pc.read_status(data)
    assert status is not None and status["status"] == "failed" and status["restored"] is True
    assert signal.getsignal(signal.SIGTERM) == before_handler


def test_market_desk_wording_keys_are_pending_mergeable(tmp_path: Path) -> None:
    paths, data = _with_slow(tmp_path)
    text = (
        '[market_desk]\nband_sigma = 0.3\nprior_mode = "context"\n'
        'momentum_guidance = true\naggregation = "median"\n'
    )
    _pending(paths, "slow", text)
    res = pc.apply_pending(paths, data, NOW)
    assert res.status == "applied", res.error
    desk = load_slow_config(paths.slow, "standard").market_desk
    assert (desk.band_sigma, desk.prior_mode, desk.momentum_guidance, desk.aggregation) == (
        0.3,
        "context",
        True,
        "median",
    )
    _pending(
        paths,
        "slow",
        "[market_desk]\ncall_p_min = 0.4\ncall_edge_min = 0.1\npublish_min_gap_min = 30\n",
    )
    res = pc.apply_pending(paths, data, NOW)
    assert res.status == "applied", res.error
    desk = load_slow_config(paths.slow, "standard").market_desk
    assert (desk.call_p_min, desk.call_edge_min, desk.publish_min_gap_min) == (0.4, 0.1, 30.0)
    _pending(paths, "slow", '[market_desk]\nmode = "replace"\n')
    res = pc.apply_pending(paths, data, NOW, check=lambda p: None)
    assert res.status == "failed" and res.offending == ["slow.toml market_desk.mode"]
    _pending(paths, "slow", '[market_desk]\nprior_mode = "start"\n')
    assert pc.apply_pending(paths, data, NOW).status == "failed"
