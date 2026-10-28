"""Process-wide log format: every line starts with UTC time, level and logger name.

launchd appends stdout and stderr of each service to one file with no timestamps of its own, so
the time has to come from the record. Unconfigured, Python's last-resort handler prints the bare
message; uncaught exceptions print a bare traceback. Both now go through one stderr handler.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import UTC, datetime
from types import TracebackType

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LEVEL_ENV = "JIAOYI_LOG_LEVEL"


class UTCFormatter(logging.Formatter):
    """``2026-10-28T13:05:09.123Z WARNING jiaoyi.gate.context: ...``, traceback on later lines."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        at = datetime.fromtimestamp(record.created, UTC)
        return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def configure_logging() -> None:
    """Install the stamped stderr handler on the root logger. Idempotent.

    The level defaults to WARNING, the same records the last-resort handler printed before, so
    the log does not grow; ``JIAOYI_LOG_LEVEL`` overrides it.
    """
    root = logging.getLogger()
    if any(getattr(h, "_jiaoyi", False) for h in root.handlers):
        return
    level = logging.getLevelName(os.environ.get(LEVEL_ENV, "WARNING").upper())
    if not isinstance(level, int):
        level = logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(UTCFormatter(FORMAT))
    handler.setLevel(level)
    handler._jiaoyi = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught


def _log_uncaught(kind: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, exc, tb)
        return
    logging.getLogger("jiaoyi").critical("未捕获的异常，进程退出", exc_info=(kind, exc, tb))


def _log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit or args.exc_value is None:
        return
    name = args.thread.name if args.thread is not None else "?"
    logging.getLogger("jiaoyi").error(
        "线程 %s 未捕获的异常", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )
