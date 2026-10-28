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


@pytest.fixture(autouse=True)
def _process_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Process-wide state one test can leave behind: the Binance ban, logging installed by
    ``cli.main``. A ban leaked from one test would refuse every later test's fake request."""
    from jiaoyi.market import rest

    monkeypatch.setattr(rest, "_blocked", None)
    monkeypatch.setattr(rest, "_used_weight", None)
    monkeypatch.setattr(rest, "_ban_path", None)
    monkeypatch.setattr(rest, "_ban_seen", None)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    root = logging.getLogger()
    level = root.level
    yield
    for h in [h for h in root.handlers if getattr(h, "_jiaoyi", False)]:
        root.removeHandler(h)
    root.setLevel(level)
    logging.captureWarnings(False)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
