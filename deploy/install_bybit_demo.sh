#!/usr/bin/env bash
# One-click installer for Ubuntu/Debian VPS control-panel terminals (e.g. aaPanel/宝塔).
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="bybit-n8n"
APP_GROUP="bybit-n8n"
VENV="$APP_DIR/.venv"
ENV_FILE="$APP_DIR/.env"
SYSTEMD_DIR="/etc/systemd/system"

log() { printf '\n[Bybit Demo 安装] %s\n' "$*"; }
fail() { printf '\n[Bybit Demo 安装] 错误：%s\n' "$*" >&2; exit 1; }

[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "请在控制面板终端以 root 执行：bash $APP_DIR/deploy/install_bybit_demo.sh"
[[ -f "$APP_DIR/auto_runner.py" && -f "$APP_DIR/terminal_menu.py" ]] || fail "未找到程序文件，请先从 GitHub 下载完整仓库。"
command -v apt-get >/dev/null 2>&1 || fail "此安装器目前支持 Ubuntu/Debian（apt-get + systemd）。"
command -v systemctl >/dev/null 2>&1 || fail "未检测到 systemd。请在 Ubuntu/Debian VPS 主机上运行，而不是容器内。"

log "安装 Python、Git 和基础依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 python3-venv python3-pip ca-certificates curl

log "创建专用运行账户"
getent group "$APP_GROUP" >/dev/null || groupadd --system "$APP_GROUP"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --gid "$APP_GROUP" --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

log "设置程序目录权限并准备运行目录"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" "$APP_DIR/state" "$APP_DIR/logs"

log "准备虚拟环境并安装依赖"
if [[ ! -x "$VENV/bin/python" ]]; then
  runuser -u "$APP_USER" -- python3 -m venv "$VENV"
fi
runuser -u "$APP_USER" -- "$VENV/bin/python" -m pip install --upgrade pip
runuser -u "$APP_USER" -- "$VENV/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

log "创建或修复安全配置文件"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'ENVEOF'
# Bybit Production/Mainnet Demo Trading only. Never put real-mainnet credentials here.
BYBIT_DEMO_API_KEY=
BYBIT_DEMO_API_SECRET=
BYBIT_API_BASE=https://api-demo.bybit.com
BYBIT_TRADING_MODE=demo
BYBIT_SETTLE_COIN=USDT

# Safe defaults: report-only; no position mutations or active close.
PROTECTION_EXECUTION_ENABLED=false
ACTIVE_CLOSE_EXECUTION_ENABLED=false

POSITION_CACHE_PATH=state/last-successful-positions.json
PROTECTION_STATE_PATH=state/protection-state.json
PROTECTION_FAILURE_LIMIT=3
CACHE_MAX_AGE_SECONDS=3600
HTTP_TIMEOUT_SECONDS=20
REPORT_HTTP_TIMEOUT_SECONDS=120

# Optional reporting integrations. Leave blank if not used.
RISK_MODEL_API_KEY=
RISK_MODEL_API_BASE=https://api.openai.com/v1
RISK_MODEL=gpt-5.6-sol
GROK_API_KEY=
GROK_API_BASE=https://api.x.ai/v1
GROK_MODEL=grok-4
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENVEOF
  printf '\nPYTHON=%s/bin/python\n' "$VENV" >> "$ENV_FILE"
fi
if ! grep -q '^PYTHON=' "$ENV_FILE"; then
  printf '\nPYTHON=%s/bin/python\n' "$VENV" >> "$ENV_FILE"
fi
chown "$APP_USER:$APP_GROUP" "$ENV_FILE"
chmod 600 "$ENV_FILE"

log "安装 systemd 服务（只监听本机，不开放公网端口）"
for unit in bybit-demo-report-http.service bybit-demo-dynamic-protection.service; do
  sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/deploy/$unit" > "$SYSTEMD_DIR/$unit"
  chmod 0644 "$SYSTEMD_DIR/$unit"
done
systemctl daemon-reload

log "运行离线测试"
runuser -u "$APP_USER" -- bash -lc "cd '$APP_DIR' && '$VENV/bin/python' -m pytest -q"

if [[ -t 0 ]]; then
  log "启动安全配置面板：密钥输入不会回显，配置保存在权限 600 的 .env 文件"
  runuser -u "$APP_USER" -- env HOME="$APP_DIR" "$VENV/bin/python" "$APP_DIR/terminal_menu.py" || true
else
  log "当前终端不是交互终端，跳过密钥输入。之后可在控制面板终端运行："
  printf '  sudo -u %s %s %s\n' "$APP_USER" "$VENV/bin/python" "$APP_DIR/terminal_menu.py"
  printf '  配置完成后运行：systemctl restart bybit-demo-report-http.service bybit-demo-dynamic-protection.service\n'
fi

log "启用并启动本机 HTTP 服务"
systemctl enable --now bybit-demo-report-http.service
systemctl enable --now bybit-demo-dynamic-protection.service

log "安装完成。检查服务状态："
systemctl --no-pager --full status bybit-demo-report-http.service || true
systemctl --no-pager --full status bybit-demo-dynamic-protection.service || true
printf '\n健康检查：\n  curl http://127.0.0.1:38635/healthz\n  curl http://127.0.0.1:38636/healthz\n'
printf '\n安全状态：保护执行=false；主动平仓=false。不要在防火墙开放 38635/38636。\n'
