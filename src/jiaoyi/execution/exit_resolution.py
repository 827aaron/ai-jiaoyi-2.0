"""人工结束对账员的平仓意图（``jiaoyi resolve-exit``）。

原 CID 一直查不到时，意图按设计不删、不重发，同币也禁止新开。人在币安上核实后，用命令结束它。
命令进程不直接改 ``positions.json``（主循环内存里的账本下次保存会把它写回去），而是写一条请求到
``data/exit_resolutions/<id>.json``；对账员下一轮在同币锁里再核对一遍 CID 和净仓后执行，
结果写到 ``done/<id>.json``，原请求删掉。同一个 id 只执行一次。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from jiaoyi.contracts import RunMode, Side
from jiaoyi.execution.exchange import CcxtBinanceFutures, ExchangePosition
from jiaoyi.execution.registry import RegistryData
from jiaoyi.interfaces import SecretStore

RESOLUTIONS_DIR = "exit_resolutions"


class ResolutionRequest(BaseModel):
    """一条人工结束请求：币种和原 CID 必须和账本里那条未完成意图完全一致。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    ts: AwareDatetime
    symbol: str
    client_order_id: str
    force: bool = False
    """为真时，币安严格持仓仍非零（或读不到）也结束。"""
    reason: str = ""
    observed_quantity: float | None = None
    """命令侧严格读到的带方向净仓（多 +、空 −）；读不到为 None。"""


class ResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    applied: bool
    detail: str
    request: ResolutionRequest | None = None
    removed: dict[str, Any] | None = None
    """被删掉的那条意图（执行了才有）。"""


def resolutions_dir(registry_path: Path) -> Path:
    return registry_path.parent / RESOLUTIONS_DIR


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_resolution(root: Path, req: ResolutionRequest) -> Path:
    path = root / f"{req.id}.json"
    _write_atomic(path, req.model_dump_json(indent=2))
    return path


def pending_resolutions(root: Path) -> list[tuple[Path, ResolutionRequest | None]]:
    """待执行的请求（按文件名）；格式不对的返回 None，由调用方记成未执行。
    同一个 id 已有结果的（重放）直接删掉，不执行、不覆盖原结果。"""
    if not root.is_dir():
        return []
    out: list[tuple[Path, ResolutionRequest | None]] = []
    for path in sorted(root.glob("*.json")):
        try:
            req = ResolutionRequest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            req = None
        if req is not None and (root / "done" / f"{req.id}.json").exists():
            path.unlink(missing_ok=True)
            continue
        out.append((path, req))
    return out


def finish_resolution(
    root: Path,
    path: Path,
    req: ResolutionRequest | None,
    applied: bool,
    detail: str,
    removed: dict[str, Any] | None = None,
) -> None:
    rid = req.id if req is not None else path.stem
    result = ResolutionResult(id=rid, applied=applied, detail=detail, request=req, removed=removed)
    _write_atomic(root / "done" / f"{rid}.json", result.model_dump_json(indent=2))
    path.unlink(missing_ok=True)


def read_result(root: Path, rid: str) -> ResolutionResult | None:
    path = root / "done" / f"{rid}.json"
    if not path.is_file():
        return None
    return ResolutionResult.model_validate_json(path.read_text(encoding="utf-8"))


PositionsRead = Callable[[], Awaitable[list[ExchangePosition]]]


def strict_exchange(
    mode: RunMode, sim_environment: str, secrets: SecretStore
) -> CcxtBinanceFutures:
    """命令侧只读查持仓用的币安客户端，按运行模式取主网 / 测试网的 key（和执行层同一套）。"""
    from jiaoyi.execution.factory import MissingSecret

    prefix = "BINANCE" if mode is RunMode.LIVE else "BINANCE_TESTNET"
    key, secret = secrets.get(f"{prefix}_API_KEY"), secrets.get(f"{prefix}_API_SECRET")
    if not key or not secret:
        raise MissingSecret(
            f"缺少密钥 {prefix}_API_KEY / {prefix}_API_SECRET，先运行 jiaoyi setup-keys"
        )
    return CcxtBinanceFutures(
        key, secret, testnet=mode is not RunMode.LIVE, demo=sim_environment == "demo"
    )


async def prepare_resolution(
    registry_path: Path,
    positions_read: PositionsRead,
    symbol: str,
    client_order_id: str,
    *,
    force: bool,
    reason: str,
    now: datetime,
) -> tuple[ResolutionRequest | None, str]:
    """命令侧检查：账本里这个币有未完成意图且 CID 完全一致；严格持仓非零或读不到时，
    没有 ``force`` 就拒绝。返回（请求或 None，给人看的说明）。"""
    try:
        data = RegistryData.model_validate(json.loads(registry_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        return None, f"读不了账本 {registry_path}：{e}"
    plan = data.emergency_exits.get(symbol)
    if plan is None:
        return None, f"{symbol} 没有未完成的对账员平仓意图"
    if plan.client_order_id != client_order_id:
        return None, f"CID 对不上：账本里 {symbol} 的平仓单是 {plan.client_order_id}"
    observed: float | None
    try:
        rows = [p for p in await positions_read() if p.symbol == symbol]
        observed = sum(p.quantity if p.side is Side.LONG else -p.quantity for p in rows)
    except Exception as e:
        if not force:
            return None, f"读不到币安严格持仓（{e!r}）；确认过要结束请加 --force"
        observed = None
    if observed and not force:
        return None, f"币安上 {symbol} 仍有净仓 {observed:g}；确认过要结束请加 --force"
    req = ResolutionRequest(
        id=f"rx-{uuid.uuid4().hex[:16]}",
        ts=now,
        symbol=symbol,
        client_order_id=client_order_id,
        force=force,
        reason=reason,
        observed_quantity=observed,
    )
    seen = "读不到" if observed is None else f"{observed:g}"
    text = (
        f"意图 {plan.exit_id}（{plan.source}，第 {plan.generation} 代，状态 "
        f"{plan.order_status}，最初 {plan.initial_quantity:g}，已成交 {plan.closed_quantity:g}）；"
        f"币安严格净仓 {seen}"
    )
    return req, text
