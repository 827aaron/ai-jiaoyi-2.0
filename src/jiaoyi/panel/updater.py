"""面板「更新到最新版」：在面板进程里跑 ``make update``（Aaron 平时手敲的那条），不另写一套步骤。

``make update`` 最后会重启面板自己，所以：
- 命令放进新会话里跑（``start_new_session``），面板被 launchd 杀掉时它不陪葬；
- 开跑前先写回执 ``panel_update.json``；输出写 ``.log``，退出码写 ``.rc``（都在 data 目录）；
- 新起的面板读回执：日志里有「重启 com.jiaoyi.panel」= 更新成功（日志写在重启面板之前）。
只有面板密码对了才会跑（见 server 的 ``POST /api/update``）；面板只对局域网开放。"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

STATE = "panel_update.json"
LOG = "panel_update.log"
RC = "panel_update.rc"
FETCH_TTL_S = 60.0
FETCH_TIMEOUT_S = 20
UPDATE_TIMEOUT_S = 20 * 60
"""开跑后这么久还没回执就当失败（make update 里装依赖、拉权重可能要几分钟）。"""
PANEL_RESTARTED = "重启 com.jiaoyi.panel（最后一步）"
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin")
"""launchd 起的面板 PATH 很短，找不到 uv / make：补上常见位置。"""

UpdateState = Literal["idle", "running", "done", "failed"]


class UpdateInfo(BaseModel):
    supported: bool
    """面板是从源码目录跑的（有 Makefile 和 .git）才能一键更新。"""
    why: str = ""
    head: str | None = None
    behind: int | None = None
    """origin/main 比本地多几个提交；None = 查不到（网络、没有 origin）。"""
    dirty: bool | None = None
    git_error: str | None = None
    state: UpdateState = "idle"
    from_hash: str | None = None
    to_hash: str | None = None
    started_at: datetime | None = None
    log_tail: str = ""


def _git(repo: Path, *args: str, timeout: float = 10) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout, env=env
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or f"git {args[0]} 失败")
    return r.stdout.strip()


class Updater:
    def __init__(self, repo: Path | None, data_dir: str | Path) -> None:
        self.repo = repo
        self.data = Path(data_dir)
        self._fetched_at = 0.0
        self._fetch_error: str | None = None

    @property
    def supported(self) -> bool:
        return (
            self.repo is not None
            and (self.repo / "Makefile").is_file()
            and (self.repo / ".git").exists()
        )

    # ---------------------------------------------------------------- 状态
    def _receipt(self) -> dict[str, object]:
        try:
            raw = json.loads((self.data / STATE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, rec: dict[str, object]) -> None:
        tmp = self.data / (STATE + ".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.data / STATE)

    def _tail(self, n: int = 12) -> str:
        try:
            lines = (self.data / LOG).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])[-1500:]

    def _resolve(self, rec: dict[str, object], head: str | None) -> dict[str, object]:
        """还在跑的回执：看 make 退出码、日志、HEAD 判断结果，定了就写回文件。"""
        if rec.get("status") != "running":
            return rec
        status: str | None = None
        try:
            rc = (self.data / RC).read_text(encoding="utf-8").strip()
        except OSError:
            rc = None
        if rc is not None:
            status = "done" if rc == "0" else "failed"
        elif PANEL_RESTARTED in self._tail(200):
            status = "done"  # make 在重启面板那步把自己（连同写退出码的 sh）一起带走了
        elif time.time() - float(str(rec.get("started_ts") or 0)) > UPDATE_TIMEOUT_S:
            status = "failed"
        if status is None:
            return rec
        rec = {**rec, "status": status, "to_hash": head}
        self._write(rec)
        return rec

    # ---------------------------------------------------------------- 对外
    def info(self, refresh: bool = True) -> UpdateInfo:
        if not self.supported or self.repo is None:
            return UpdateInfo(supported=False, why="面板不是从源码目录跑的，不能一键更新")
        repo = self.repo
        out = UpdateInfo(supported=True)
        try:
            out.head = _git(repo, "rev-parse", "--short", "HEAD")
            out.dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=no"))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            out.git_error = str(e)[:200]
        rec = self._resolve(self._receipt(), out.head)
        if rec:
            state = str(rec.get("status") or "idle")
            out.state = state if state in ("running", "done", "failed") else "idle"  # type: ignore[assignment]
            out.from_hash = str(rec["from_hash"]) if rec.get("from_hash") else None
            out.to_hash = str(rec["to_hash"]) if rec.get("to_hash") else None
            if rec.get("started_at"):
                out.started_at = datetime.fromisoformat(str(rec["started_at"]))
            if out.state in ("running", "failed", "done"):
                out.log_tail = self._tail()
        if out.state != "running" and out.git_error is None:
            self._count_behind(repo, out, refresh)
        return out

    def _count_behind(self, repo: Path, out: UpdateInfo, refresh: bool) -> None:
        now = time.monotonic()
        if refresh and now - self._fetched_at >= FETCH_TTL_S:
            self._fetched_at = now
            try:
                _git(repo, "fetch", "--quiet", "origin", "main", timeout=FETCH_TIMEOUT_S)
                self._fetch_error = None
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                self._fetch_error = f"查不到最新版本：{str(e)[:120]}"
        try:
            out.behind = int(_git(repo, "rev-list", "--count", "HEAD..origin/main"))
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            out.behind = None
        if out.behind is None or self._fetch_error:
            out.git_error = out.git_error or self._fetch_error

    def start(self, now: datetime) -> None:
        """开跑 ``make update``。调用方先验过面板密码。已有一次在跑就拒绝。"""
        if not self.supported or self.repo is None:
            raise RuntimeError("面板不是从源码目录跑的，不能一键更新")
        cur = self.info(refresh=False)
        if cur.state == "running":
            raise RuntimeError("已经有一次更新在跑")
        for name in (RC, LOG):
            (self.data / name).unlink(missing_ok=True)
        self._write(
            {
                "status": "running",
                "from_hash": cur.head,
                "started_at": now.isoformat(),
                "started_ts": time.time(),
            }
        )
        log, rc = shlex.quote(str(self.data / LOG)), shlex.quote(str(self.data / RC))
        path = os.pathsep.join(
            [*(os.path.expanduser(p) for p in EXTRA_PATH), os.environ.get("PATH", "")]
        )
        subprocess.Popen(
            ["sh", "-c", f"make update > {log} 2>&1; echo $? > {rc}"],
            cwd=self.repo,
            env={**os.environ, "PATH": path},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
