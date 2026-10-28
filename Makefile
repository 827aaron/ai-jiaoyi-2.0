# 常用命令。make help 看全部。
SHELL := /bin/bash
VENV  := .venv
BIN   := $(VENV)/bin

# 上游仓库钉在设计时读过的提交（设计文档附录 B）。升级先在测试网验证。
HB_API_REPO := https://github.com/hummingbot/hummingbot-api
HB_API_REF  := 7ae108f11bf20651682dbdff639a8717068c5883
CONDOR_REPO := https://github.com/hummingbot/condor
KRONOS_REPO := https://github.com/shiyu-coder/Kronos
KRONOS_REF  := 67b630e67f6a18c9e9be918d9b4337c960db1e9a
CONDOR_REF  := 8e4860fa3d7efd4b55c2b74dd7f1f439e5a4f32c

.PHONY: help install update update-after-pull init-config check-config lint format typecheck test check \
        kronos-fetch hb-fetch hb-patch hb-setup hb-up hb-down hb-logs condor-install condor-run

help:
	@echo "开发："
	@echo "  make install        建 .venv 并安装（含开发依赖和军师团 slow 依赖）"
	@echo "  make update         日常更新：git pull --ff-only → install → 拉预测模型权重 → 合并待更新配置 → check-config → 重启 jiaoyi run"
	@echo "  make init-config    从示例复制 config/risk.toml、config/app.toml（不覆盖已有）"
	@echo "  make check-config   检查配置，不合格返回非 0"
	@echo "  make check          lint + 类型检查 + 测试（同 CI）"
	@echo "  make format         自动格式化"
	@echo "执行层（M0，执行工程师）："
	@echo "预测员："
	@echo "  make kronos-fetch   拉官方 Kronos 源码到 vendor/Kronos，并预先拉最大 / 标准模型权重（都钉提交号）"
	@echo "  make hb-fetch       拉上游 hummingbot-api 和 Condor 到 vendor/"
	@echo "  make hb-setup       跑 hummingbot-api 上游 setup（生成它的 .env 和 MQTT 账号）"
	@echo "  make hb-patch       给 hummingbot-api 镜像打 Demo Trading 补丁（hb-up 会自动做）"
	@echo "  make hb-up / hb-down / hb-logs"
	@echo "  make condor-install / condor-run   Condor 官方本机运行（uv + tmux）"

install:
	@# 已有 .venv 就跳过创建（uv venv 对已存在的目录会报错），重复运行只补装依赖
	@if [ -x $(BIN)/python ]; then echo "$(VENV) 已存在，跳过创建"; else uv venv $(VENV); fi
	uv pip install --python $(BIN)/python -e ".[dev,slow]"

# 日常更新（Mac mini）。依赖装完、配置检查通过之后才重启，重启本身几秒，不会触发看门狗（2 分钟阈值）。
# 拉完代码交给子 make：make 已经读进内存的是旧 Makefile，后面的步骤要用刚拉下来的。
update:
	git pull --ff-only
	$(MAKE) update-after-pull

update-after-pull:
	$(MAKE) install
	@# 预测员权重（最大 / 标准）先拉好，已有缓存就跳过；失败不挡更新，第一次推理时会再下载
	-@if [ -d vendor/Kronos ]; then $(BIN)/python -m jiaoyi.predictor.fetch_weights; fi
	@# 待更新配置（config/*.pending.toml）用新代码合并、校验；不合格就不动正式配置，不挡更新
	-$(BIN)/jiaoyi apply-pending-config
	$(MAKE) check-config
	@# 面板放最后重启：面板里的「更新到最新版」按钮就是它拉起的 make，先重启面板会把自己杀掉
	@if launchctl print gui/$$(id -u)/com.jiaoyi.run >/dev/null 2>&1; then \
		launchctl kickstart -k gui/$$(id -u)/com.jiaoyi.run && echo "已重启 com.jiaoyi.run"; \
	else \
		echo "com.jiaoyi.run 还没装进 launchd：纸面跑通后执行 scripts/install_launchd.sh --load-run；在前台跑的请自己重启 jiaoyi run"; \
	fi
	@if launchctl print gui/$$(id -u)/com.jiaoyi.panel >/dev/null 2>&1; then \
		echo "重启 com.jiaoyi.panel（最后一步）"; \
		launchctl kickstart -k gui/$$(id -u)/com.jiaoyi.panel; \
	fi

