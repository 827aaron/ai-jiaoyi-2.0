"""让 hummingbot 的币安永续连接器认美股 / 金属永续（contractType = TRADIFI_PERPETUAL）。

老版本 ``is_exchange_information_valid`` 只收 ``contractType == "PERPETUAL"``，TSLAUSDT 这类
合约没有交易规则，执行器下不了单。补丁把判断改成 PERPETUAL 或 TRADIFI_PERPETUAL。

新版本已经认 TRADIFI 时什么都不做；找不到 contractType 判断（说明上游不按它过滤）也不动；
找到别的写法报错退出，不静默放过（上游改了说明补丁要重审）。
"""

import importlib.util
import pathlib
import re
import sys

MODULES = (
    "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_web_utils",
    "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_utils",
)
MARK = "TRADIFI_PERPETUAL"
_CHECK = re.compile(
    r"""(rule(?:\[["']contractType["']\]|\.get\(["']contractType["']\)))\s*==\s*["']PERPETUAL["']"""
)


def patch(text: str) -> str:
    """返回打过补丁的源码；已认 TRADIFI 或没有 contractType 判断时原样返回。"""
    if MARK in text or "def is_exchange_information_valid" not in text:
        return text
    new, n = _CHECK.subn(r'\1 in ("PERPETUAL", "TRADIFI_PERPETUAL")', text)
    if n == 0 and "contractType" in text:
        raise ValueError("is_exchange_information_valid 里的 contractType 判断认不出")
    return new


def main() -> None:
    found = False
    for name in MODULES:
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError:
            spec = None
        if spec is None or spec.origin is None:
            continue
        path = pathlib.Path(spec.origin)
        text = path.read_text()
        if "def is_exchange_information_valid" not in text:
            continue
        found = True
        try:
            new = patch(text)
        except ValueError as e:
            sys.exit(f"{path}：{e}，上游改了，补丁要重审")
        if new != text:
            compile(new, str(path), "exec")  # 打坏了就在构建时失败
            path.write_text(new)
        print("patched", path)
    if not found:
        sys.exit("找不到 is_exchange_information_valid，镜像里的 hummingbot 版本不对")


if __name__ == "__main__":
    main()
