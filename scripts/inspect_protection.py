"""只读诊断：核对某笔持仓在交易所上的保护单，找出 ProtectionIncomplete 的具体原因。

用法（在仓库根目录，用项目 venv）：
    .venv/bin/python scripts/inspect_protection.py pos-59ed6a95c388 pos-13a7eb1adba1

只做 GET 查询，不下单、不撤单、不改任何本地文件；结果写到 data/debug/protection_inspect_<时间>.json。
输出里没有密钥。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from jiaoyi.core.secrets import ChainedSecretStore
from jiaoyi.execution.exchange import CcxtBinanceFutures, _bracket_from

ROOT = Path(__file__).resolve().parents[1]


def _jsonable(obj: object) -> object:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def _accept_checks(tracked: dict, leg: dict, order) -> list[str]:
    """复刻 BracketManager._accept 的每一项比较，返回不通过的项。"""
    rec = tracked["record"]
    close_side = "sell" if rec["side"] == "long" else "buy"
    expect_wt = "MARK_PRICE" if leg["purpose"] == "lifeboat" else "CONTRACT_PRICE"
    fails = []
    if order.symbol != rec["symbol"]:
        fails.append(f"symbol {order.symbol!r} != {rec['symbol']!r}")
    if order.client_order_id != leg["client_order_id"]:
        fails.append(f"cid {order.client_order_id!r} != {leg['client_order_id']!r}")
    if order.kind != leg["kind"]:
        fails.append(f"kind {order.kind!r} != {leg['kind']!r}")
    if order.close_side != close_side:
        fails.append(f"close_side {order.close_side!r} != {close_side!r}")
    if order.position_side != "BOTH":
        fails.append(f"position_side {order.position_side!r}")
    if order.working_type != expect_wt:
        fails.append(f"working_type {order.working_type!r} != {expect_wt!r}")
    if not order.reduce_only:
        fails.append("reduce_only is False")
    if order.price_protect:
        fails.append("price_protect is True")
    if order.close_position:
        fails.append("close_position is True")
    if order.quantity != leg["quantity"]:
        fails.append(f"quantity {order.quantity!r} != {leg['quantity']!r}")
    if order.trigger_price != leg["trigger_price"]:
        fails.append(f"trigger_price {order.trigger_price!r} != {leg['trigger_price']!r}")
    if leg.get("algo_id") is not None and leg["algo_id"] != order.algo_id:
        fails.append(f"algo_id {order.algo_id!r} != {leg['algo_id']!r}")
    return fails


async def main(position_ids: list[str]) -> None:
    store = ChainedSecretStore()
    key, secret = store.get("BINANCE_TESTNET_API_KEY"), store.get("BINANCE_TESTNET_API_SECRET")
    if not key or not secret:
        sys.exit("钥匙串里没有 BINANCE_TESTNET_API_KEY / SECRET")
    registry = json.loads((ROOT / "data" / "positions.json").read_text())["positions"]
    ex = CcxtBinanceFutures(key, secret, testnet=True, demo=True)
    report: dict[str, object] = {"inspected_at": datetime.now(UTC).isoformat(), "positions": {}}
    try:
        await ex._ready()
        try:
            report["position_mode"] = await ex.position_mode()
        except Exception as e:  # noqa: BLE001
            report["position_mode"] = f"ERR {type(e).__name__}: {e}"
        try:
            report["open_positions_now"] = _jsonable(await ex.positions_evidence())
        except Exception as e:  # noqa: BLE001
            report["open_positions_now"] = f"ERR {type(e).__name__}: {e}"
        for pid in position_ids:
            tracked = registry.get(pid)
            if tracked is None:
                report["positions"][pid] = "positions.json 里没有这个 position_id"
                continue
            rec = tracked["record"]
            out: dict[str, object] = {
                "symbol": rec["symbol"],
                "side": rec["side"],
                "opened_at": rec["opened_at"],
                "closed_at": rec["closed_at"],
                "close_reason": rec["close_reason"],
                "entry_cid": tracked.get("entry_client_order_id"),
                "server_protection_stage": (tracked.get("server_protection") or {}).get("stage"),
                "ever_armed": (tracked.get("server_protection") or {}).get("ever_armed"),
                "last_error": (tracked.get("server_protection") or {}).get("last_error"),
                "entry_order": None,
                "legs": [],
            }
            cid = tracked.get("entry_client_order_id")
            if cid:
                try:
                    out["entry_order"] = _jsonable(await ex.entry_order(rec["symbol"], cid))
                except Exception as e:  # noqa: BLE001
                    out["entry_order"] = f"ERR {type(e).__name__}: {e}"
            for leg in (tracked.get("server_protection") or {}).get("legs", []):
                item: dict[str, object] = {
                    "local": {
                        k: leg.get(k)
                        for k in (
                            "purpose", "kind", "state", "client_order_id", "algo_id",
                            "algo_status", "child_state", "quantity", "trigger_price",
                        )
                    }
                }
                try:
                    raw = await ex._ex.fapiPrivateGetAlgoOrder(
                        {"clientAlgoId": leg["client_order_id"]}
                    )
                    item["exchange_raw"] = raw
                    try:
                        order = _bracket_from(raw, lifeboat=leg["purpose"] == "lifeboat")
                        item["parsed"] = _jsonable(order)
                        item["accept_failures"] = _accept_checks(tracked, leg, order)
                        item["would_be_live"] = bool(
                            order.algo_status == "NEW"
                            and order.child_state == "absent"
                            and order.actual_order_id is None
                            and order.triggered_at is None
                        )
                        item["child_state_detail"] = {
                            "actualOrderId": raw.get("actualOrderId"),
                            "actualOrderId_type": type(raw.get("actualOrderId")).__name__,
                            "triggerTime": raw.get("triggerTime"),
                            "triggerTime_type": type(raw.get("triggerTime")).__name__,
                        }
                    except Exception as e:  # noqa: BLE001
                        item["parse_error"] = f"{type(e).__name__}: {e}"
                except Exception as e:  # noqa: BLE001
                    item["exchange_raw"] = f"ERR {type(e).__name__}: {e}"
                out["legs"].append(item)
            # 同币现在还有没有挂着的条件单
            try:
                open_algos = await ex._ex.fapiPrivateGetOpenAlgoOrders({"symbol": rec["symbol"]})
                out["open_algo_orders_now"] = open_algos
            except Exception as e:  # noqa: BLE001
                out["open_algo_orders_now"] = f"ERR {type(e).__name__}: {e}"
            report["positions"][pid] = out
    finally:
        await ex._ex.close()
    dest = ROOT / "data" / "debug"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"protection_inspect_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\n已写到 {path}")


if __name__ == "__main__":
    ids = sys.argv[1:] or ["pos-59ed6a95c388", "pos-13a7eb1adba1"]
    asyncio.run(main(ids))
