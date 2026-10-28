from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "jiaoyi" / "contracts" / "examples"
ROOT = Path(__file__).resolve().parents[1]


def load_example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@pytest.fixture(autouse=True)
def _process_globals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Process-wide state one test can leave behind: the Binance ban, logging installed by
    ``cli.main``. A ban leaked from one test would refuse every later test's fake request.

    ``cli.main`` without ``--data-dir`` shares the ban file of the repo's own ``data/``; it is
    pointed at the test's tmp dir instead, and the repo file must come out untouched."""
    from jiaoyi.market import rest

    monkeypatch.setattr(rest, "_blocked", None)
    monkeypatch.setattr(rest, "_own", None)
    monkeypatch.setattr(rest, "_used_weight", None)
    monkeypatch.setattr(rest, "_ban_path", None)
    monkeypatch.setattr(rest, "_seen", {})
    monkeypatch.setattr(rest, "_warned", set())
    real_use = rest.use_shared_ban

    def use_outside_repo(data_dir: str | Path | None) -> None:
        if data_dir and Path(data_dir).resolve().is_relative_to(ROOT):
            data_dir = tmp_path / "shared-ban"
            data_dir.mkdir(exist_ok=True)
        real_use(data_dir)

    monkeypatch.setattr(rest, "use_shared_ban", use_outside_repo)
    repo_ban = ROOT / "data" / rest.BAN_FILE
    before = _stamp(repo_ban)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    root = logging.getLogger()
    level = root.level
    yield
    for h in [h for h in root.handlers if getattr(h, "_jiaoyi", False)]:
        root.removeHandler(h)
    root.setLevel(level)
    logging.captureWarnings(False)
    assert _stamp(repo_ban) == before, f"a test wrote {repo_ban}"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
