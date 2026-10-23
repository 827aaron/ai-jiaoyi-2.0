# 在上游 hummingbot-api 镜像上打补丁：binance_perpetual_testnet 连接器改连币安 Demo Trading。
# 构建方式见 Makefile 的 hb-patch（会重新打成 hummingbot/hummingbot-api:latest 本地标签，
# 这样上游 compose 不用改）。
ARG BASE=hummingbot/hummingbot-api:latest
FROM ${BASE}
COPY patch_demo.py /tmp/patch_demo.py
RUN python /tmp/patch_demo.py && rm /tmp/patch_demo.py
