"""hummingbot-api 客户端、Telegram 告警、代码格式转换。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import Alert
from jiaoyi.contracts.common import Role
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.execution.hb_client import HbApiError, HttpHummingbotApi
from jiaoyi.execution.symbols import from_ccxt_symbol, from_hb_pair, to_ccxt_symbol, to_hb_pair


def test_symbol_roundtrip() -> None:
    assert to_hb_pair("BTCUSDT") == "BTC-USDT"
    assert to_hb_pair("1000PEPEUSDT") == "1000PEPE-USDT"
    assert from_hb_pair("BTC-USDT") == "BTCUSDT"
    assert to_ccxt_symbol("TSLAUSDT") == "TSLA/USDT:USDT"
    assert from_ccxt_symbol("ETH/USDT:USDT") == "ETHUSDT"
    for bad in ("USDT", "BTCUSD", ""):
        with pytest.raises(ValueError):
            to_hb_pair(bad)
    with pytest.raises(ValueError):
        from_ccxt_symbol("BTC/USDT")  # 现货不是永续


def hb(handler: Any) -> HttpHummingbotApi:
    return HttpHummingbotApi("http://hb", "admin", "pw", transport=httpx.MockTransport(handler))


async def test_hb_client_requests_and_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/executors/":
            return httpx.Response(201, json={"executor_id": "e1"})
        if req.url.path == "/executors/search":
            return httpx.Response(200, json={"data": [{"executor_id": "e1"}]})
        if req.url.path == "/portfolio/state":
            return httpx.Response(200, json={"acct": {"conn": [{"token": "USDT", "units": 5.0}]}})
        return httpx.Response(200, json={})

    api = hb(handler)
    assert (await api.create_executor({"type": "position_executor"}, "acct", "jy-p1"))[
        "executor_id"
    ] == "e1"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert json.loads(seen[0].content) == {
        "account_name": "acct",
        "executor_config": {"type": "position_executor"},
        "controller_id": "jy-p1",
    }
    assert await api.running_executors("acct") == [{"executor_id": "e1"}]
    assert json.loads(seen[1].content)["status"] == "RUNNING"
    assert (await api.portfolio_state("acct", "conn"))[0]["units"] == 5.0
    await api.close()


async def test_hb_client_errors_are_uniform() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/executors/missing":
            return httpx.Response(404)
        if req.url.path == "/executors/bad":
            return httpx.Response(500, text="x" * 1000)
        raise httpx.ConnectError("refused")

    api = hb(boom)
    assert await api.get_executor("missing") is None
    with pytest.raises(HbApiError, match="500"):
        await api.get_executor("bad")
    with pytest.raises(HbApiError, match="不可达"):
        await api.running_executors("acct")
    with pytest.raises(HbApiError):
        await api.stop_executor("missing", keep_position=False)


def alert(key: str | None = "k") -> Alert:
    return Alert(
        ts=datetime(2026, 10, 22, tzinfo=UTC),
        level=AlertLevel.CRITICAL,
        role=Role.RECONCILER,
        title="保命止损缺失",
        body="BTCUSDT pos-1",
        symbol="BTCUSDT",
        dedup_key=key,
    )


async def test_telegram_sends_and_dedups() -> None:
    sent: list[dict[str, Any]] = []
    clock = [0.0]

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    tg = TelegramAlerter(
        "tok",
        "42",
        cooldown_s=300,
        monotonic=lambda: clock[0],
        transport=httpx.MockTransport(handler),
    )
    await tg.send(alert())
    await tg.send(alert())  # 冷却内不重复
    assert len(sent) == 1 and sent[0]["chat_id"] == "42"
    assert "保命止损缺失" in sent[0]["text"] and "BTCUSDT" in sent[0]["text"]
    clock[0] = 301.0
    await tg.send(alert())
    await tg.send(alert(None))
    await tg.send(alert(None))  # 没有 dedup_key 的每条都发
    assert len(sent) == 4


async def test_telegram_failure_never_raises_or_leaks_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}")

    tg = TelegramAlerter("SECRETTOKEN", "42", transport=httpx.MockTransport(boom))
    await tg.send(alert())
    assert "SECRETTOKEN" not in caplog.text
    assert "发送失败" in caplog.text


class FakeCcxt:
    """只模拟 ccxt 的几个方法，检查我们传给币安的参数。"""

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.created: list[tuple[Any, ...]] = []

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.1f}"

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.3f}"

    async def fetch_open_orders(self, *args: Any) -> list[dict[str, Any]]:
        return self.orders

    async def create_order(self, *args: Any) -> dict[str, Any]:
        self.created.append(args)
        order = {
            "id": "99",
            "symbol": "BTC/USDT:USDT",
            "type": "stop_market",
            "clientOrderId": args[5]["newClientOrderId"],
            "stopPrice": args[5]["stopLossPrice"],
            "reduceOnly": True,
        }
        self.orders.append(order)
        return order

    async def fetch_positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": "BTC/USDT:USDT", "contracts": -0.5, "side": "short", "entryPrice": 100.0},
            {"symbol": "ETH/USDT:USDT", "contracts": 0, "side": "long"},
        ]


async def test_ccxt_wrapper_params_and_mapping() -> None:
    from jiaoyi.contracts import Side
    from jiaoyi.execution.exchange import CcxtBinanceFutures

    ex = CcxtBinanceFutures("k", "s", testnet=True)
    await ex._ex.close()
    fake = FakeCcxt()
    ex._ex = fake
    ex._markets_loaded = True

    stop = await ex.place_stop("BTCUSDT", "sell", 0.0153, 67788.84, "lb-pos-1")
    sym, kind, side, amount, price, params = fake.created[0]
    assert (sym, kind, side, amount, price) == ("BTC/USDT:USDT", "market", "sell", "0.015", None)
    assert params["reduceOnly"] is True and params["workingType"] == "MARK_PRICE"
    assert params["newClientOrderId"] == "lb-pos-1" and params["stopLossPrice"] == 67788.8
    assert (stop.symbol, stop.order_id, stop.stop_price) == ("BTCUSDT", "99", 67788.8)
    # 同一个 clientOrderId 再挂不会重复下单
    again = await ex.place_stop("BTCUSDT", "sell", 0.0153, 67788.84, "lb-pos-1")
    assert again.order_id == "99" and len(fake.created) == 1

    (pos,) = await ex.positions()  # 数量为 0 的被忽略
    assert (pos.symbol, pos.side, pos.quantity) == ("BTCUSDT", Side.SHORT, 0.5)
