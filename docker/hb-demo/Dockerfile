# 在上游 hummingbot-api 镜像上打补丁：
# 1. binance_perpetual_testnet 连接器改连币安 Demo Trading（patch_demo.py）；
# 2. 执行器的平仓单永远只减仓（patch_reduce_only.py）：保命止损先成交后不会再反向开仓；
# 3. 币安永续连接器认美股 / 金属永续 TRADIFI_PERPETUAL（patch_tradifi.py）。
# 构建方式见 Makefile 的 hb-patch（会重新打成 hummingbot/hummingbot-api:latest 本地标签，
# 这样上游 compose 不用改）。
# DEMO=0（旧测试网）不打第 1 个。
ARG BASE=hummingbot/hummingbot-api:latest
FROM ${BASE}
ARG DEMO=1
COPY patch_demo.py patch_reduce_only.py patch_tradifi.py /tmp/
RUN if [ "$DEMO" = "1" ]; then python /tmp/patch_demo.py; fi \
    && python /tmp/patch_reduce_only.py \
    && python /tmp/patch_tradifi.py \
    && rm /tmp/patch_demo.py /tmp/patch_reduce_only.py /tmp/patch_tradifi.py
