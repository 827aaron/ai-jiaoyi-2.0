"""持仓模式缓存：position_mode_cache_s > 0 时少查 30 权重的 positionSide/dual；0 = 每次都查。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from jiaoyi.execution.exchange import CcxtBinanceFutures, ExchangeError
from tests.test_exchange_execution_evidence import CID, client


def cached_client(ttl: float) -> tuple[CcxtBinanceFutures, Any, list[float]]:
    obj, ex = client()
    obj.position_mode_cache_s = ttl
    now = [1000.0]
    obj._mode_clock = lambda: now[0]
    ex.fetch_positions.return_value = [position_row()]
    return obj, ex, now


def position_row(side: str = "BOTH") -> dict[str, Any]:
    return {
        "symbol": "SYNTH/USDT:USDT",
        "contracts": 1,
        "side": "long",
        "entryPrice": 100,
        "info": {"positionSide": side, "positionAmt": "1"},
    }


async def test_zero_ttl_asks_binance_on_every_call() -> None:
    obj, ex, _ = cached_client(0)
    for _ in range(3):
        assert await obj.position_mode() == "one_way"
    assert ex.fapiPrivateGetPositionSideDual.await_count == 3


async def test_default_constructor_value_is_zero() -> None:
    obj = CcxtBinanceFutures("k", "s", testnet=True)
    try:
        assert obj.position_mode_cache_s == 0.0
    finally:
        await obj.close()


async def test_cached_evidence_reads_skip_the_weight_30_request_until_ttl() -> None:
    obj, ex, now = cached_client(300)
    await obj.entry_basis("SYNTHUSDT", CID)
    await obj.positions_evidence()
    await obj.entry_basis("SYNTHUSDT", CID)
    await obj.positions_evidence()
    assert ex.fapiPrivateGetPositionSideDual.await_count == 1
    now[0] += 299.9
    await obj.position_mode()
    assert ex.fapiPrivateGetPositionSideDual.await_count == 1
    now[0] += 0.1
    await obj.position_mode()
    assert ex.fapiPrivateGetPositionSideDual.await_count == 2


async def test_fresh_read_bypasses_a_warm_cache() -> None:
    obj, ex, _ = cached_client(300)
    await obj.position_mode()
    ex.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": True}
    assert await obj.position_mode(fresh=True) == "hedge"
    assert await obj.position_mode() == "hedge"  # 重查结果替换了缓存
    assert ex.fapiPrivateGetPositionSideDual.await_count == 2


async def test_failed_read_is_never_cached_and_clears_the_previous_value() -> None:
    obj, ex, now = cached_client(300)
    await obj.position_mode()
    now[0] += 301
    ex.fapiPrivateGetPositionSideDual.side_effect = RuntimeError("down")
    with pytest.raises(ExchangeError):
        await obj.position_mode()
    ex.fapiPrivateGetPositionSideDual.side_effect = None
    await obj.position_mode()
    assert ex.fapiPrivateGetPositionSideDual.await_count == 3


async def test_non_both_position_drops_the_cache() -> None:
    obj, ex, _ = cached_client(300)
    await obj.position_mode()
    ex.fetch_positions.return_value = [position_row("LONG")]
    with pytest.raises(ExchangeError):
        await obj.positions_evidence()
    await obj.position_mode()
    assert ex.fapiPrivateGetPositionSideDual.await_count == 2


@pytest.mark.parametrize(("code", "dropped"), [(-4061, True), (-2019, False)])
async def test_binance_position_side_mismatch_response_drops_the_cache(
    code: int, dropped: bool
) -> None:
    obj = CcxtBinanceFutures("k", "s", testnet=True, position_mode_cache_s=300)
    try:
        obj._position_mode_cached = (0.0, "one_way")
        body = json.dumps({"code": code, "msg": "rejected"})
        with pytest.raises(Exception):  # noqa: B017 - ccxt maps each code to its own class
            obj._ex.handle_errors(
                400, "Bad Request", "https://x/fapi/v1/order", "POST", {}, body,
                json.loads(body), {}, "",
            )  # fmt: skip
        assert (obj._position_mode_cached is None) is dropped
    finally:
        await obj.close()


@pytest.mark.parametrize(("ttl", "requests"), [(300, 1), (0, 5)])
async def test_concurrent_cold_callers_share_one_request(ttl: float, requests: int) -> None:
    obj, ex, _ = cached_client(ttl)

    async def slow_dual() -> dict[str, bool]:
        await asyncio.sleep(0.01)
        return {"dualSidePosition": False}

    ex.fapiPrivateGetPositionSideDual.side_effect = slow_dual
    modes = await asyncio.gather(*(obj.position_mode() for _ in range(5)))
    assert modes == ["one_way"] * 5
    assert ex.fapiPrivateGetPositionSideDual.await_count == requests
