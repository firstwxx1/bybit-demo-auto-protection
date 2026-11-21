#!/usr/bin/env bash
# ==============================================================================
# 统一项目终端部署与运维面板 (兼容 He's Linux Toolbox v3.0 协议)
# 项目: bybit-demo-auto-protection — Bybit V5 模拟盘自动风控与止盈止损系统
# 用法:
#   bash panel.sh           # 打开交互式终端控制面板
#   bash panel.sh status    # 输出一行状态摘要 (供 linux-toolbox 全局巡检)
#   bash panel.sh install   # 直接执行一键安装/部署
#   bash panel.sh start     # 直接启动或进入专属管理面板
#   bash panel.sh update    # 拉取最新代码更新
# ==============================================================================

set -u

PROJECT_NAME="bybit-demo-auto-protection"
PROJECT_TITLE="Bybit V5 模拟盘自动风控与止盈止损系统"
PROJECT_CAT="量化风控"
PROJECT_VIS="public"
SERVICE_NAME="bybit-demo-protection"
COMPOSE_FILE=""
CONFIG_PATH=".env"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

cd "$PROJECT_DIR" || exit 1

pause() {
  echo ""
  read -r -p "$(echo -e "${DIM}按回车键返回面板...${NC}")" _
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo -e "${YELLOW}[INFO] 正在安装 Docker...${NC}"
    curl -fsSL https://get.docker.com | bash
    systemctl enable --now docker 2>/dev/null || true
  fi
}

ensure_node() {
  if ! command -v node >/dev/null 2>&1; then
    echo -e "${YELLOW}[INFO] 正在安装 Node.js 22 LTS...${NC}"
    if command -v apt-get >/dev/null 2>&1; then
      curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs
    elif command -v dnf >/dev/null 2>&1; then
      curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - && dnf install -y nodejs
    fi
  fi
}

ensure_py() {
  if ! command -v python3 >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -y && apt-get install -y python3 python3-pip python3-venv
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y python3 python3-pip
    elif command -v yum >/dev/null 2>&1; then
      yum install -y python3 python3-pip
    fi
  fi
}

ensure_py_venv() {
  ensure_py
  if [ ! -d "${PROJECT_DIR}/.venv" ]; then
    python3 -m venv "${PROJECT_DIR}/.venv" 2>/dev/null || true
  fi
  if [ -f "${PROJECT_DIR}/requirements.txt" ]; then
    if [ -x "${PROJECT_DIR}/.venv/bin/pip" ]; then
      "${PROJECT_DIR}/.venv/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
    else
      pip3 install -r "${PROJECT_DIR}/requirements.txt" --break-system-packages 2>/dev/null || pip3 install -r "${PROJECT_DIR}/requirements.txt"
    fi
  fi
}

run_py() {
  if [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
    "${PROJECT_DIR}/.venv/bin/python" "$@"
  else
    python3 "$@"
  fi
}

get_runtime_status() {
  if [ -n "$COMPOSE_FILE" ] && [ -f "${PROJECT_DIR}/${COMPOSE_FILE}" ] && command -v docker >/dev/null 2>&1; then
    local up
    up="$(docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" ps --status running -q 2>/dev/null | wc -l | tr -d ' ')"
    if [ "${up:-0}" -gt 0 ]; then
      echo "running (docker:${up})"
      return
    fi
  fi
  if [ -n "$SERVICE_NAME" ] && command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
      echo "running (systemd:${SERVICE_NAME})"
      return
    fi
  fi
  if pgrep -f "$PROJECT_NAME" >/dev/null 2>&1; then
    echo "running (process)"
    return
  fi
  echo "installed/idle"
}

