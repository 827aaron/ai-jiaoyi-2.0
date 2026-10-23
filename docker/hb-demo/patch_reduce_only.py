"""让 hummingbot 执行器的平仓单（止损 / 止盈 / 到时平 / 停执行器平仓）永远只减仓。

上游 ``PositionExecutor.close_position_action`` 在单向持仓（ONEWAY）下返回 ``PositionAction.OPEN``，
平仓单不带 reduce-only。我们每个币只有一个执行器，不存在上游说的「多个执行器互相对冲」；反而币安上的
保命止损（只减仓 STOP_MARKET）先把仓平掉之后，执行器的平仓单再成交一次，就会反向开出一笔同样大小、
没有止损的仓。补丁把它改成永远返回 ``PositionAction.CLOSE``：仓位已经是 0 时平仓单被币安拒绝
（ReduceOnly rejected），执行器重试到上限后结束，对账员按「外部平仓」结算。

找不到原文就报错退出，不静默放过（上游改了说明补丁要重审）；已打过补丁时什么都不做。
"""

import importlib.util
import pathlib
import re
import sys

MODULE = "hummingbot.strategy_v2.executors.position_executor.position_executor"
MARK = "# jiaoyi: 平仓永远只减仓"
_PROPERTY = re.compile(
    r"(    @property\n    def close_position_action\(self\) -> PositionAction:\n)"
    r"(.*?)"
    r"(?=\n    @property\n|\n    def |\n    async def )",
    re.S,
)
BODY = (
    f'        """平仓单永远只减仓（单向持仓也是）。{MARK}"""\n        return PositionAction.CLOSE\n'
)


def patch(text: str) -> str:
    """返回打过补丁的源码。已打过、或老版本平仓本来就写死 ``PositionAction.CLOSE`` 时原样返回；
    两样都找不到抛 ValueError。"""
    if MARK in text:
        return text
    m = _PROPERTY.search(text)
    if m is None:
        if "close_position_action" not in text and "position_action=PositionAction.CLOSE" in text:
            return text  # 老版本：平仓单直接写 CLOSE，本来就是只减仓
        raise ValueError("找不到 close_position_action 属性")
    return text[: m.start(2)] + BODY + text[m.end(2) :]


def main() -> None:
    spec = importlib.util.find_spec(MODULE)
    if spec is None or spec.origin is None:
        sys.exit("找不到 position_executor，镜像里的 hummingbot 版本不对")
    path = pathlib.Path(spec.origin)
    text = path.read_text()
    try:
        new = patch(text)
    except ValueError as e:
        sys.exit(f"{path}：{e}，上游改了，补丁要重审")
    if new != text:
        compile(new, str(path), "exec")  # 打坏了就在构建时失败
        path.write_text(new)
    print("patched", path)


if __name__ == "__main__":
    main()
