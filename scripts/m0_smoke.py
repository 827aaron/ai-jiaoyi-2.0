"""M0 冒烟：在测试网上跑通一笔 开仓 → 保命止损 → 平仓 → 保命止损被撤。

手动运行（需要 hummingbot-api 已启动、币安测试网 key 已进钥匙串，见 docs/M0-setup.md）::

    .venv/bin/python scripts/m0_smoke.py --symbol BTCUSDT --quantity 0.002

这是人跑的联调脚本，它自己造一条 OrderIntent 绕过风控员，所以：
  - 只允许 paper / testnet 模式，live 直接退出；
  - 不在 src/ 里，不属于运行时代码。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from jiaoyi.alerting.log import LogAlerter
from jiaoyi.contracts import OrderIntent, RunMode
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.core.secrets import ChainedSecretStore
from jiaoyi.execution.factory import build_execution

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "src" / "jiaoyi" / "contracts" / "examples" / "order_intent.json"


def check(ok: bool, text: str) -> bool:
    print(f"{'通过' if ok else '失败'}  {text}")
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--quantity", type=float, default=0.002)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--hold-s", type=float, default=20.0, help="持仓多久后手动平仓")
    ap.add_argument("--hb-url", default="http://127.0.0.1:8000")
    ap.add_argument("--hb-username", default="admin")
    args = ap.parse_args()

    app = load_app_config()
    risk = load_risk_config()
    if app.mode is RunMode.LIVE:
        print("冒烟脚本不允许在 live 模式运行", file=sys.stderr)
        return 2

    stack = build_execution(
        app.mode,
        risk,
        app.data_dir,
        ChainedSecretStore(),
        LogAlerter(),
        hb_url=args.hb_url,
        hb_username=args.hb_username,
    )
    results: list[bool] = []
    try:
        account = await stack.adapter.account()
        results.append(
            check(account.equity_usdt > 0, f"读到账户权益 {account.equity_usdt:.2f} USDT")
        )

        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data.update(
            intent_id=f"smoke-{uuid.uuid4().hex[:8]}",
            symbol=args.symbol,
            side=args.side,
            quantity=args.quantity,
            leverage=2,
            stop_loss_pct=1.0,
            take_profit_pct=2.0,
            lifeboat_stop_pct=1.5,
            time_limit_s=600,
        )
        intent = OrderIntent.model_validate(data)

        position = await stack.adapter.open(intent)
        results.append(
            check(position.status is PositionStatus.OPEN, f"开仓成交 {position.entry_price}")
        )
        stops = await stack.exchange.open_stops(args.symbol)
        mine = [s for s in stops if s.client_order_id == f"lb-{position.position_id}"]
        results.append(
            check(len(mine) == 1, f"币安上挂着保命止损 {mine[0].stop_price if mine else '-'}")
        )

        report = await stack.reconciler.reconcile(account.ts)
        results.append(check(report.clean, f"对账干净（{len(report.issues)} 个问题）"))

        print(f"持仓 {args.hold_s:.0f} 秒后平仓……")
        await asyncio.sleep(args.hold_s)
        await stack.adapter.close(position.position_id, CloseReason.MANUAL)
        remaining = await stack.adapter.positions()
        results.append(check(not remaining, "账本里已无在管持仓"))
        stops_after = [
            s
            for s in await stack.exchange.open_stops(args.symbol)
            if s.client_order_id == f"lb-{position.position_id}"
        ]
        results.append(check(not stops_after, "平仓后保命止损已撤"))
        flat = not [p for p in await stack.exchange.positions() if p.symbol == args.symbol]
        results.append(check(flat, "币安上已无该标的仓位"))
    finally:
        await stack.close()
    print("全部通过" if all(results) else "有失败项，见上")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