git_pull_safe() {
  echo -e "${YELLOW}[INFO] 正在拉取 ${PROJECT_NAME} 最新代码...${NC}"
  local tok="${GITHUB_TOKEN:-}"
  if [ -z "$tok" ] && [ -f "${HOME}/.config/he-toolbox/github_token" ]; then
    tok="$(tr -d ' \r\n' < "${HOME}/.config/he-toolbox/github_token")"
  fi
  if [ "$PROJECT_VIS" = "private" ] && [ -n "$tok" ]; then
    local b64
    b64="$(printf 'x-access-token:%s' "$tok" | base64 | tr -d '\n')"
    git -c "http.extraheader=AUTHORIZATION: basic ${b64}" pull --ff-only || git -c "http.extraheader=AUTHORIZATION: basic ${b64}" pull
  else
    git pull --ff-only || git pull
  fi
}

do_install() {
  echo -e "${CYAN}[INSTALL] 执行 ${PROJECT_NAME} 一键安装/部署...${NC}"
  ensure_py_venv; [ -f deploy/install_bybit_demo.sh ] && bash deploy/install_bybit_demo.sh || echo '[OK] 依赖已装好，请通过 [2] 打开交互菜单'
}

do_start() {
  echo -e "${CYAN}[START] 打开 Bybit 风控终端控制面板 (terminal_menu.py) ...${NC}"
  run_py terminal_menu.py
}

