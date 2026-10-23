# 换电脑 / 新装一台 Mac

新机器上只做这一条：

```bash
git clone https://github.com/827aaron/ai-jiaoyi-2.0 && cd ai-jiaoyi-2.0 && bash scripts/bootstrap_mac.sh
```

向导会自己装 Homebrew 依赖、起 colima、装 Python 依赖、拉 hummingbot-api / Condor / Kronos、
装开机自启（colima、Condor、每日备份、看门狗）、设防睡眠，最后启动面板并打开
<http://127.0.0.1:8090/setup>。**密钥、面板密码、基本配置、自检都在那一页里点完**，不用再敲命令。

- 向导结束时会探测 `/setup`：打不开（页面还没上线）就直接打印终端命令，不会让你落在 404。后备命令：终端里 `.venv/bin/jiaoyi set-panel-password`，再 `.venv/bin/jiaoyi setup-keys HUMMINGBOT_API_PASSWORD BINANCE_TESTNET_API_KEY BINANCE_TESTNET_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID OPENROUTER_API_KEY`（只在提示处粘贴），最后 `.venv/bin/jiaoyi check-config`。
- 中途断了（断网、Ctrl+C）直接重新运行同一条，做完的步骤会跳过；想全部重来加 `--redo`。
- 唯一要在终端里输入的是 hummingbot-api 的用户名和密码（上游的 setup，向导会提示）。
- 币安 Demo 的 key 要交给 hummingbot-api 一次，见 `docs/M0-setup.md` 第 3 步末尾。
- 首次设置做完、纸面跑通之后：`scripts/install_launchd.sh --load-run`，主程序才开机自启。
- 以后更新代码：`make update`。

## 从旧电脑带走什么

| 要带 | 位置 | 说明 |
|---|---|---|
| 配置 | `config/app.toml`、`config/risk.toml`、`config/slow.toml` | 拷过去就不用重填；`risk.toml` 变了实盘要重新 `go-live` |
| 历史与账本 | `data/`（尤其 `decision_log/`、`backups/`） | 想保留历史才带；不带也能跑 |
| 密钥 | 旧机钥匙串（服务名 `jiaoyi`） | **不用搬**：在新机面板「首次设置」里重新填，旧的 key 换新更干净 |
| hummingbot 账号 | `vendor/hummingbot-api/.env` | 不用搬，向导重新 setup |

旧机上的密钥建议在新机跑通后作废重建（币安 key、Telegram token、OpenRouter key）。
