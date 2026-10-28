from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest

from tests import conftest


@pytest.fixture
def drain_attempts() -> Iterator[None]:
    yield
    conftest._take_attempts()  # 本文件故意触发拦截，别让 autouse 判失败


def test_non_loopback_connect_is_blocked(drain_attempts: None) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s,
        pytest.raises(conftest.NetworkBlocked, match=r"1\.1\.1\.1:443"),
    ):
        s.connect(("1.1.1.1", 443))
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s,
        pytest.raises(conftest.NetworkBlocked),
    ):
        s.connect_ex(("8.8.8.8", 53))
    assert conftest.blocked_attempts == ["connect 1.1.1.1:443", "connect 8.8.8.8:53"]


def test_hostname_resolution_and_http_client_are_blocked(drain_attempts: None) -> None:
    with pytest.raises(conftest.NetworkBlocked, match=r"fapi\.binance\.com"):
        socket.getaddrinfo("fapi.binance.com", 443)
    with pytest.raises(httpx.ConnectError):
        httpx.get("https://fapi.binance.com/fapi/v1/ping", timeout=1)
    assert conftest.blocked_attempts[-1] == "resolve fapi.binance.com"


def test_loopback_is_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=2):
            pass
    assert conftest.blocked_attempts == []


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("localhost", True),
        (b"LOCALHOST.", True),
        ("ip6-localhost", True),
        ("127.0.0.53", True),
        ("::1", True),
        ("0.0.0.0", True),
        ("::", True),
        (None, True),
        ("fapi.binance.com", False),
        (b"fapi.binance.com", False),
        ("localhost.example.com", False),
        ("10.0.0.1", False),
    ],
)
def test_is_loopback(host: object, local: bool) -> None:
    assert conftest._is_loopback(host) is local


def test_ticks_cache_is_offline_and_empty_per_test() -> None:
    from jiaoyi.slow import binance_gate
    from jiaoyi.slow.ticks import TICKS

    assert TICKS.get("BTCUSDT") is None
    assert binance_gate.banned_until() == 0.0
    assert conftest.blocked_attempts == []


def test_swallowed_attempt_still_fails_the_test(pytester: pytest.Pytester) -> None:
    pytester.makeconftest((conftest.ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(
        """
        import socket
        import threading

        def dial():
            try:
                socket.create_connection(("203.0.113.1", 80), timeout=1)
            except OSError:
                pass

        def test_swallows():
            t = threading.Thread(target=dial, name="leaky-worker")
            t.start()
            t.join()
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*203.0.113.1:80*leaky-worker*", "*in dial*"])


def test_attempt_outside_any_test_fails_the_session(pytester: pytest.Pytester) -> None:
    pytester.makeconftest((conftest.ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(
        """
        import socket

        try:
            socket.getaddrinfo("api.binance.com", 443)
        except OSError:
            pass

        def test_ok():
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stderr.fnmatch_lines(["*测试之外*", "*resolve api.binance.com*"])


def test_network_marker_is_skipped_by_default(pytester: pytest.Pytester) -> None:
    pytester.makeconftest((conftest.ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.network
        def test_online():
            raise AssertionError("不该跑到")
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(skipped=1)
