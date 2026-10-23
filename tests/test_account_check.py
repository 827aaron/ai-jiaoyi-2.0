"""启动时核对 hummingbot 与 ccxt 是同一个币安账户。"""

import pytest

from jiaoyi.contracts import Side
from jiaoyi.execution.account_check import AccountMismatch, verify_same_account
from jiaoyi.execution.hb_client import HbApiError
from tests.fakes import FakeExchange, FakeHbApi


async def _check(api: FakeHbApi, ex: FakeExchange) -> None:
    await verify_same_account(api, ex, "acct", "binance_perpetual_testnet")


async def test_same_account_passes() -> None:
    api, ex = FakeHbApi(), FakeExchange()
    ex.wallet = 1003.0  # 容差内
    await _check(api, ex)


async def test_balance_mismatch_refuses() -> None:
    api, ex = FakeHbApi(), FakeExchange()
    ex.wallet = 5000.0
    with pytest.raises(AccountMismatch, match="余额"):
        await _check(api, ex)


async def test_position_mismatch_refuses() -> None:
    api, ex = FakeHbApi(), FakeExchange()
    ex.set_position("ETHUSDT", Side.LONG, 1.0)  # 币安上有，hummingbot 没有
    with pytest.raises(AccountMismatch, match="持仓"):
        await _check(api, ex)
    api.pos = [{"trading_pair": "ETH-USDT", "amount": 1.0}]
    await _check(api, ex)


async def test_unreadable_side_is_not_a_mismatch_but_an_error() -> None:
    api, ex = FakeHbApi(), FakeExchange()
    api.down = True
    with pytest.raises(HbApiError):  # 读不到不是“不一致”，由调用方按模式决定
        await _check(api, ex)
