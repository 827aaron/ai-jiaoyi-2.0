from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "jiaoyi" / "contracts" / "examples"
ROOT = Path(__file__).resolve().parents[1]


def load_example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
