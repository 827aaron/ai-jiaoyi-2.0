#!/bin/bash
# 新 Mac 一键安装向导：从空机器走到“面板首次设置页已打开”。
#
#   git clone https://github.com/827aaron/ai-jiaoyi-2.0 && cd ai-jiaoyi-2.0 && bash scripts/bootstrap_mac.sh
#
# 它只装软件和后台服务；密钥、面板密码、基本配置、自检都在面板的「首次设置」页里做
# （http://127.0.0.1:8090/setup，密钥页只有本机能打开）。
# 可重复运行、可中断续跑：做完的步骤记在 data/.bootstrap_done，下次直接跳过；
# 想全部重来：bash scripts/bootstrap_mac.sh --redo
set -euo pipefail

REPO_URL="https://github.com/827aaron/ai-jiaoyi-2.0"
PANEL_PORT=8090
SETUP_URL="http://127.0.0.1:$PANEL_PORT/setup"

say() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || die "这个向导只支持 macOS。"
[ "$(id -u)" -ne 0 ] || die "不要用 sudo 运行；需要管理员密码的地方会自己问。"

# 在仓库里就用这个仓库；否则（例如 curl | bash）克隆到 ~/ai-jiaoyi-2.0 再从那里重新执行
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/../pyproject.toml" ]; then
  ROOT="$(cd "$SELF_DIR/.." && pwd)"
else
  ROOT="$HOME/ai-jiaoyi-2.0"
  command -v git >/dev/null 2>&1 || xcode-select --install || true
  [ -d "$ROOT/.git" ] || git clone "$REPO_URL" "$ROOT"
  exec bash "$ROOT/scripts/bootstrap_mac.sh" "$@" </dev/tty
fi
cd "$ROOT"

if [ "${1:-}" = "--redo" ]; then rm -f data/.bootstrap_done; fi
mkdir -p data/logs
DONE="$ROOT/data/.bootstrap_done"
touch "$DONE"

# step <名字> <中文说明> <函数>：做过就跳过；函数失败就停下，修好后重跑即可续上
step() {
  local name="$1" text="$2" fn="$3"
  if grep -qx "$name" "$DONE"; then
    note "✓ 已完成，跳过：$text"
    return 0
  fi
  say "$text"
  "$fn"
  echo "$name" >>"$DONE"
}

BREW_PACKAGES=(git uv colima docker docker-compose tmux node)

s_brew() {
  if ! command -v brew >/dev/null 2>&1; then
    note "没有 Homebrew，先装它（会要你的 Mac 登录密码）。"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
  local p
  for p in "${BREW_PACKAGES[@]}"; do
    if brew list --formula "$p" >/dev/null 2>&1; then note "已装：$p"; else brew install "$p"; fi
  done
}

s_colima() {
  # docker 要靠 colima 跑（不是 Docker Desktop）；开机自启交给 install_launchd.sh
  if colima status >/dev/null 2>&1; then note "colima 已在运行"; else colima start; fi
  docker info >/dev/null 2>&1 || die "docker 连不上 colima，运行 colima status 看看。"
}

s_code() {
  git pull --ff-only || note "git pull 没成功（有本地改动？）——继续用当前代码。"
}

s_install() {
  make install
  # 慢环（军师团）和预测员（Kronos）的依赖；Kronos 带 torch，体积大，几分钟
  uv pip install --python .venv/bin/python -e ".[dev,slow,kronos]"
  make kronos-fetch
  make init-config
}

s_hb_fetch() { make hb-fetch; }

s_hb_setup() {
  if [ -f vendor/hummingbot-api/.env ]; then
    note "hummingbot-api 已 setup 过（vendor/hummingbot-api/.env 存在）"
    return 0
  fi
  note "下面是上游的交互式 setup：它会问 API 用户名 / 密码 / 配置密码。"
  note "用户名默认 admin；密码自己定一个并记住——稍后在面板「首次设置」里要再填一次。"
  make hb-setup </dev/tty
}

s_hb_up() { make hb-up; }

s_condor() { make condor-install; }

s_launchd() {
  # 不带 --load-run：主程序要等密钥配好、纸面跑通之后再装
  scripts/install_launchd.sh
}

s_pmset() {
  note "让 Mac mini 不睡眠、断电来电后自动开机（需要管理员密码，可以跳过）。"
  local ans=""
  read -r -p "  现在设置吗？[Y/n] " ans </dev/tty || true
  case "${ans:-Y}" in
    [Nn]*) note "跳过。以后手动：sudo pmset -a sleep 0 disksleep 0 autorestart 1" ;;
    *) sudo pmset -a sleep 0 disksleep 0 autorestart 1 ;;
  esac
}

s_panel() {
  if curl -fsS -o /dev/null "http://127.0.0.1:$PANEL_PORT/" 2>/dev/null; then
    note "面板已经在 $PANEL_PORT 端口运行"
  else
    nohup .venv/bin/jiaoyi panel --host 127.0.0.1 --port "$PANEL_PORT" \
      >>data/logs/panel-setup.log 2>&1 &
    disown
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 1
      curl -fsS -o /dev/null "http://127.0.0.1:$PANEL_PORT/" 2>/dev/null && break
    done
  fi
}

step brew    "1/10 装 Homebrew 和依赖（uv colima docker tmux node …）" s_brew
step colima  "2/10 启动 colima（跑 Docker）" s_colima
step code    "3/10 更新代码" s_code
step install "4/10 装 Python 依赖、拉 Kronos 源码、生成配置文件" s_install
step hbfetch "5/10 拉 hummingbot-api 和 Condor 源码" s_hb_fetch
step hbsetup "6/10 hummingbot-api 初始设置（要你输入用户名和密码）" s_hb_setup
step hbup    "7/10 启动 hummingbot-api（Docker）" s_hb_up
step condor  "8/10 安装 Condor" s_condor
step launchd "9/10 开机自启：colima、Condor、每日备份、看门狗" s_launchd
step pmset   "10/10 防睡眠与断电自启" s_pmset

# 面板每次都确认一下（重启后它不在了就重新拉起）
say "启动面板"
s_panel
if open "$SETUP_URL" 2>/dev/null; then :; fi

cat <<EOF

────────────────────────────────────────
装好了。接下来在浏览器里完成「首次设置」：
  $SETUP_URL
（没自动打开就手动复制这个地址。）

那里依次填：密钥、面板密码、基本配置，最后点自检。
币安 Demo 的 key 还要交给 hummingbot-api 一次：浏览器开 http://127.0.0.1:8000/docs，
POST /accounts/add-credential/master_account/binance_perpetual_testnet（详见 docs/M0-setup.md 第 3 步）。

首次设置做完、纸面跑通后，再让主程序开机自启：
  scripts/install_launchd.sh --load-run
以后更新代码：make update
EOF