do_extra() {
  echo -e "${CYAN}[ACTION] 查看后台服务与风控日志 ...${NC}"
  systemctl status bybit-demo-protection --no-pager 2>/dev/null | head -n 20; ls -lt *.log logs/*.log 2>/dev/null | head -n 5
}

do_logs() {
  echo -e "${CYAN}=== ${PROJECT_NAME} 状态与日志 ===${NC}"
  echo -e "运行状态: $(get_runtime_status)"
  if [ -n "$COMPOSE_FILE" ] && [ -f "${PROJECT_DIR}/${COMPOSE_FILE}" ] && command -v docker >/dev/null 2>&1; then
    docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" ps
    echo ""
    docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" logs --tail 50
  elif [ -n "$SERVICE_NAME" ] && command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q "${SERVICE_NAME}"; then
    systemctl status "$SERVICE_NAME" --no-pager | head -n 20
    echo ""
    journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  else
    local logf
    logf="$(find "$PROJECT_DIR" -maxdepth 2 -name '*.log' 2>/dev/null | head -n 1)"
    if [ -n "$logf" ]; then
      echo -e "${DIM}--- tail -n 40 ${logf} ---${NC}"
      tail -n 40 "$logf"
    else
      echo -e "${DIM}未发现 systemd/docker/log 文件，以下为目录与 git 状态:${NC}"
      git log -n 3 --oneline 2>/dev/null || true
      ls -la "$PROJECT_DIR" | head -n 15
    fi
  fi
}

do_config() {
  local target="$CONFIG_PATH"
  if [[ "$target" != /* ]]; then
    target="${PROJECT_DIR}/${target}"
  fi
  if [ ! -f "$target" ]; then
    if [ -f "${PROJECT_DIR}/.env.example" ]; then
      cp "${PROJECT_DIR}/.env.example" "$target"
      echo -e "${GREEN}[OK] 已从 .env.example 生成 ${target}${NC}"
    else
      echo -e "${YELLOW}[INFO] 配置文件 ${target} 尚不存在，将新建空文件。${NC}"
      mkdir -p "$(dirname "$target")"
      touch "$target"
    fi
  fi
  local editor="${EDITOR:-nano}"
  command -v "$editor" >/dev/null 2>&1 || editor="vi"
  "$editor" "$target"
}

do_stop_or_remove() {
  echo -e "  1) 停止运行中的服务 / 容器"
  echo -e "  2) ${RED}彻底卸载并删除目录 ${PROJECT_DIR}${NC}"
  echo -e "  0) 取消"
  read -r -p "请选择 [0-2]: " opt
  case "$opt" in
    1)
      [ -n "$COMPOSE_FILE" ] && [ -f "${PROJECT_DIR}/${COMPOSE_FILE}" ] && docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" down 2>/dev/null || true
      [ -n "$SERVICE_NAME" ] && systemctl stop "$SERVICE_NAME" 2>/dev/null || true
      echo -e "${GREEN}[OK] 服务已停止。${NC}"
      ;;
    2)
      read -r -p "确认删除整个目录 ${PROJECT_DIR}？输入 YES 确认: " confirm
      if [ "$confirm" = "YES" ]; then
        [ -n "$COMPOSE_FILE" ] && [ -f "${PROJECT_DIR}/${COMPOSE_FILE}" ] && docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" down 2>/dev/null || true
        [ -n "$SERVICE_NAME" ] && systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
        cd /tmp && rm -rf "$PROJECT_DIR"
        echo -e "${GREEN}[OK] 已彻底卸载 ${PROJECT_NAME}${NC}"
        exit 0
      fi
      ;;
  esac
}

print_status_line() {
  local st commit
  st="$(get_runtime_status)"
  commit="$(git log -1 --format='%h %cd' --date=short 2>/dev/null || echo 'no-git')"
  printf "%-28s | %-10s | %-24s | %s\n" "$PROJECT_NAME" "$PROJECT_CAT" "$st" "$commit"
}

show_menu() {
  while true; do
    clear
    local st commit
    st="$(get_runtime_status)"
    commit="$(git log -1 --format='%h (%cd)' --date=short 2>/dev/null || echo 'N/A')"
    echo -e "${CYAN}┌────────────────────────────────────────────────────────────────────┐${NC}"
    echo -e "${CYAN}│${NC}  ${BOLD}${WHITE}${PROJECT_NAME}${NC}  ${DIM}[${PROJECT_CAT} | ${PROJECT_VIS}]${NC}"
    echo -e "${CYAN}│${NC}  ${DIM}${PROJECT_TITLE}${NC}"
    echo -e "${CYAN}├────────────────────────────────────────────────────────────────────┤${NC}"
    echo -e "${CYAN}│${NC}  路径: ${PROJECT_DIR}"
    echo -e "${CYAN}│${NC}  状态: ${GREEN}${st}${NC}   版本: ${CYAN}${commit}${NC}"
    echo -e "${CYAN}└────────────────────────────────────────────────────────────────────┘${NC}"
    echo ""
    echo -e "  ${CYAN}1)${NC} 一键安装 / 初始化部署"
    echo -e "  ${CYAN}2)${NC} 打开 Bybit 风控终端控制面板 (terminal_menu.py)"
    echo -e "  ${CYAN}3)${NC} 查看后台服务与风控日志"
    echo -e "  ${CYAN}4)${NC} 编辑配置 / 环境变量 (${DIM}${CONFIG_PATH}${NC})"
    echo -e "  ${CYAN}5)${NC} 查看实时状态与日志"
    echo -e "  ${CYAN}6)${NC} 拉取最新代码更新 (git pull)"
    echo -e "  ${CYAN}7)${NC} 查看项目 README 文档摘要"
    echo -e "  ${CYAN}8)${NC} 停止服务 / 卸载项目"
    echo -e "${CYAN}────────────────────────────────────────────────────────────────────${NC}"
    echo -e "  ${DIM}0) 退出 / 返回主工具箱${NC}"
    echo ""
    read -r -p "请输入选项 [0-8]: " ch
    case "$ch" in
      1) do_install; pause ;;
      2) do_start; pause ;;
      3) do_extra; pause ;;
      4) do_config ;;
      5) do_logs; pause ;;
      6) git_pull_safe; pause ;;
      7) [ -f README.md ] && head -n 60 README.md || echo "无 README.md"; pause ;;
      8) do_stop_or_remove; pause ;;
      0) return 0 ;;
      *) echo -e "${RED}无效选项${NC}"; sleep 1 ;;
    esac
  done
}

case "${1:-}" in
  status)  print_status_line ;;
  install) do_install ;;
  start)   do_start ;;
  update)  git_pull_safe ;;
  logs)    do_logs ;;
  *)       show_menu ;;
esac
