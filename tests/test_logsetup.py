from __future__ import annotations

import io
import logging
import re
import sys

import pytest

from jiaoyi import logsetup

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (\w+) ([\w.]+): ")


def _install(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stderr", out)
    logsetup.configure_logging()
    logsetup.configure_logging()
    return out


def test_records_carry_utc_millis_level_and_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _install(monkeypatch)
    logging.getLogger("jiaoyi.gate.context").warning("拍板人快照补拉 %s 失败", "4h")
    logging.getLogger("jiaoyi.gate.jev").info("below the default level")
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    m = STAMP.match(lines[0])
    assert m is not None and m.groups() == ("WARNING", "jiaoyi.gate.context")
    assert lines[0].endswith("拍板人快照补拉 4h 失败")


def test_traceback_follows_a_stamped_line(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _install(monkeypatch)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("jiaoyi.loop").exception("查成交明细失败")
    lines = out.getvalue().splitlines()
    assert STAMP.match(lines[0]) and lines[1] == "Traceback (most recent call last):"
    assert lines[-1] == "ValueError: boom"


def test_uncaught_exception_is_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _install(monkeypatch)
    try:
        raise RuntimeError("crash")
    except RuntimeError as e:
        sys.excepthook(type(e), e, e.__traceback__)
    lines = out.getvalue().splitlines()
    m = STAMP.match(lines[0])
    assert m is not None and m.group(1) == "CRITICAL" and lines[-1] == "RuntimeError: crash"


def test_level_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(logsetup.LEVEL_ENV, "info")
    out = _install(monkeypatch)
    logging.getLogger("jiaoyi.x").info("shown")
    assert out.getvalue().rstrip().endswith("INFO jiaoyi.x: shown")
