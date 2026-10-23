"""启动时核对：hummingbot 用的币安账户 = 我们自己的 ccxt 用的币安账户。

保命止损是 ccxt 挂在“我们这把 key 的账户”上的；执行器开的仓在“hummingbot 那把 key 的账户”上。
两把 key 不是同一个账户（或一个是主账户一个是子账户、一个是 Demo 一个是实盘），保命止损就挂在
别人账户上，真仓位没有最后防线。这里比对 USDT 钱包余额和持仓列表，对不上就拒绝启动。

局限：这是间接核对（币安没有给 hummingbot 侧暴露账户 ID）。两个全新、空仓、余额相同的账户会
被当成同一个；有持仓或余额不同的会被查出来。
"""

from __future__ import annotations

from jiaoyi.execution.exchange import ExchangeClient, ExchangeError
from jiaoyi.execution.hb_client import HbApiError, HummingbotApi
from jiaoyi.execution.symbols import from_hb_pair


class AccountMismatch(RuntimeError):
    """hummingbot 和 ccxt 看到的不是同一个币安账户。消息是给人看的中文。"""


async def verify_same_account(
    api: HummingbotApi,
    exchange: ExchangeClient,
    account_name: str,
    connector_name: str,
    *,
    rel_tol: float = 0.02,
    abs_tol: float = 5.0,
) -> None:
    """余额容差 = max(abs_tol, 余额 × rel_tol)（两次读取之间有浮动盈亏和手续费）。

    读不到任一边抛 ``HbApiError`` / ``ExchangeError``（调用方决定是否因此拒绝启动）；
    读到了但对不上抛 ``AccountMismatch``。
    """
    balances = await api.portfolio_state(account_name, connector_name)
    usdt = next((b for b in balances if str(b.get("token")) == "USDT"), None)
    if usdt is None:
        raise HbApiError("hummingbot 账户里读不到 USDT 余额")
    hb_wallet = float(usdt.get("units") or 0.0)
    ex_wallet = await exchange.wallet_usdt()
    tol = max(abs_tol, max(hb_wallet, ex_wallet) * rel_tol)
    if abs(hb_wallet - ex_wallet) > tol:
        raise AccountMismatch(
            f"USDT 余额对不上：hummingbot {hb_wallet:.2f}，直连币安 {ex_wallet:.2f}"
            f"（容差 {tol:.2f}）。两边不是同一个账户，保命止损会挂错地方"
        )

    hb_symbols: set[str] = set()
    for p in await api.positions(account_name, connector_name):
        if abs(float(p.get("amount") or 0.0)) == 0.0:
            continue
        try:
            hb_symbols.add(from_hb_pair(str(p.get("trading_pair") or "")))
        except ValueError as e:
            raise AccountMismatch(f"读不懂 hummingbot 的持仓：{p!r}") from e
    ex_symbols = {p.symbol for p in await exchange.positions()}
    if hb_symbols != ex_symbols:
        raise AccountMismatch(
            f"持仓对不上：hummingbot {sorted(hb_symbols)}，直连币安 {sorted(ex_symbols)}"
        )


__all__ = ["AccountMismatch", "ExchangeError", "HbApiError", "verify_same_account"]