init-config:
	@for f in risk app; do \
		if [ -f config/$$f.toml ]; then echo "config/$$f.toml 已存在，跳过"; \
		else cp config/$$f.example.toml config/$$f.toml && echo "已生成 config/$$f.toml"; fi; \
	done

check-config:
	$(BIN)/jiaoyi check-config

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/pytest

check: lint typecheck test

hb-fetch:
	@mkdir -p vendor
	@for spec in "hummingbot-api $(HB_API_REPO) $(HB_API_REF)" "condor $(CONDOR_REPO) $(CONDOR_REF)"; do \
		set -- $$spec; \
		if [ ! -d vendor/$$1/.git ]; then git clone $$2 vendor/$$1; fi; \
		git -C vendor/$$1 fetch --quiet origin && git -C vendor/$$1 checkout --quiet $$3; \
		echo "vendor/$$1 @ $$3"; \
	done

kronos-fetch:
	@mkdir -p vendor
	@if [ ! -d vendor/Kronos/.git ]; then git clone $(KRONOS_REPO) vendor/Kronos; fi
	@git -C vendor/Kronos fetch --quiet origin && git -C vendor/Kronos checkout --quiet $(KRONOS_REF)
	@echo "vendor/Kronos @ $(KRONOS_REF)"
	$(BIN)/python -m jiaoyi.predictor.fetch_weights

hb-setup:
	$(MAKE) -C vendor/hummingbot-api setup

# 币安已用 Demo Trading 取代旧测试网；hummingbot 连接器写死旧域名，这里打补丁（见 docs/M0-setup.md）。
# 回退旧测试网：HB_SIM=legacy_testnet make hb-up（同时把 config/app.toml 的 sim_environment 改成 legacy_testnet）。
# 镜像钉 digest（2026-10-15 推送的 latest，多架构 index）：升级先在 Demo 上跑冒烟，再改这里。
HB_SIM ?= demo
DATA_DIR ?= data
HB_IMAGE_DIGEST ?= sha256:c2406ccccd943bc361e3fb4cd06394ae27bae9318c71e30d079e7328c8103c14
HB_IMAGE_PINNED := hummingbot/hummingbot-api@$(HB_IMAGE_DIGEST)
HB_IMAGE_TAG := hummingbot/hummingbot-api:latest

hb-patch:
	docker pull $(HB_IMAGE_PINNED)
	docker build --build-arg BASE=$(HB_IMAGE_PINNED) --build-arg DEMO=$(if $(filter demo,$(HB_SIM)),1,0) -t $(HB_IMAGE_TAG) docker/hb-demo

hb-up:
	@# 两个分支都要重建本地 :latest 标签：legacy 不能带上次打的 Demo 补丁，否则 hummingbot 连 Demo
	@# 而 ccxt 连旧测试网，保命止损和执行器就分在两个环境；平仓只减仓的补丁两边都要打
	@if [ "$(HB_SIM)" = "demo" ] || [ "$(HB_SIM)" = "legacy_testnet" ]; then $(MAKE) hb-patch; \
	else echo "HB_SIM 只能是 demo 或 legacy_testnet"; exit 1; fi
	@# 记下 hummingbot 连的环境，jiaoyi 启动时和 sim_environment 对比，不一致拒绝启动
	@mkdir -p $(DATA_DIR) && echo "$(HB_SIM)" > $(DATA_DIR)/hb_sim_environment
	$(MAKE) -C vendor/hummingbot-api emqx-auth
	docker compose up -d

hb-down:
	docker compose down

hb-logs:
	docker compose logs -f --tail=200

condor-install:
	CONDOR_TELEMETRY=off $(MAKE) -C vendor/condor install

condor-run:
	CONDOR_TELEMETRY=off $(MAKE) -C vendor/condor run
