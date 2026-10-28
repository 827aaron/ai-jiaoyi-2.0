from __future__ import annotations

import ipaddress
import json
import logging
import socket
import sys
import threading
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "jiaoyi" / "contracts" / "examples"
ROOT = Path(__file__).resolve().parents[1]

pytest_plugins = ["pytester"]

# 测试不许连外网：一次 make check 打到币安主网，整台机器的 IP 被限流封过（HTTP 418）。
# 只放行回环地址和 Unix 套接字；被拦的去向记下来，测试结束时判失败——
# 很多代码把连接错误当软失败吞掉，只抛异常的话漏网请求会悄悄变成绿灯。
# 真要联网的测试标 @pytest.mark.network，默认跳过，加 --run-network 才跑。

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_network_allowed = False
blocked_attempts: list[str] = []
"""本测试里被拦的去向；``_attempt_details`` 同序记着线程名和调用栈。"""
_attempt_details: list[str] = []
_outside_attempts: list[str] = []
"""不在任何测试里发生的拦截（导入、收集、会话级夹具、测试之间的后台线程），收尾时报出来。"""
_LOCAL_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}


class NetworkBlocked(OSError):
    pass


def _is_loopback(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    name = str(host).lower().rstrip(".")
    if name in _LOCAL_NAMES or name == "":
        return True
    try:
        ip = ipaddress.ip_address(name.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified  # 0.0.0.0 / :: 连的是本机


def _refuse(what: str) -> NetworkBlocked:
    stack = "".join(traceback.format_stack(limit=15)[:-2])
    blocked_attempts.append(what)
    _attempt_details.append(f"{what}（线程 {threading.current_thread().name}）\n{stack}")
    return NetworkBlocked(f"测试里禁止连外网：{what}（要联网就标 @pytest.mark.network）")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if not _network_allowed and isinstance(address, tuple) and not _is_loopback(address[0]):
        raise _refuse(f"connect {address[0]}:{address[1]}")
    _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if not _network_allowed and isinstance(address, tuple) and not _is_loopback(address[0]):
        raise _refuse(f"connect {address[0]}:{address[1]}")
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    # 系统解析器不走 socket.connect，域名在这里拦，报错里也能看到是哪个主机
    if not _network_allowed and not _is_loopback(host):
        name = host.decode(errors="replace") if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(name.split("%")[0])
        except ValueError:
            raise _refuse(f"resolve {name}") from None
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign,assignment]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign,assignment]
socket.getaddrinfo = _guarded_getaddrinfo


def _take_attempts() -> list[str]:
    details = list(_attempt_details)
    blocked_attempts.clear()
    _attempt_details.clear()
    return details


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-network", action="store_true", help="跑标了 network 的测试（会连外网）")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "network: 要连外网，默认跳过；--run-network 才跑")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="要连外网，加 --run-network 才跑")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _outside_attempts.extend(_take_attempts())
    if not _outside_attempts:
        return
    print("\n测试之外有代码试图连外网（被拦下）：", file=sys.stderr)
    for d in _outside_attempts:
        print(d, file=sys.stderr)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    global _network_allowed
    _outside_attempts.extend(_take_attempts())
    _network_allowed = request.node.get_closest_marker("network") is not None
    try:
        yield
    finally:
        _network_allowed = False
    hits = _take_attempts()
    if hits:
        pytest.fail(
            "测试试图连外网（被拦下），把 HTTP 层换成假的：\n" + "\n".join(hits), pytrace=False
        )


@pytest.fixture(autouse=True)
def _offline_binance_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """TICKS 是进程级缓存，谁先调谁去币安 exchangeInfo 取；封禁状态也是进程级的，
    一个测试登记的 418 会让后面的测试全挂。每个测试都从空缓存、未封禁开始，
    要精度值的测试照旧自己 monkeypatch ``TICKS.get``。"""
    from jiaoyi.slow import binance_gate
    from jiaoyi.slow.ticks import TICKS

    monkeypatch.setattr(TICKS, "_fetch", lambda: {})
    monkeypatch.setattr(TICKS, "_ticks", {})
    monkeypatch.setattr(TICKS, "_at", float("-inf"))
    binance_gate.reset()
    yield
    binance_gate.reset()


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
    """Process-wide state ``cli.main`` installs: logging, and the shared ban file (the ban itself
    is cleared by ``_offline_binance_state``). Without ``--data-dir`` it would share the repo's
    own ``data/rest_ban.json``; it is pointed at the test's tmp dir, and the repo file must come
    out untouched."""
    from jiaoyi.market import rest

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
