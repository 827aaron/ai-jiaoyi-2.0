"""模拟环境选择：Demo Trading（默认）与旧测试网回退。"""

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
