"""命令行入口：``jiaoyi <命令>``。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from jiaoyi.core.config import (
    DEFAULT_APP_PATH,
    DEFAULT_RISK_PATH,
    ConfigError,
    load_app_config,
    load_risk_config,
)
from jiaoyi.core.secrets import KNOWN_KEYS, ChainedSecretStore, mask


def _check_config(args: argparse.Namespace) -> int:
    try:
        risk = load_risk_config(Path(args.risk))
        app = load_app_config(Path(args.app))
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    print(
        f"配置合格。模式 {app.mode}，每笔风险 {risk.risk_per_trade_pct}%，"
        f"最大杠杆 {risk.max_leverage}x，日亏熔断 {risk.daily_loss_halt_pct}%。"
    )
    return 0


def _key_status(_: argparse.Namespace) -> int:
    store = ChainedSecretStore()
    for name in KNOWN_KEYS:
        print(f"{name:<26} {mask(store.get(name))}")
    return 0


def _status(args: argparse.Namespace) -> int:
    from jiaoyi.app import App

    try:
        app = App(load_risk_config(Path(args.risk)), load_app_config(Path(args.app)))
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    for role, st in app.status().items():
        print(f"{role.value:<12} {st.health.value:<9} {st.headline}")
    asyncio.run(app.stop())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jiaoyi")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check-config", help="检查硬上限和运行配置，不合格返回非 0")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.set_defaults(func=_check_config)

    p = sub.add_parser("status", help="团队页的文字版：每个角色的状态")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.set_defaults(func=_status)

    p = sub.add_parser("key-status", help="显示每个密钥配没配（只显示末四位）")
    p.set_defaults(func=_key_status)

    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
