"""把 hummingbot 的 binance_perpetual_testnet 连接器指向币安 Demo Trading。

上游把测试网域名写死在 binance_perpetual_constants.py（testnet.binancefuture.com），
币安已停用它。URL 构造函数对所有非 "binance_perpetual" 的域名都取这两个常量，
所以只改常量就能让整个连接器（REST、WS 行情、用户数据流）走 Demo。
找不到原文就报错退出，不静默放过（上游改了常量说明补丁要重审）。
"""

import importlib.util
import pathlib
import sys

REPLACEMENTS = {
    'TESTNET_BASE_URL = "https://testnet.binancefuture.com/fapi/"': 'TESTNET_BASE_URL = "https://demo-fapi.binance.com/fapi/"',
    'TESTNET_WS_URL = "wss://stream.binancefuture.com/"': 'TESTNET_WS_URL = "wss://demo-fstream.binance.com/"',
}

spec = importlib.util.find_spec(
    "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_constants"
)
if spec is None or spec.origin is None:
    sys.exit("找不到 binance_perpetual_constants，镜像里的 hummingbot 版本不对")
path = pathlib.Path(spec.origin)
text = path.read_text()
for old, new in REPLACEMENTS.items():
    if new in text:
        continue  # 已打过补丁，幂等
    if old not in text:
        sys.exit(f"{path} 里没有预期的行：{old}，上游改了，补丁要重审")
    text = text.replace(old, new)
path.write_text(text)
print("patched", path)
