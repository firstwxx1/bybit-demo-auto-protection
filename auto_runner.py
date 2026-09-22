"""GPT-driven automatic TP/SL protection runner for Bybit mainnet Demo Trading."""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bybit_adapter import BybitDemoClient, BybitProtectionEngine
from bybit_live_reporter import fetch_candles, fetch_positions
from bybit_protection_core import evaluate_fib_close_candidate
from bybit_risk_reporter import build_report, fib_targets, position_from_snapshot
from decision_layer import evaluate_decision
from model_clients import grok_news, independent_risk_analysis, risk_analysis
from telegram_notifier import send_telegram

BYBIT_BASE_URL = "https://api-demo.bybit.com"


def env_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _extract_validated_prices(report: str) -> tuple[float | None, float | None]:
    stop_match = re.search(r"(?:止损保护建议|安全修正止损价)[：:]\s*([0-9]+(?:\.[0-9]+)?)", report)
    tp_match = re.search(r"止盈建议[：:]\s*([0-9]+(?:\.[0-9]+)?)\s*[；;]\s*通过", report)
    return (
        float(stop_match.group(1)) if stop_match else None,
        float(tp_match.group(1)) if tp_match else None,
    )


def _format_telegram_report(report: str, protection_results: list[dict[str, Any]]) -> str:
    """Convert report markdown markers and append Bybit protection status."""
    html_lines = [re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line) for line in report.split("\n")]
    footer = ["", "── Bybit Demo 自动止盈止损 ──"]
    if not protection_results:
        footer.append("ℹ️ 当前无持仓或没有可执行的保护候选")
    for item in protection_results:
        status = item.get("status", "UNKNOWN")
        instrument = item.get("instrument", "")
        if status == "PROTECTED":
            footer.append(f"✅ {instrument}: 保护参数已存在（{item.get('protection_mode', 'TP_SL')}）")
        elif status in {"UPDATED", "CREATED", "REPLACED"}:
            footer.append(f"🆕 {instrument}: 已更新{item.get('protection_mode', 'TP_SL')}（止损={item.get('stop_loss', 'N/A')}，止盈={item.get('take_profit', '已清除') if item.get('take_profit') is not None else '已清除'}）")
        elif status == "DISABLED":
            footer.append(f"⏸️ {instrument}: 保护执行未开启（仅报告）")
        elif status == "STALE_POSITION_BLOCKED":
            footer.append(f"⚠️ {instrument}: 持仓数据来自缓存，跳过下单")
        elif status == "FIXTURE_EXECUTION_BLOCKED":
            footer.append(f"🧪 {instrument}: 离线fixture禁止下单")
        elif status == "NO_VALID_CANDIDATE":
            footer.append(f"ℹ️ {instrument}: 没有通过校验的保护候选")
        elif status == "CIRCUIT_OPEN":
            footer.append(f"🔴 {instrument}: 熔断中，需要人工检查")
        elif status == "ERROR":
            footer.append(f"❌ {instrument}: 执行错误（{item.get('error', 'unknown')}）")
        elif status == "POST_EXECUTION_STATE_UNKNOWN":
            footer.append(f"🔴 {instrument}: API已调用但状态文件未知，禁止继续执行")
        else:
            footer.append(f"ℹ️ {instrument}: {status}")
    html_lines.extend(footer)
    html_lines.append(f"\n🕐 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(html_lines)


def _load_fixture(path: Path) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    positions = payload.get("positions", [payload.get("position")])
    positions = [row for row in positions if isinstance(row, dict)]
    candles_4h = payload.get("fib_candles") or payload.get("candles_4h") or payload.get("candles") or []
    candles_1m = payload.get("candles_1m") or payload.get("candles") or []
    return positions, payload.get("cached_at"), candles_4h, candles_1m


def run_auto_cycle(*, enabled: bool, fixture: Path | None = None, send_tg: bool = True) -> dict[str, Any]:
    cache_path = Path(os.getenv("POSITION_CACHE_PATH", "state/last-successful-positions.json"))
    state_path = Path(os.getenv("PROTECTION_STATE_PATH", "state/protection-state.json"))
    fixture_mode = fixture is not None
    if fixture_mode:
        positions, cached_at, fixture_4h, fixture_1m = _load_fixture(fixture)  # type: ignore[arg-type]
        realtime_receipt = None
    else:
        positions, cached_at, realtime_receipt = fetch_positions(cache_path, include_receipt=True)
        fixture_4h, fixture_1m = [], []

    if not positions:
        report_text = "【Bybit Demo量化风险报告】\n当前Bybit线性永续持仓为空；不执行交易。"
        tg_result = send_telegram(_format_telegram_report(report_text, [])) if send_tg else {"ok": False, "skipped": True}
        return {"report": report_text, "protection_results": [], "telegram": tg_result}

    all_reports: list[str] = []
    candidates: dict[str, dict[str, float | None]] = {}
    for position in positions:
        instrument = str(position["instrument"])
        if fixture_mode:
            candles_4h, candles_1m = fixture_4h, fixture_1m
        else:
            try:
                candles_4h = fetch_candles(instrument, limit=100, bar="4H")
            except (TypeError, OSError, KeyError, ValueError, RuntimeError):
                candles_4h = []
            try:
                candles_1m = fetch_candles(instrument, limit=100, bar="1m")
            except (TypeError, OSError, KeyError, ValueError, RuntimeError):
                candles_1m = []
        news = grok_news(position)
        captured_at = datetime.now(timezone.utc).isoformat()
        try:
            fib = fib_targets(position_from_snapshot(position), candles_4h)
            fib["as_of"] = captured_at
        except (ValueError, TypeError):
            fib = {"valid": False, "reason": "行情证据无效", "as_of": captured_at}
        fib_close_candidate = evaluate_fib_close_candidate(position, fib, {}, now=datetime.fromisoformat(captured_at))
        model = risk_analysis(position, fib, news)
        second_model = independent_risk_analysis(position, fib, news)
        snapshot = {
            "captured_at": captured_at,
            "timestamp": captured_at,
            "position": position,
            "candles": candles_1m,
            "fib_candles": candles_4h,
            "fib": fib,
            "fib_close_candidate": fib_close_candidate,
            "fib_timeframe": "4H（局部摆动点）",
            "grok": news,
            "models": [model, second_model],
            "risk_model": model,
            "independent_risk_model": second_model,
            "cached_at": cached_at,
        }
        decision = evaluate_decision(position, news, [model, second_model], candles=candles_1m, evidence_snapshot=snapshot)
        snapshot["decision"] = decision
        report = build_report(snapshot)
        all_reports.append(report)
        stop_loss, take_profit = _extract_validated_prices(report)
        if stop_loss is not None:
            candidates[instrument] = {"stop_loss": stop_loss, "take_profit": take_profit}

    protection_results: list[dict[str, Any]] = []
    if not enabled:
        protection_results = [{"instrument": p.get("instrument"), "status": "DISABLED"} for p in positions]
    elif fixture_mode:
        protection_results = [{"instrument": p.get("instrument"), "status": "FIXTURE_EXECUTION_BLOCKED"} for p in positions]
    elif cached_at is not None:
        protection_results = [{"instrument": p.get("instrument"), "status": "STALE_POSITION_BLOCKED"} for p in positions]
    elif os.getenv("BYBIT_TRADING_MODE", "demo").strip().lower() != "demo" or os.getenv("BYBIT_API_BASE", BYBIT_BASE_URL).rstrip("/") != BYBIT_BASE_URL:
        protection_results = [{"instrument": p.get("instrument"), "status": "BLOCKED", "reason": "Bybit Demo host and trading mode are required"} for p in positions]
    elif candidates:
        client = BybitDemoClient(
            os.getenv("BYBIT_DEMO_API_KEY", ""),
            os.getenv("BYBIT_DEMO_API_SECRET", ""),
            os.getenv("BYBIT_API_BASE", BYBIT_BASE_URL),
            timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        )
        engine = BybitProtectionEngine(client, state_path, enabled=True)
        protection_results = engine.reconcile_dynamic(positions, candidates)
    else:
        protection_results = [{"instrument": p.get("instrument"), "status": "NO_VALID_CANDIDATE"} for p in positions]

    combined_report = "\n\n".join(all_reports)
    tg_result = send_telegram(_format_telegram_report(combined_report, protection_results)) if send_tg else {"ok": False, "skipped": True}
    return {"report": combined_report, "protection_results": protection_results, "telegram": tg_result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Demo GPT auto TP/SL protection runner")
    parser.add_argument("--fixture", type=Path, help="offline position fixture")
    parser.add_argument("--no-telegram", action="store_true", help="skip Telegram push")
    parser.add_argument("--no-protection", action="store_true", help="report only")
    args = parser.parse_args()
    enabled = env_enabled(os.getenv("PROTECTION_EXECUTION_ENABLED")) and not args.no_protection
    result = run_auto_cycle(enabled=enabled, fixture=args.fixture, send_tg=not args.no_telegram)
    print(result["report"])
    if result["protection_results"]:
        print("\n" + json.dumps(result["protection_results"], ensure_ascii=False, indent=2))
    telegram = result["telegram"]
    if telegram.get("ok"):
        print("\n✅ Telegram推送成功")
    elif telegram.get("skipped"):
        print("\n⏸️ Telegram推送已跳过")
    else:
        print(f"\n⚠️ Telegram推送失败：{telegram.get('error', 'unknown')}")


if __name__ == "__main__":
    main()
