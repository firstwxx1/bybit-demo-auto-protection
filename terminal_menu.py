"""BYBIT GPT 自动止盈止损 — 中文终端面板。

覆盖：BYBIT API、模型 API（GPT + Grok）、Telegram、保护参数、
实时查询、自动周期、仅报告模式、Telegram 连接测试、保护状态查看、
自动推送任务管理（cron 安装 / 关闭 / 改间隔）。

自动推送任务：
  面板直接把 /etc/cron.d/bybit-demo-auto 写出来，并强制 cron 重载
  （touch 到下一秒），避免出现「改了文件但 cron 还用旧表」的问题。
  需要 root 权限（从服务器 root 终端运行本面板）。
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"

# --- 自动推送任务（cron）---
CRON_FILE = Path("/etc/cron.d/bybit-demo-auto")
CRON_JOB_USER = "bybit-n8n"
CRON_LOG = "logs/auto-runner.log"
DEFAULT_INTERVAL_MINUTES = 30

# --- BYBIT 模拟盘密钥 ---
BYBIT_CREDENTIAL_KEYS = (
    "BYBIT_DEMO_API_KEY",
    "BYBIT_DEMO_API_SECRET",
)

# --- 模型密钥 ---
MODEL_CREDENTIAL_KEYS = (
    "RISK_MODEL_API_KEY",
    "GROK_API_KEY",
)

# --- Telegram 密钥 ---
TELEGRAM_CREDENTIAL_KEYS = (
    "TELEGRAM_BOT_TOKEN",
)

# --- 所有密钥（不回显） ---
ALL_SECRET_KEYS = BYBIT_CREDENTIAL_KEYS + MODEL_CREDENTIAL_KEYS + TELEGRAM_CREDENTIAL_KEYS

# --- BYBIT 实时查询所需 ---
REQUIRED_BYBIT_KEYS = BYBIT_CREDENTIAL_KEYS

# --- Telegram 推送所需 ---
REQUIRED_TG_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)

# --- 可选配置默认值 ---
OPTIONAL_DEFAULTS: dict[str, str] = {
    "BYBIT_API_BASE": "https://api-demo.bybit.com",
    "GROK_API_BASE": "https://api.x.ai/v1",
    "GROK_MODEL": "grok-4",
    "RISK_MODEL_API_BASE": "",
    "RISK_MODEL": "gpt-5.6-sol",
    "TELEGRAM_CHAT_ID": "",
    "POSITION_CACHE_PATH": "state/last-successful-positions.json",
    "FIXED_STOP_ENTRY_PCT": "0.04855847842644323",
    "FIXED_STOP_MAX_MARK_DISTANCE_PCT": "0.015",
    "FIXED_TAKE_PROFIT_MARK_PCT": "",
    "CACHE_MAX_AGE_SECONDS": "3600",
    "HTTP_TIMEOUT_SECONDS": "20",
    "BYBIT_TRADING_MODE": "demo",
    "PROTECTION_EXECUTION_ENABLED": "false",
    "ACTIVE_CLOSE_EXECUTION_ENABLED": "false",
    "PROTECTION_STATE_PATH": "state/protection-state.json",
    "PROTECTION_FAILURE_LIMIT": "3",
}


# ============================================================
# 环境工具
# ============================================================

def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def save_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 由终端面板自动生成 - 请保持权限 0600"]
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"值不能包含换行: {key}")
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _env_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _masked(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "****" + value[-4:]


# ============================================================
# 自动推送任务（cron）工具
# ============================================================

def _cron_schedule(interval_minutes: int) -> str:
    """把「每 N 分钟」翻译成合法 cron 时间字段。

    cron 的 */N 只有在 N 整除 60 时才成立（*/90 表示「第 0 分钟」，实际永不触发）。
    规则：
      - N 整除 60（1/2/3/4/5/6/10/12/15/20/30/60）→ */N
      - N 是 60 的整数倍且 >60（120/180…）→ 0 */(N/60)
      - 其余（如 90）→ 退化为整点倍率不成立，用最近可表达的间隔并提示
    """
    interval = max(1, int(interval_minutes))
    if interval < 60:
        if 60 % interval == 0:
            return f"*/{interval} * * * *"
        # 不能整除 60：用「每 60/N 分钟」的合法近似
        valid = [m for m in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30) if m <= interval]
        nearest = max(valid) if valid else 1
        return f"*/{nearest} * * * *"
    if interval == 60:
        return "0 * * * *"
    if interval % 60 == 0:
        return f"0 */{interval // 60} * * *"
    # 大于 60 且非整小时倍率（如 90）：退化为最近的整点倍率
    hours = max(1, round(interval / 60))
    return f"0 */{hours} * * *"


def cron_job_line(interval_minutes: int) -> str:
    """生成 cron 作业行：先 source .env 注入密钥/开关，再跑 auto_runner.py。"""
    interval = max(1, int(interval_minutes))
    schedule = _cron_schedule(interval)
    return (
        f"{schedule} {CRON_JOB_USER} "
        f"set -a; . {ROOT}/.env; set +a; "
        f"cd {ROOT} && ./.venv/bin/python auto_runner.py >> {CRON_LOG} 2>&1"
    )


def render_cron_file(interval_minutes: int) -> str:
    interval = max(1, int(interval_minutes))
    return (
        f"# Bybit Demo 自动风控 — 每 {interval} 分钟运行一次"
        f"，生成报告 + 维护 TP/SL + Telegram 推送\n"
        f"# 先 source .env 把密钥/开关注入环境，再运行 auto_runner.py\n"
        f"SHELL=/bin/bash\n"
        f"PATH=/usr/local/bin:/usr/bin:/bin\n"
        f"{cron_job_line(interval)}\n"
    )


def cron_interval_minutes() -> int | None:
    """读取当前 cron 文件里的间隔；未安装或解析失败返回 None。"""
    if not CRON_FILE.exists():
        return None
    try:
        text = CRON_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        parts = line.split()
        if len(parts) < 5 or "auto_runner.py" not in line:
            continue
        minute_field = parts[0]
        hour_field = parts[1]
        if minute_field.startswith("*/"):
            try:
                return int(minute_field[2:])
            except ValueError:
                return None
        if minute_field == "0" and hour_field.startswith("*/"):
            try:
                return int(hour_field[2:]) * 60
            except ValueError:
                return None
        if minute_field == "0" and hour_field == "*":
            return 60  # 每小时整点
        if minute_field.isdigit() and hour_field == "*":
            return 60
        return None
    return None


def cron_installed() -> bool:
    return cron_interval_minutes() is not None


def _force_cron_reload() -> None:
    """把 cron 文件 mtime 推到「当前秒 + 1」，强制 cron 重新读盘。

    cron 只在文件 mtime 比上次记录更新（且跨过整秒边界）时才重载。
    面板内用 sed / 覆盖写文件常在 1 秒内完成，mtime 不变 → cron 继续用旧表，
    这正是之前「改成 */30 却还在每分钟跑」的根因。这里统一把 mtime 推后一格。
    """
    now = time.time()
    stamp = int(now) + 1
    try:
        os.utime(CRON_FILE, (stamp, stamp))
    except OSError:
        subprocess.run(["touch", "-d", f"@{stamp}", str(CRON_FILE)], check=False)
    try:
        subprocess.run(["systemctl", "restart", "cron"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass
    time.sleep(0.5)


def cron_last_run() -> str:
    """从 journal 里找最近一次自动运行的时间；读不到就退回日志文件的运行时间。"""
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "cron", "--no-pager", "-n", "200"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        proc = None
    if proc is not None:
        for line in reversed(proc.stdout.splitlines()):
            if "auto_runner.py" in line and "CMD" in line:
                return "cron: " + line.strip()[:110]

    # 非 root 用户读不到 cron journal，退回日志文件里的最近运行时间。
    fallback = _log_last_run_timestamp()
    if fallback:
        return fallback
    return "（暂无自动运行记录）"


def _log_last_run_timestamp() -> str | None:
    """从 logs/auto-runner.log 里取最后一次运行的时间戳行。"""
    log_file = LOG_DIR / CRON_LOG.split("/")[-1]
    if not log_file.exists():
        return None
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("时间：") or stripped.startswith("时间:"):
            last = "log: " + stripped
    try:
        mtime = datetime.fromtimestamp(log_file.stat().st_mtime, timezone.utc)
    except OSError:
        mtime = None
    if mtime is not None:
        return f"{last}  （文件最后写入 {mtime.strftime('%Y-%m-%d %H:%M:%S UTC')}）"
    return last


def _cron_status_text() -> str:
    if platform.system() != "Linux":
        return "🔴 未启用（仅 Linux/cron 支持）"
    interval = cron_interval_minutes()
    if interval is None:
        return "🔴 未启用"
    return f"🟢 已启用（每 {interval} 分钟）"


# ============================================================
# 状态显示
# ============================================================

def _bybit_status(values: dict[str, str]) -> str:
    missing = [k for k in REQUIRED_BYBIT_KEYS if not values.get(k)]
    if not missing:
        return "✅ 已配置"
    return f"❌ 缺 {len(missing)} 项"


def _grok_status(values: dict[str, str]) -> str:
    key = values.get("GROK_API_KEY", "")
    if key:
        return f"✅ {values.get('GROK_MODEL', 'grok-4')}"
    return "❌ 未配置"


def _gpt_status(values: dict[str, str]) -> str:
    key = values.get("RISK_MODEL_API_KEY", "")
    base = values.get("RISK_MODEL_API_BASE", "")
    model = values.get("RISK_MODEL", "gpt-5.6-sol")
    if key and base:
        return f"✅ {model}"
    if key and not base:
        return "❌ 缺 Base URL"
    return "❌ 未配置"


def _telegram_status(values: dict[str, str]) -> str:
    missing = [k for k in REQUIRED_TG_KEYS if not values.get(k)]
    if not missing:
        return "✅ 已配置"
    return f"❌ 缺 {len(missing)} 项"


def _protection_status(values: dict[str, str]) -> str:
    enabled = _env_enabled(values.get("PROTECTION_EXECUTION_ENABLED"))
    return "🟢 开启" if enabled else "🔴 关闭"


def _circuit_breaker_status(values: dict[str, str]) -> str:
    state_rel = values.get("PROTECTION_STATE_PATH", "state/protection-state.json")
    state_path = ROOT / state_rel
    if not state_path.exists():
        return "✅ 正常（无状态文件）"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        # Bybit protection state uses ``failures``.  Keep the legacy field as
        # a read-only fallback so an existing state file still renders safely.
        failures = data.get("failures", data.get("consecutive_failures", 0))
        if data.get("circuit_open"):
            return f"⚠️ 熔断（连续失败 {failures} 次）"
        return f"✅ 正常（失败 {failures} 次）"
    except (json.JSONDecodeError, OSError):
        return "❌ 读取失败"


def _protection_orders_status(values: dict[str, str]) -> str:
    state_rel = values.get("PROTECTION_STATE_PATH", "state/protection-state.json")
    state_path = ROOT / state_rel
    if not state_path.exists():
        return "  无托管单"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        # Current Bybit state tracks managed protection prices rather than
        # exchange-specific algo order IDs.  Read the old ``orders`` shape as
        # a compatibility fallback for state files created before migration.
        managed = data.get("managed", data.get("orders", {}))
        if not isinstance(managed, dict) or not managed:
            return "  无托管单"
        lines = []
        for inst, info in managed.items():
            if not isinstance(info, dict):
                continue
            stop = info.get("stop_loss", info.get("stopLoss", "?"))
            tp = info.get("take_profit", info.get("takeProfit", "-"))
            if tp in (None, "", 0, "0"):
                tp = "-"
            suffix = f" (algoId={info['algoId']})" if info.get("algoId") else ""
            lines.append(f"  {inst}: 止损={stop} 止盈={tp}{suffix}")
        if not lines:
            return "  无托管单"
        return "\n".join(lines)
    except (json.JSONDecodeError, OSError):
        return "  ❌ 读取失败"


# Compatibility/config helpers used by unattended setup and tests.
CREDENTIAL_KEYS = (
    "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET",
    "RISK_MODEL_API_KEY", "GROK_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)

def config_status(values: dict[str, str]) -> dict[str, Any]:
    required = ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET")
    missing = [key for key in required if not values.get(key)]
    return {"complete": not missing, "missing": missing}

def dependency_status() -> dict[str, Any]:
    result: dict[str, Any] = {"python": bool(sys.executable), "pytest": False}
    try:
        import pytest  # type: ignore
        result["pytest"] = getattr(pytest, "__version__", True)
    except Exception:
        result["pytest"] = False
    return result

def _update_flag(path: Path, key: str, enabled: bool) -> None:
    values = load_env(path)
    values[key] = "true" if enabled else "false"
    save_env(path, values)

def set_protection_enabled(path: Path, enabled: bool) -> None:
    _update_flag(path, "PROTECTION_EXECUTION_ENABLED", enabled)

def set_active_close_enabled(path: Path, enabled: bool) -> None:
    _update_flag(path, "ACTIVE_CLOSE_EXECUTION_ENABLED", enabled)

# ============================================================
# 输入工具
# ============================================================

def _set_protection(path: Path, input_fn: Callable[[str], str] = input) -> None:
    values = load_env(path)
    if not _env_enabled(values.get("PROTECTION_EXECUTION_ENABLED")):
        if input_fn("输入 ENABLE 才能开启保护执行：").strip() != "ENABLE":
            return
        values["PROTECTION_EXECUTION_ENABLED"] = "true"
    else:
        values["PROTECTION_EXECUTION_ENABLED"] = "false"
    save_env(path, values)


def _prompt_secret(prompt_text: str, current: str) -> str:
    label = f"已配置 ({_masked(current)})" if current else "空"
    value = getpass.getpass(f"  {prompt_text} [{label}] → ")
    return value


def _prompt_text(prompt_text: str, current: str) -> str:
    value = input(f"  {prompt_text} [{current}]: ").strip()
    return value


# ============================================================
# 菜单渲染
# ============================================================

def render_menu(values: dict[str, str] | None = None) -> str:
    values = dict(values or load_env())
    bybit = _bybit_status(values)
    gpt = _gpt_status(values)
    grok = _grok_status(values)
    tg = _telegram_status(values)
    prot = _protection_status(values)
    cb = _circuit_breaker_status(values)
    cron = _cron_status_text()

    lines = [
        "",
        "╔══════════════════════════════════════════════════════════╗",
        "║     BYBIT 模拟盘自动止盈止损 — GPT 风控 + Telegram 推送      ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  BYBIT: {bybit}  GPT: {gpt}  Grok: {grok}",
        f"║  TG: {tg}  保护: {prot}  熔断: {cb}",
        f"║  自动推送: {cron}",
        "╠══════════════════════════════════════════════════════════╣",
        "║  【配置】                                                ",
        "║  1. BYBIT API     （主网 Demo Trading key / secret）     ",
        "║  2. 模型 API    （GPT key+地址+模型, Grok key+地址+模型） ",
        "║  3. Telegram    （机器人 token / 聊天 ID）               ",
        "║  4. 保护参数    （止损 / 止盈 / 缓存 / 熔断）            ",
        "║  5. 查看完整配置                                        ",
        "║                                                          ",
        "║  【自动推送】                                            ",
        "║  T. 开启自动推送任务（按间隔定时跑 + TG 推送）           ",
        "║  U. 关闭自动推送任务                                     ",
        "║  V. 修改推送间隔（分钟）                                 ",
        "║                                                          ",
        "║  【运行】                                                ",
        "║  6. 运行完整周期（报告 + 止盈止损 + TG 推送）             ",
        "║  7. 仅运行报告（不挂单、不推送）                         ",
        "║  8. 查询 BYBIT 实际持仓（只读 GET）                        ",
        "║                                                          ",
        "║  【开关】                                                ",
        "║  A. 切换保护执行开关（默认关闭）                         ",
        "║     主动平仓开关固定关闭，需人工改环境文件才能启用       ",
        "║                                                          ",
        "║  【诊断】                                                ",
        "║  B. 测试 Telegram 连接                                   ",
        "║  C. 查看保护状态文件（托管单 + 熔断）                    ",
        "║  D. 查看最近 50 行日志                                   ",
        "║  G. 查看自动推送任务状态（cron + 最近运行）              ",
        "║                                                          ",
        "║  0. 退出                                                 ",
        "╠══════════════════════════════════════════════════════════╣",
        "║  安全边界：仅维护已有持仓 reduceOnly 条件单               ",
        "║  禁止开仓、加仓、转账、提现、调杠杆                       ",
        "╚══════════════════════════════════════════════════════════╝",
    ]
    return "\n".join(lines)


# ============================================================
# 配置操作
# ============================================================

def _action_configure_bybit(path: Path) -> None:
    values = load_env(path)
    print()
    print("══ BYBIT 模拟盘 API ══")
    print("输入空值保留当前配置；密钥不回显。")
    print()
    for key in BYBIT_CREDENTIAL_KEYS:
        current = values.get(key, "")
        value = _prompt_secret(key, current)
        if value:
            values[key] = value
    print()
    # Base URL
    base_key = "BYBIT_API_BASE"
    default = OPTIONAL_DEFAULTS.get(base_key, "https://api-demo.bybit.com")
    current = values.get(base_key, default)
    value = _prompt_text("BYBIT_API_BASE（必须是 Demo Trading 地址）", current)
    values[base_key] = value or current
    save_env(path, values)
    print(f"\n  ✅ BYBIT API 配置已保存 → {path}")


def _action_configure_model(path: Path) -> None:
    values = load_env(path)
    print()
    print("══ 模型 API 配置 ══")
    print()

    # --- GPT ---
    print("── GPT 风险模型 ──")
    print("  用途：风险分析、止损和止盈建议")
    print()
    current_key = values.get("RISK_MODEL_API_KEY", "")
    value = _prompt_secret("RISK_MODEL_API_KEY（GPT 密钥）", current_key)
    if value:
        values["RISK_MODEL_API_KEY"] = value

    current_base = values.get("RISK_MODEL_API_BASE", "")
    default_base = OPTIONAL_DEFAULTS.get("RISK_MODEL_API_BASE", "")
    value = _prompt_text("RISK_MODEL_API_BASE（接口地址）", current_base or default_base)
    values["RISK_MODEL_API_BASE"] = value or current_base or default_base

    current_model = values.get("RISK_MODEL", "")
    default_model = OPTIONAL_DEFAULTS.get("RISK_MODEL", "gpt-5.6-sol")
    value = _prompt_text("RISK_MODEL（模型名称）", current_model or default_model)
    values["RISK_MODEL"] = value or current_model or default_model

    print()

    # --- Grok ---
    print("── Grok 新闻模型 ──")
    print("  用途：搜索 X/Twitter 新闻作为风险参考")
    print()
    current_key = values.get("GROK_API_KEY", "")
    value = _prompt_secret("GROK_API_KEY（Grok 密钥）", current_key)
    if value:
        values["GROK_API_KEY"] = value

    current_base = values.get("GROK_API_BASE", "")
    default_base = OPTIONAL_DEFAULTS.get("GROK_API_BASE", "https://api.x.ai/v1")
    value = _prompt_text("GROK_API_BASE（接口地址）", current_base or default_base)
    values["GROK_API_BASE"] = value or current_base or default_base

    current_model = values.get("GROK_MODEL", "")
    default_model = OPTIONAL_DEFAULTS.get("GROK_MODEL", "grok-4")
    value = _prompt_text("GROK_MODEL（模型名称）", current_model or default_model)
    values["GROK_MODEL"] = value or current_model or default_model

    print()
    # --- HTTP 超时 ---
    current_timeout = values.get("HTTP_TIMEOUT_SECONDS", "20")
    value = _prompt_text("HTTP_TIMEOUT_SECONDS（请求超时秒数）", current_timeout)
    values["HTTP_TIMEOUT_SECONDS"] = value or current_timeout

    save_env(path, values)
    print(f"\n  ✅ 模型 API 配置已保存 → {path}")


def _action_configure_telegram(path: Path) -> None:
    values = load_env(path)
    print()
    print("══ Telegram 推送配置 ══")
    print()
    current_token = values.get("TELEGRAM_BOT_TOKEN", "")
    value = _prompt_secret("TELEGRAM_BOT_TOKEN（机器人令牌）", current_token)
    if value:
        values["TELEGRAM_BOT_TOKEN"] = value

    current_chat = values.get("TELEGRAM_CHAT_ID", "")
    value = _prompt_text("TELEGRAM_CHAT_ID（聊天 ID）", current_chat)
    values["TELEGRAM_CHAT_ID"] = value or current_chat

    save_env(path, values)
    print(f"\n  ✅ Telegram 配置已保存 → {path}")


def _action_configure_protection(path: Path) -> None:
    values = load_env(path)
    print()
    print("══ 保护参数配置 ══")
    print("  以下参数控制止损 / 止盈的计算和执行。")
    print()

    params = [
        ("FIXED_STOP_ENTRY_PCT", "固定止损比例（相对开仓价，GPT不可用时的兜底）", "0.04855847842644323"),
        ("FIXED_STOP_MAX_MARK_DISTANCE_PCT", "止损距标记价最大百分比", "0.015"),
        ("FIXED_TAKE_PROFIT_MARK_PCT", "固定止盈百分比（留空=仅用GPT建议）", ""),
        ("CACHE_MAX_AGE_SECONDS", "持仓缓存最大有效期（秒）", "3600"),
        ("PROTECTION_FAILURE_LIMIT", "熔断连续失败次数上限", "3"),
    ]

    for key, desc, default in params:
        current = values.get(key, default)
        print(f"  {desc}")
        value = _prompt_text(key, current)
        values[key] = value or current
        print()

    save_env(path, values)
    print(f"  ✅ 保护参数已保存 → {path}")


# ============================================================
# 查看完整配置
# ============================================================

def _action_full_status(path: Path) -> None:
    values = load_env(path)
    print()
    print("══ BYBIT 模拟盘 ══")
    for key in BYBIT_CREDENTIAL_KEYS:
        val = values.get(key, "")
        print(f"  {key}: {_masked(val) if val else '（未配置）'}")
    print(f"  BYBIT_API_BASE: {values.get('BYBIT_API_BASE', 'https://api-demo.bybit.com')}")
    print()
    print("══ GPT 风险模型 ══")
    gpt_key = values.get("RISK_MODEL_API_KEY", "")
    print(f"  RISK_MODEL_API_KEY: {_masked(gpt_key) if gpt_key else '（未配置）'}")
    print(f"  RISK_MODEL_API_BASE: {values.get('RISK_MODEL_API_BASE', '') or '（未配置）'}")
    print(f"  RISK_MODEL: {values.get('RISK_MODEL', 'gpt-5.6-sol')}")
    print()
    print("══ Grok 新闻模型 ══")
    grok_key = values.get("GROK_API_KEY", "")
    print(f"  GROK_API_KEY: {_masked(grok_key) if grok_key else '（未配置）'}")
    print(f"  GROK_API_BASE: {values.get('GROK_API_BASE', 'https://api.x.ai/v1')}")
    print(f"  GROK_MODEL: {values.get('GROK_MODEL', 'grok-4')}")
    print()
    print("══ Telegram ══")
    tg_token = values.get("TELEGRAM_BOT_TOKEN", "")
    print(f"  TELEGRAM_BOT_TOKEN: {_masked(tg_token) if tg_token else '（未配置）'}")
    print(f"  TELEGRAM_CHAT_ID: {values.get('TELEGRAM_CHAT_ID', '') or '（未配置）'}")
    print()
    print("══ 保护执行 ══")
    print(f"  PROTECTION_EXECUTION_ENABLED: {values.get('PROTECTION_EXECUTION_ENABLED', 'false')}")
    print(f"  FIXED_STOP_ENTRY_PCT: {values.get('FIXED_STOP_ENTRY_PCT', '0.04855847842644323')}")
    print(f"  FIXED_STOP_MAX_MARK_DISTANCE_PCT: {values.get('FIXED_STOP_MAX_MARK_DISTANCE_PCT', '0.015')}")
    print(f"  FIXED_TAKE_PROFIT_MARK_PCT: {values.get('FIXED_TAKE_PROFIT_MARK_PCT', '') or '（仅用GPT）'}")
    print(f"  PROTECTION_FAILURE_LIMIT: {values.get('PROTECTION_FAILURE_LIMIT', '3')}")
    print(f"  熔断状态: {_circuit_breaker_status(values)}")
    print()
    print("══ 托管单 ══")
    orders = _protection_orders_status(values)
    print(orders)
    print()
    print("══ 自动推送任务 ══")
    print(f"  状态: {_cron_status_text()}")
    print(f"  配置文件: {CRON_FILE}")
    print()
    print("══ 环境信息 ══")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  配置文件: {path}")
    if path.exists():
        print(f"  文件权限: {path.stat().st_mode & 0o777:o}")
    else:
        print("  文件权限: （文件不存在）")


# ============================================================
# 运行操作
# ============================================================

def _action_run_auto_cycle(path: Path) -> None:
    values = load_env(path)
    if not values.get("BYBIT_DEMO_API_KEY"):
        print("\n  ❌ 请先配置 BYBIT API（菜单 1）")
        return
    print()
    print("  ▶ 正在运行完整周期（报告 + 止盈止损 + Telegram 推送）...")
    print("  预计需要 20-60 秒，请稍候。")
    print()
    env = os.environ.copy()
    env.update(values)
    subprocess.run(
        [sys.executable, str(ROOT / "auto_runner.py")],
        cwd=ROOT, env=env,
    )


def _action_run_report_only(path: Path) -> None:
    values = load_env(path)
    if not values.get("BYBIT_DEMO_API_KEY"):
        print("\n  ❌ 请先配置 BYBIT API（菜单 1）")
        return
    print("\n  ▶ 正在运行仅报告模式（不挂单、不推送）...\n")
    env = os.environ.copy()
    env.update(values)
    subprocess.run(
        [sys.executable, str(ROOT / "auto_runner.py"), "--no-protection", "--no-telegram"],
        cwd=ROOT, env=env,
    )


def _action_query_positions(path: Path) -> None:
    values = load_env(path)
    if not values.get("BYBIT_DEMO_API_KEY"):
        print("\n  ❌ 请先配置 BYBIT API（菜单 1）")
        return
    print("\n  ▶ 正在查询 BYBIT 模拟盘持仓（只读 GET）...\n")
    env = os.environ.copy()
    env.update(values)
    subprocess.run(
        [sys.executable, str(ROOT / "live_reporter.py")],
        cwd=ROOT, env=env,
    )


# ============================================================
# 开关操作
# ============================================================

def _action_toggle_protection(path: Path) -> None:
    values = load_env(path)
    current = _env_enabled(values.get("PROTECTION_EXECUTION_ENABLED"))
    if not current:
        print()
        print("  当前状态: 🔴 关闭")
        print("  开启后将自动在 BYBIT 模拟盘挂 reduceOnly 条件止盈止损单。")
        print()
        answer = input("  请输入 ENABLE 确认开启: ").strip()
        if answer != "ENABLE":
            print("  未确认，保护执行保持关闭。")
            return
        values["PROTECTION_EXECUTION_ENABLED"] = "true"
        save_env(path, values)
        print("  ✅ 保护执行已开启: 🟢")
    else:
        print()
        print("  当前状态: 🟢 开启")
        values["PROTECTION_EXECUTION_ENABLED"] = "false"
        save_env(path, values)
        print("  ✅ 保护执行已关闭: 🔴")


# ============================================================
# 自动推送任务操作
# ============================================================

def _read_interval_from_user(default: int = DEFAULT_INTERVAL_MINUTES) -> int | None:
    print("  常用间隔：1 / 5 / 10 / 15 / 20 / 30 / 60 / 120 分钟")
    while True:
        raw = input(f"  推送间隔（分钟）[{default}]: ").strip()
        if not raw:
            return default
        if not raw.isdigit():
            print("  ❌ 请输入正整数。")
            continue
        value = int(raw)
        if value < 1:
            print("  ❌ 至少 1 分钟。")
            continue
        if value > 1440:
            print("  ❌ 上限 1440 分钟（24 小时）。")
            continue
        return value


def _write_cron(interval: int) -> bool:
    """写 cron 文件并强制重载。返回是否成功。"""
    if platform.system() != "Linux":
        print("  ❌ 自动推送任务仅支持 Linux（cron）。Windows 请用任务计划程序。")
        return False
    if os.geteuid() != 0:
        print("  ❌ 需要 root 权限写 /etc/cron.d/。请从服务器 root 终端运行本面板，")
        print("     或用: sudo python3 terminal_menu.py")
        return False
    try:
        CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
        CRON_FILE.write_text(render_cron_file(interval), encoding="utf-8")
        os.chmod(CRON_FILE, 0o644)
    except OSError as exc:
        print(f"  ❌ 写入 {CRON_FILE} 失败: {exc}")
        return False
    _force_cron_reload()
    return True


def _action_enable_auto_push(path: Path) -> None:
    values = load_env(path)
    print()
    print("══ 开启自动推送任务 ══")
    if platform.system() != "Linux":
        print("  仅支持 Linux（cron）。")
        return
    if not values.get("BYBIT_DEMO_API_KEY"):
        print("  ❌ 请先配置 BYBIT API（菜单 1）")
        return
    if not (values.get("TELEGRAM_BOT_TOKEN") and values.get("TELEGRAM_CHAT_ID")):
        print("  ⚠️ Telegram 未完整配置，任务会跑但推送会失败。可先用菜单 3 配好。")
        print()

    existing = cron_interval_minutes()
    if existing is not None:
        print(f"  当前已启用：每 {existing} 分钟。")
        answer = input("  重新设置间隔？(y/N): ").strip().lower()
        interval = _read_interval_from_user(existing) if answer == "y" else existing
    else:
        interval = _read_interval_from_user(DEFAULT_INTERVAL_MINUTES) or DEFAULT_INTERVAL_MINUTES

    print()
    print(f"  ▶ 正在写入 {CRON_FILE}（每 {interval} 分钟）...")
    if not _write_cron(interval):
        return
    effective = cron_interval_minutes()
    print("  ✅ 自动推送任务已开启。")
    print(f"     计划: {cron_job_line(interval).split(' ', 5)[0]}")
    if effective is not None and effective != interval:
        print(f"     ⚠️ cron 无法精确表达 {interval} 分钟，已按每 {effective} 分钟执行。")
    print(f"     日志: {ROOT / CRON_LOG}")
    print()
    print("  提示：cron 采用 UTC 计时；如需立即验证，可用菜单 6 手动跑一次。")


def _action_disable_auto_push() -> None:
    print()
    print("══ 关闭自动推送任务 ══")
    if platform.system() != "Linux":
        print("  仅支持 Linux（cron）。")
        return
    if not CRON_FILE.exists():
        print("  当前未启用，无需关闭。")
        return
    if os.geteuid() != 0:
        print("  ❌ 需要 root 权限删除 /etc/cron.d/bybit-demo-auto。")
        return
    answer = input("  确认关闭自动推送任务？(y/N): ").strip().lower()
    if answer != "y":
        print("  已取消。")
        return
    try:
        if CRON_FILE.exists():
            CRON_FILE.unlink()
    except OSError as exc:
        print(f"  ❌ 删除失败: {exc}")
        return
    _force_cron_reload()
    if CRON_FILE.exists():
        print("  ⚠️ 文件仍存在（可能权限异常），请手动检查。")
        return
    print("  ✅ 自动推送任务已关闭。")


def _action_change_interval() -> None:
    print()
    print("══ 修改推送间隔 ══")
    if platform.system() != "Linux":
        print("  仅支持 Linux（cron）。")
        return
    current = cron_interval_minutes()
    if current is None:
        print("  当前未启用。请先用菜单 T 开启。")
        return
    if os.geteuid() != 0:
        print("  ❌ 需要 root 权限修改 /etc/cron.d/bybit-demo-auto。")
        return
    interval = _read_interval_from_user(current)
    if interval is None:
        return
    if not _write_cron(interval):
        return
    effective = cron_interval_minutes()
    print(f"  ✅ 推送间隔已改为每 {interval} 分钟。")
    if effective is not None and effective != interval:
        print(f"     ⚠️ cron 无法精确表达 {interval} 分钟，实际按每 {effective} 分钟执行。")


def _action_auto_push_status() -> None:
    print()
    print("══ 自动推送任务状态 ══")
    print(f"  状态: {_cron_status_text()}")
    print(f"  配置文件: {CRON_FILE}  {'（存在）' if CRON_FILE.exists() else '（不存在）'}")
    interval = cron_interval_minutes()
    if interval is not None:
        print(f"  计划行: {cron_job_line(interval).split(' ', 5)[0]}")
        print(f"  执行用户: {CRON_JOB_USER}")
    print()
    print("══ 最近一次自动运行 ══")
    print(f"  {cron_last_run()}")
    print()
    print("  说明：")
    print("  - cron 用 UTC 计时，与服务器 date 一致。")
    print("  - 若刚改过间隔请等下一次整点触发验证（menu D 看日志）。")


# ============================================================
# 诊断操作
# ============================================================

def _action_test_telegram(path: Path) -> None:
    values = load_env(path)
    bot_token = values.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = values.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("\n  ❌ 请先配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID（菜单 3）")
        return
    print("\n  ▶ 正在发送测试消息到 Telegram...")
    env = os.environ.copy()
    env.update(values)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    test_script = (
        "from telegram_notifier import send_telegram; "
        "r = send_telegram("
        "'<b>【BYBIT模拟盘自动止盈止损】</b>\\n"
        "✅ Telegram 连接测试成功\\n"
        f"🕐 {now_str}')"
        "; print('✅ 发送成功' if r.get('ok') else '❌ 发送失败')"
    )
    subprocess.run(
        [sys.executable, "-c", test_script],
        cwd=ROOT, env=env,
    )


def _action_view_state(path: Path) -> None:
    values = load_env(path)
    print()
    print("══ 熔断状态 ══")
    print(f"  {_circuit_breaker_status(values)}")
    print()
    print("══ 托管单 ══")
    print(f"{_protection_orders_status(values)}")
    print()
    state_rel = values.get("PROTECTION_STATE_PATH", "state/protection-state.json")
    state_path = ROOT / state_rel
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            print("══ 原始状态文件 ══")
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ❌ 状态文件读取失败: {exc}")


def _action_view_logs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "auto-runner.log"
    if not log_file.exists():
        print("\n  暂无日志文件（auto_runner.py 尚未运行过）。")
        return
    print(f"\n══ 最近 50 行日志 ({log_file.name}) ══")
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-50:]:
            print(f"  {line}")
    except OSError as exc:
        print(f"  ❌ 日志读取失败: {exc}")


# ============================================================
# 主循环
# ============================================================

def _action_install_services(path: Path) -> None:
    """Run the supported Ubuntu/Debian one-click installer as root."""
    installer = ROOT / "deploy" / "install_bybit_demo.sh"
    if not installer.is_file():
        print("  未找到部署安装器，请先更新完整仓库。")
        return
    if platform.system() != "Linux":
        print("  一键服务器部署仅支持 Ubuntu/Debian VPS；Windows 请使用本地运行模式。")
        return
    if os.geteuid() != 0:
        print("  请从服务器控制面板的 root 终端运行安装器：")
        print(f"  sudo bash {installer}")
        return
    subprocess.run(["bash", str(installer)], cwd=ROOT, check=False)


def run_menu(path: Path = ENV_PATH) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    actions: dict[str, Callable[[], None]] = {
        # 配置
        "1": lambda: _action_configure_bybit(path),
        "2": lambda: _action_configure_model(path),
        "3": lambda: _action_configure_telegram(path),
        "4": lambda: _action_configure_protection(path),
        "5": lambda: _action_full_status(path),
        # 自动推送
        "T": lambda: _action_enable_auto_push(path),
        "t": lambda: _action_enable_auto_push(path),
        "U": lambda: _action_disable_auto_push(),
        "u": lambda: _action_disable_auto_push(),
        "V": lambda: _action_change_interval(),
        "v": lambda: _action_change_interval(),
        # 运行
        "6": lambda: _action_run_auto_cycle(path),
        "7": lambda: _action_run_report_only(path),
        "8": lambda: _action_query_positions(path),
        # 开关
        "A": lambda: _action_toggle_protection(path),
        "a": lambda: _action_toggle_protection(path),
        # 诊断
        "B": lambda: _action_test_telegram(path),
        "b": lambda: _action_test_telegram(path),
        "C": lambda: _action_view_state(path),
        "c": lambda: _action_view_state(path),
        "D": lambda: _action_view_logs(),
        "d": lambda: _action_view_logs(),
        "G": lambda: _action_auto_push_status(),
        "g": lambda: _action_auto_push_status(),
        # 部署
        "I": lambda: _action_install_services(path),
        "i": lambda: _action_install_services(path),
    }

    while True:
        values = load_env(path)
        print(render_menu(values))
        try:
            choice = input("\n  请选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return

        if choice == "0":
            print("\n再见。")
            return

        action = actions.get(choice)
        if action:
            try:
                action()
            except KeyboardInterrupt:
                print("\n已取消。")
        else:
            print("  无效选项，请重新选择。")


if __name__ == "__main__":
    run_menu()
