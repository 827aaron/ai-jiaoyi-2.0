"""止损 / 止盈触发价格源可配：默认原行为，按腿持久化，改设置只影响之后新建的腿。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jiaoyi.contracts import Side
from jiaoyi.contracts.risk_config import AppConfig
from jiaoyi.core.config import ConfigError, load_app_config
from jiaoyi.execution.brackets import ProtectionIncomplete
from jiaoyi.execution.exchange import BracketSubmissionUnknown, ExchangeError
from jiaoyi.execution.registry import PositionRegistry, ProtectiveLeg
from jiaoyi.panel.pages import _items
from tests.support import builders as b
from tests.test_config_limits import _write
from tests.test_exchange_brackets_io import client, raw
from tests.test_server_protection_regression import Harness, trailing_inputs

MARK, LAST = "MARK_PRICE", "CONTRACT_PRICE"


def _set(h: Harness, stop: str, take_profit: str) -> None:
    h.manager.stop_working_type = stop  # type: ignore[assignment]
    h.manager.take_profit_working_type = take_profit  # type: ignore[assignment]


def _types(h: Harness) -> dict[str, str]:
    """按角色取交易所上仍 NEW 的条件单触发价格源。"""
    out = {}
    for cid, order in h.exchange.orders.items():
        if order.algo_status == "NEW":
            out[cid.split("-")[0] if cid.startswith("lb-") else cid[:5]] = order.working_type
    return out


# ---------------------------------------------------------------- 配置


def test_config_default_is_contract_price_for_both() -> None:
    cfg = AppConfig.model_validate(b.APP_DEFAULTS)
    assert cfg.protection_stop_working_type == LAST
    assert cfg.protection_take_profit_working_type == LAST


def test_config_accepts_mark_price(tmp_path: Path) -> None:
    data = {
        **b.APP_DEFAULTS,
        "protection_stop_working_type": MARK,
        "protection_take_profit_working_type": MARK,
    }
    cfg = load_app_config(_write(tmp_path, data))
    assert cfg.protection_stop_working_type == MARK
    assert cfg.protection_take_profit_working_type == MARK


@pytest.mark.parametrize(
    "field", ["protection_stop_working_type", "protection_take_profit_working_type"]
)
@pytest.mark.parametrize("value", ["LAST_PRICE", "mark_price", "INDEX_PRICE", ""])
def test_config_rejects_other_values(tmp_path: Path, field: str, value: str) -> None:
    with pytest.raises(ConfigError, match=field):
        load_app_config(_write(tmp_path, {**b.APP_DEFAULTS, field: value}))


def test_panel_shows_both_read_only() -> None:
    items = {i.key: i for i in _items(AppConfig.model_validate(b.APP_DEFAULTS), "app")}
    for key in ("protection_stop_working_type", "protection_take_profit_working_type"):
        assert items[key].value == LAST and not items[key].editable


# ---------------------------------------------------------------- 交易所提交参数


@pytest.mark.parametrize(
    "kind,field,price,cid",
    [
        ("stop_loss", "stopLossPrice", 0.9, "br-sl-synthetic"),
        ("take_profit", "takeProfitPrice", 1.1, "br-tp-synthetic"),
    ],
)
async def test_mark_price_submission_params(kind: Any, field: str, price: float, cid: str) -> None:
    obj, ex = client()
    ex.create_order.return_value = {"info": raw(kind, triggerPrice=str(price), workingType=MARK)}
    order = await obj.place_bracket(
        "SYNTHUSDT", "sell", 10, price, cid, kind=kind, working_type=MARK
    )
    ex.create_order.assert_awaited_once_with(
        "SYNTH/USDT:USDT",
        "market",
        "sell",
        "10.0",
        None,
        {
            field: price,
            "reduceOnly": True,
            "workingType": MARK,
            "priceProtect": False,
            "positionSide": "BOTH",
            "newClientOrderId": cid,
        },
    )
    assert order.working_type == MARK


async def test_mark_price_request_with_contract_price_ack_is_unknown() -> None:
    obj, ex = client()
    ex.create_order.return_value = {"info": raw(workingType=LAST)}
    with pytest.raises(BracketSubmissionUnknown):
        await obj.place_bracket(
            "SYNTHUSDT", "sell", 10, 0.9, "br-sl-synthetic", kind="stop_loss", working_type=MARK
        )


async def test_existing_cid_with_other_working_type_is_never_reused() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(workingType=LAST)
    with pytest.raises(ExchangeError, match="不一致"):
        await obj.place_bracket(
            "SYNTHUSDT", "sell", 10, 0.9, "br-sl-synthetic", kind="stop_loss", working_type=MARK
        )
    ex.create_order.assert_not_awaited()


async def test_unknown_working_type_is_rejected_before_any_request() -> None:
    obj, ex = client()
    with pytest.raises(ValueError):
        await obj.place_bracket(
            "SYNTHUSDT",
            "sell",
            10,
            0.9,
            "br-sl-synthetic",
            kind="stop_loss",
            working_type="LAST_PRICE",  # type: ignore[arg-type]
        )
    ex.create_order.assert_not_awaited()
    ex.fapiPrivateGetAlgoOrder.assert_not_awaited()


# ---------------------------------------------------------------- 编排：新建、重启、移动止损


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_default_manager_keeps_todays_types(tmp_path: Path, side: Side) -> None:
    h = Harness(tmp_path, side)
    assert await h.manager.sync("pos-1") == "armed"
    assert _types(h) == {"lb": MARK, "br-sl": LAST, "br-tp": LAST}
    persisted = {leg.client_order_id[:5]: leg.working_type for leg in h.state().legs}
    assert persisted == {"lb-po": MARK, "br-sl": LAST, "br-tp": LAST}


@pytest.mark.parametrize("stop,take_profit", [(MARK, LAST), (LAST, MARK), (MARK, MARK)])
async def test_configured_types_are_submitted_and_persisted(
    tmp_path: Path, stop: str, take_profit: str
) -> None:
    h = Harness(tmp_path, Side.LONG)
    _set(h, stop, take_profit)
    assert await h.manager.sync("pos-1") == "armed"
    assert _types(h) == {"lb": MARK, "br-sl": stop, "br-tp": take_profit}
    h.restart()  # 重启回到默认设置：已挂的腿按记下的核对，不算不符。
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 3 and not h.cancels()


@pytest.mark.parametrize("before,after", [(LAST, MARK), (MARK, LAST)])
async def test_restart_with_changed_setting_keeps_existing_legs(
    tmp_path: Path, before: str, after: str
) -> None:
    h = Harness(tmp_path, Side.LONG)
    _set(h, before, before)
    assert await h.manager.sync("pos-1") == "armed"
    h.restart()
    _set(h, after, after)
    for _ in range(2):
        assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 3 and not h.cancels()
    assert h.state().last_error is None
    assert _types(h) == {"lb": MARK, "br-sl": before, "br-tp": before}
    assert {leg.working_type for leg in h.state().legs if leg.purpose != "lifeboat"} == {before}


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_order_disagreeing_with_persisted_leg_is_rejected(tmp_path: Path, side: Side) -> None:
    h = Harness(tmp_path, side)
    _set(h, MARK, MARK)
    await h.manager.sync("pos-1")
    sl = next(cid for cid in h.exchange.orders if cid.startswith("br-sl-"))
    h.exchange.orders[sl] = replace(h.exchange.orders[sl], working_type=LAST)
    h.restart()
    _set(h, LAST, LAST)  # 当前设置恰好和交易所上的值一样，也不能代替腿自己记下的值。
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    assert len(h.creates()) == 3 and not h.cancels()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("before,after", [(LAST, MARK), (MARK, LAST)])
async def test_trailing_generation_uses_current_setting_and_cancels_old_after(
    tmp_path: Path, side: Side, before: str, after: str
) -> None:
    h = Harness(tmp_path, side)
    _set(h, before, before)
    bid, ask = (102.0, 103.0) if side is Side.LONG else (97.0, 98.0)
    rec, _, basis, book = trailing_inputs(tmp_path / "math", side, bid, ask)
    h.registry.put(h.registry.get("pos-1").model_copy(update={"record": rec}))
    assert await h.manager.sync("pos-1") == "armed"
    old_sl = next(cid for cid in h.exchange.orders if cid.startswith("br-sl-"))
    tp = next(cid for cid in h.exchange.orders if cid.startswith("br-tp-"))
    h.restart()
    _set(h, after, after)
    assert await h.manager.observe_trailing("pos-1", basis, book)
    assert await h.manager.sync("pos-1") == "armed"
    new_sl = h.creates()[-1]
    assert new_sl.startswith("br-sl-") and new_sl != old_sl
    assert h.exchange.orders[new_sl].working_type == after
    assert h.exchange.orders[old_sl].working_type == before
    assert h.exchange.events.index(("create", new_sl)) < h.exchange.events.index(("cancel", old_sl))
    assert h.cancels() == [old_sl]
    legs = {leg.client_order_id: leg for leg in h.state().legs}
    assert legs[new_sl].purpose == "trailing" and legs[new_sl].working_type == after
    assert legs[old_sl].state == "cancelled" and legs[old_sl].working_type == before
    assert legs[tp].working_type == before and h.exchange.orders[tp].algo_status == "NEW"


# ---------------------------------------------------------------- 旧账本


async def test_old_positions_json_without_field_loads_as_legacy_types(tmp_path: Path) -> None:
    h = Harness(tmp_path, Side.LONG)
    assert await h.manager.sync("pos-1") == "armed"
    data = json.loads(h.path.read_text(encoding="utf-8"))
    for leg in data["positions"]["pos-1"]["server_protection"]["legs"]:
        del leg["working_type"]
    h.path.write_text(json.dumps(data), encoding="utf-8")
    h.restart()
    _set(h, MARK, MARK)  # 新设置不改旧腿的含义。
    persisted = {leg.purpose: leg.working_type for leg in h.state().legs}
    assert persisted == {"lifeboat": MARK, "fixed": LAST}
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 3 and not h.cancels()
    reloaded = PositionRegistry(h.path).get("pos-1")
    assert reloaded is not None and reloaded.server_protection is not None
    assert {leg.purpose: leg.working_type for leg in reloaded.server_protection.legs} == persisted


def test_lifeboat_leg_must_be_mark_price() -> None:
    leg = {
        "kind": "stop_loss",
        "purpose": "lifeboat",
        "generation": 1,
        "client_order_id": "lb-pos-1-1",
        "quantity": 1.0,
        "trigger_price": 1.0,
    }
    assert ProtectiveLeg.model_validate(leg).working_type == MARK
    with pytest.raises(ValidationError):
        ProtectiveLeg.model_validate({**leg, "working_type": LAST})
    with pytest.raises(ValidationError):
        ProtectiveLeg.model_validate({**leg, "purpose": "fixed", "working_type": "LAST_PRICE"})
