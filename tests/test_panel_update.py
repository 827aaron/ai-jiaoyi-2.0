"""面板「更新到最新版」：在源码目录跑 make update，密码对了才跑，回执留给重启后的新面板。"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.panel import updater
from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.server import create_app
from tests.test_panel_actions import HEADERS, PW, _setup

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
GOOD = f"update:\n\t@echo 拉代码\n\t@echo {updater.PANEL_RESTARTED}\n"
SLOW = f"update:\n\t@echo 拉代码\n\t@sleep 1\n\t@echo {updater.PANEL_RESTARTED}\n"
BAD = "update:\n\t@echo 工作区有改动，拉不了; exit 1\n"


def _git(cwd: Path, *args: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
    }
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _repo(tmp_path: Path, makefile: str) -> Path:
    """origin（裸仓库）+ 本地克隆；origin 上比本地多一个提交。"""
    origin, repo, other = tmp_path / "origin.git", tmp_path / "repo", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "Makefile").write_text(makefile, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "one")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    (other / "x.txt").write_text("x", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", "two")
    _git(other, "push", "-q", "origin", "main")
    return repo


def _client(tmp_path: Path, repo: Path | None) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths, _ = _setup(tmp_path)
    hb = HbReader(
        "http://hb",
        "u",
        None,
        account_name="a",
        connector_name="c",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    app = create_app(
        load_risk_config(paths.risk),
        load_app_config(paths.app),
        hb,
        clock=lambda: NOW,
        config_paths=paths,
        update_repo=repo,
    )
    return TestClient(app, client=("192.168.1.20", 5000), base_url="http://localhost:8090")


def _wait(client: TestClient, state: str) -> dict[str, object]:
    for _ in range(100):
        j: dict[str, object] = client.get("/api/update").json()
        if j["state"] == state:
            return j
        time.sleep(0.1)
    raise AssertionError(f"没等到 {state}")


def test_not_from_source_dir_is_unsupported(tmp_path: Path) -> None:
    with _client(tmp_path, None) as c:
        assert c.get("/api/update").json()["supported"] is False
        r = c.post("/api/update", json={"password": PW}, headers=HEADERS)
        assert r.status_code == 409


def test_info_shows_head_and_how_far_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "g", GOOD)
    with _client(tmp_path, repo) as c:
        j = c.get("/api/update").json()
    assert j["supported"] and j["state"] == "idle"
    assert len(j["head"]) >= 7 and j["behind"] == 1 and j["dirty"] is False


def test_wrong_password_does_not_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "g", GOOD)
    with _client(tmp_path, repo) as c:
        r = c.post("/api/update", json={"password": "nope"}, headers=HEADERS)
        assert r.status_code == 400
        assert c.get("/api/update").json()["state"] == "idle"
    assert not list((tmp_path / "data").glob("panel_update.*"))


def test_update_runs_make_update_and_leaves_receipt(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "g", GOOD)
    with _client(tmp_path, repo) as c:
        head = c.get("/api/update").json()["head"]
        r = c.post("/api/update", json={"password": PW}, headers=HEADERS)
        assert r.status_code == 200
        j = _wait(c, "done")
    assert j["from_hash"] == head and "拉代码" in str(j["log_tail"])
    assert (tmp_path / "data" / "panel_update.json").exists()


def test_failure_shows_output_and_second_click_while_running_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "g1", BAD)
    with _client(tmp_path / "t1", repo) as c:
        c.post("/api/update", json={"password": PW}, headers=HEADERS)
        j = _wait(c, "failed")
        assert "工作区有改动" in str(j["log_tail"])
    repo2 = _repo(tmp_path / "g2", SLOW.replace(updater.PANEL_RESTARTED, "ok"))
    with _client(tmp_path / "t2", repo2) as c:
        assert c.post("/api/update", json={"password": PW}, headers=HEADERS).status_code == 200
        assert c.post("/api/update", json={"password": PW}, headers=HEADERS).status_code == 409
        _wait(c, "done")


def test_panel_killed_midway_is_still_reported_done(tmp_path: Path) -> None:
    """make 在重启面板那步把写退出码的 sh 一起带走了：日志里有标记就算成功。"""
    repo = _repo(tmp_path / "g", GOOD)
    data = tmp_path / "data"
    data.mkdir()
    u = updater.Updater(repo, data)
    (data / updater.STATE).write_text(
        f'{{"status":"running","from_hash":"abc","started_ts":{time.time()}}}'
    )
    (data / updater.LOG).write_text(f"拉代码\n{updater.PANEL_RESTARTED}\n", encoding="utf-8")
    assert u.info(refresh=False).state == "done"
    (data / updater.STATE).write_text(f'{{"status":"running","started_ts":{time.time() - 3600}}}')
    (data / updater.LOG).write_text("装依赖…\n", encoding="utf-8")
    assert u.info(refresh=False).state == "failed"


@pytest.mark.parametrize("fn", ["GOOD"])
def test_makefile_restarts_panel_last(fn: str) -> None:
    mk = (Path(__file__).parent.parent / "Makefile").read_text(encoding="utf-8")
    body = mk[mk.index("\nupdate:") : mk.index("\ninit-config:")]
    assert body.index("com.jiaoyi.run") < body.index("com.jiaoyi.panel")
    assert updater.PANEL_RESTARTED in body
