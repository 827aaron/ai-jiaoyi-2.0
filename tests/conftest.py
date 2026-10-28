from __future__ import annotations

import ipaddress
import json
import socket
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


class NetworkBlocked(OSError):
    pass


def _is_loopback(host: Any) -> bool:
    if host is None or host == "localhost":
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _refuse(what: str) -> NetworkBlocked:
    blocked_attempts.append(what)
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
        try:
            ipaddress.ip_address(str(host).split("%")[0])
        except ValueError:
            raise _refuse(f"resolve {host}") from None
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign,assignment]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign,assignment]
socket.getaddrinfo = _guarded_getaddrinfo


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


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    global _network_allowed
    _network_allowed = request.node.get_closest_marker("network") is not None
    blocked_attempts.clear()
    try:
        yield
    finally:
        _network_allowed = False
    if blocked_attempts:
        hits = sorted(set(blocked_attempts))
        blocked_attempts.clear()
        pytest.fail(f"测试试图连外网（被拦下）：{hits}；把 HTTP 层换成假的", pytrace=False)


def load_example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
