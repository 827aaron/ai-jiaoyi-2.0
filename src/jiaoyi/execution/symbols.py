"""代码格式互转：契约里是 ``BTCUSDT``，hummingbot 是 ``BTC-USDT``，ccxt 是 ``BTC/USDT:USDT``。"""

from __future__ import annotations

_QUOTE = "USDT"


def to_hb_pair(symbol: str) -> str:
    if not symbol.endswith(_QUOTE) or len(symbol) == len(_QUOTE):
        raise ValueError(f"不是 U 本位永续代码：{symbol}")
    return f"{symbol[: -len(_QUOTE)]}-{_QUOTE}"


def from_hb_pair(pair: str) -> str:
    base, _, quote = pair.partition("-")
    if quote != _QUOTE or not base:
        raise ValueError(f"不是 USDT 交易对：{pair}")
    return f"{base}{quote}"


def to_ccxt_symbol(symbol: str) -> str:
    base = to_hb_pair(symbol).split("-")[0]
    return f"{base}/{_QUOTE}:{_QUOTE}"


def from_ccxt_symbol(ccxt_symbol: str) -> str:
    base, _, rest = ccxt_symbol.partition("/")
    if rest != f"{_QUOTE}:{_QUOTE}" or not base:
        raise ValueError(f"不是 USDT 本位永续：{ccxt_symbol}")
    return f"{base}{_QUOTE}"
