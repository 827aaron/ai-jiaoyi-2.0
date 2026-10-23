"""模拟环境选择：Demo Trading（默认）与旧测试网回退。"""

import pytest

from jiaoyi.contracts.risk_config import AppConfig
from jiaoyi.execution.exchange import CcxtBinanceFutures


def _fapi(ex: CcxtBinanceFutures) -> str:
    return str(ex._ex.urls["api"]["fapiPublic"])


def test_testnet_defaults_to_demo_trading():
    ex = CcxtBinanceFutures("k", "s", testnet=True)
    assert "demo-fapi.binance.com" in _fapi(ex)


def test_legacy_testnet_still_selectable():
    ex = CcxtBinanceFutures("k", "s", testnet=True, demo=False)
    assert "testnet.binancefuture.com" in _fapi(ex)


def test_mainnet_untouched():
    ex = CcxtBinanceFutures("k", "s", testnet=False)
    assert "fapi.binance.com" in _fapi(ex)
    assert "demo" not in _fapi(ex)


def test_sim_environment_defaults_to_demo():
    fields = AppConfig.model_fields
    assert fields["sim_environment"].default == "demo"


def test_hummingbot_patch_script_rewrites_constants_idempotently(tmp_path, monkeypatch):
    import pathlib
    import runpy
    import sys

    pkg = tmp_path / "hummingbot/connector/derivative/binance_perpetual"
    pkg.mkdir(parents=True)
    for d in (
        tmp_path / "hummingbot",
        tmp_path / "hummingbot/connector",
        tmp_path / "hummingbot/connector/derivative",
        pkg,
    ):
        (d / "__init__.py").write_text("")
    const = pkg / "binance_perpetual_constants.py"
    const.write_text(
        'TESTNET_BASE_URL = "https://testnet.binancefuture.com/fapi/"\n'
        'TESTNET_WS_URL = "wss://stream.binancefuture.com/"\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    script = pathlib.Path(__file__).parents[1] / "docker/hb-demo/patch_demo.py"
    for _ in range(2):  # 第二次应当幂等
        runpy.run_path(str(script))
    text = const.read_text()
    assert "https://demo-fapi.binance.com/fapi/" in text
    assert "wss://demo-fstream.binance.com/" in text
    assert "testnet.binancefuture.com" not in text

    const.write_text("TESTNET_BASE_URL = 'changed'\n")
    sys.modules.pop("hummingbot", None)
    import pytest

    with pytest.raises(SystemExit):
        runpy.run_path(str(script))


# ccxt 解析 algo 条件单后的形态（冒烟在 Demo 上实测）：type 是 "market"，真实类型在 info.orderType
DEMO_ALGO_STOP = {
    "id": "1000000012345",
    "clientOrderId": "lb-pos-abc",
    "symbol": "BTC/USDT:USDT",
    "type": "market",
    "reduceOnly": True,
    "triggerPrice": 61234.5,
    "info": {
        "algoId": "1000000012345",
        "clientAlgoId": "lb-pos-abc",
        "algoStatus": "NEW",
        "orderType": "STOP_MARKET",
        "triggerPrice": "61234.50",
        "reduceOnly": True,
    },
}


def test_algo_stop_market_is_recognised_despite_type_market():
    assert CcxtBinanceFutures._is_stop(DEMO_ALGO_STOP)
    stop = CcxtBinanceFutures._stop_from(DEMO_ALGO_STOP)
    assert stop.client_order_id == "lb-pos-abc"
    assert stop.stop_price == 61234.5


def test_plain_orders_are_not_stops():
    assert not CcxtBinanceFutures._is_stop({"type": "limit", "info": {"type": "LIMIT"}})
    assert not CcxtBinanceFutures._is_stop({"type": "market", "info": {}})


def test_fetch_open_orders_without_symbol_does_not_warn():
    ex = CcxtBinanceFutures("k", "s", testnet=True)
    assert ex._ex.options["fetchOpenOrders"]["warnWithoutSymbol"] is False


async def test_ensure_leverage_checks_readback():
    import pytest

    from jiaoyi.execution.exchange import ExchangeError

    class FakeCcxt:
        def __init__(self, actual: int) -> None:
            self.actual = actual
            self.set_calls: list[tuple[int, str]] = []

        async def load_markets(self):
            return {}

        async def set_leverage(self, lev, sym):
            self.set_calls.append((lev, sym))

        async def fetch_leverage(self, sym):
            return {"longLeverage": self.actual, "shortLeverage": self.actual}

    ex = CcxtBinanceFutures("k", "s", testnet=True)
    ex._ex = FakeCcxt(2)
    await ex.ensure_leverage("BTCUSDT", 2)
    assert ex._ex.set_calls == [(2, "BTC/USDT:USDT")]
    ex._ex = FakeCcxt(20)
    with pytest.raises(ExchangeError, match="20x"):
        await ex.ensure_leverage("BTCUSDT", 2)


def test_sim_environment_marker_must_match(tmp_path):
    import pytest

    from jiaoyi.execution.factory import SimEnvironmentMismatch, check_sim_environment

    with pytest.raises(SimEnvironmentMismatch, match="make hb-up"):
        check_sim_environment(tmp_path, "demo")  # 没标记
    (tmp_path / "hb_sim_environment").write_text("legacy_testnet\n")
    with pytest.raises(SimEnvironmentMismatch, match="必须一致"):
        check_sim_environment(tmp_path, "demo")
    check_sim_environment(tmp_path, "legacy_testnet")


async def test_direct_close_reads_fill_price_and_fee_with_fallbacks():
    class FakeCcxt:
        def __init__(self, response, fetched=None, trades=None) -> None:
            self.response, self.fetched, self.trades, self.params = response, fetched, trades, None

        async def load_markets(self, *a):
            return {}

        def amount_to_precision(self, sym, qty):
            return str(qty)

        async def create_order(self, sym, kind, side, amount, price, params):
            self.params = params
            return self.response

        async def fetch_order(self, order_id, sym):
            if self.fetched is None:
                raise RuntimeError("boom")
            return self.fetched

        async def fetch_my_trades(self, sym, since, limit, params):
            if self.trades is None:
                raise RuntimeError("boom")
            return self.trades

    ex = CcxtBinanceFutures("k", "s", testnet=True)
    # 响应里就有价和手续费
    ex._ex = FakeCcxt({"id": "1", "average": 68100.0, "fee": {"cost": 0.34, "currency": "USDT"}})
    fill = await ex.close_position_market("BTCUSDT", "sell", 0.01)
    assert (fill.price, fill.fee_usdt) == (68100.0, 0.34)
    assert ex._ex.params["reduceOnly"] and ex._ex.params["newOrderRespType"] == "RESULT"
    # 价靠 fetch_order，手续费靠成交明细求和
    ex._ex = FakeCcxt(
        {"id": "2", "average": None},
        {"average": 68200.0},
        [{"fee": {"cost": 0.1, "currency": "USDT"}}, {"fee": {"cost": 0.2, "currency": "USDT"}}],
    )
    fill = await ex.close_position_market("BTCUSDT", "sell", 0.01)
    assert fill.price == 68200.0 and fill.fee_usdt == pytest.approx(0.3)
    # 都查不到：不报错，字段是 None；非 USDT 手续费（BNB 抵扣）不当 USDT 算
    ex._ex = FakeCcxt({"id": "3", "average": None, "fee": {"cost": 0.001, "currency": "BNB"}})
    fill = await ex.close_position_market("BTCUSDT", "sell", 0.01)
    assert fill.price is None and fill.fee_usdt is None
