"""GPT-driven automatic TP/SL protection runner for OKX demo.

This module is the main 30-minute cycle entry point. It:

1. Fetches live OKX demo positions (read-only GET).
2. For each position, fetches 4H candles, calls Grok (news) + GPT (risk analysis).
3. Generates the risk report (same format as the existing reporter).
4. Extracts validated TP/SL prices from the report.
5. If PROTECTION_EXECUTION_ENABLED=true, places reduce-only conditional orders
   (TP+SL) on OKX demo. Orders are reconciled each cycle: existing matching
   orders are kept; stale ones are replaced.
6. Sends the report to Telegram via the bot API.

Security boundaries (unchanged from the original project):
- Only OKX demo (x-simulated-trading: 1).
- Only reduceOnly conditional orders; never opens, adds, or reverses.
- Stale (cached) positions never trigger order placement.
- Circuit breaker persists consecutive failures.
- GPT TP/SL must pass deterministic validation (direction, liquidation, RR).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_reporter import fetch_positions, fetch_candles
from model_clients import grok_news, risk_analysis, independent_risk_analysis
from okx_demo_risk_reporter import (
    Position,
    build_report,
    fib_targets,
    position_from_snapshot,
    fixed_stop,
    validate_stop,
)
from protection_engine import (
    DemoTransport,
    ProtectionEngine,
    ProtectionError,
    build_protection_order,
    build_stop_order,
)
from protection_runner import env_enabled
from telegram_notifier import send_telegram

OKX_BASE_URL = "https://www.okx.com"


def _extract_validated_prices(report: str) -> tuple[float | None, float | None]:
    """Extract validated TP/SL from the report text.

    Returns (stop_loss, take_profit). Either may be None if not validated.
    """
    stop_loss: float | None = None
    take_profit: float | None = None

    # Stop loss: look for the final validated stop-loss line.
    # The report has either:
    #   **止损保护建议：1918**
    #   **安全修正止损价：1918（强平缓冲边界）**
    stop_match = re.search(
        r"(?:止损保护建议|安全修正止损价)[：:]\s*([0-9]+(?:\.[0-9]+)?)", report
    )
    if stop_match:
        stop_loss = float(stop_match.group(1))

    # Take profit: only if validated (report shows a number, not "暂不采用").
    #   **止盈建议：1831.9139；通过（...）**
    tp_match = re.search(r"止盈建议[：:]\s*([0-9]+(?:\.[0-9]+)?)\s*[；;]\s*通过", report)
    if tp_match:
        take_profit = float(tp_match.group(1))

    return stop_loss, take_profit


def _format_telegram_report(report: str, protection_results: list[dict[str, Any]]) -> str:
    """Format the plain-text report into a Telegram-friendly HTML message.

    The existing build_report() already outputs Markdown-ish text with ** bold.
    We strip the ** markers for HTML and add a protection status footer.
    """
    # Convert **bold** to HTML <b>bold</b>
    lines = report.split("\n")
    html_lines: list[str] = []
    for line in lines:
        converted = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        html_lines.append(converted)

    # Add protection status footer
    if protection_results:
        footer_lines = ["", "── 自动止盈止损 ──"]
        for item in protection_results:
            status = item.get("status", "UNKNOWN")
            inst = item.get("instrument", "")
            if status == "PROTECTED":
                footer_lines.append(f"✅ {inst}: 保护单已存在 (algoId: {item.get('algoId', 'N/A')})")
            elif status == "CREATED":
                sl = item.get("stop_loss", item.get("stop", "N/A"))
                tp = item.get("take_profit", "N/A")
                footer_lines.append(f"🆕 {inst}: 已挂止盈止损单 (止损={sl}, 止盈={tp})")
            elif status == "REPLACED":
                sl = item.get("stop_loss", "N/A")
                tp = item.get("take_profit", "N/A")
                footer_lines.append(f"🔄 {inst}: 已更新止盈止损单 (止损={sl}, 止盈={tp})")
            elif status == "DISABLED":
                footer_lines.append(f"⏸️ {inst}: 保护执行未开启（仅报告）")
            elif status == "STALE_POSITION_BLOCKED":
                footer_lines.append(f"⚠️ {inst}: 持仓数据非实时，跳过下单")
            elif status == "CIRCUIT_OPEN":
                footer_lines.append(f"🔴 {inst}: 熔断中，需人工检查")
            elif status == "ERROR":
                footer_lines.append(f"❌ {inst}: 错误 ({item.get('error', 'unknown')})")
            elif status == "MANUAL_INTERVENTION_REQUIRED":
                footer_lines.append(f"🔧 {inst}: 需人工干预 ({item.get('reason', '')})")
            elif status == "CANCELLED_CLOSED_POSITION":
                footer_lines.append(f"🗑️ {inst}: 已平仓，撤销保护单")
            else:
                footer_lines.append(f"ℹ️ {inst}: {status}")
        html_lines.extend(footer_lines)
    else:
        html_lines.extend(["", "── 自动止盈止损 ──", "ℹ️ 当前无持仓，无需保护"])

    html_lines.append(f"\n🕐 生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(html_lines)


def run_auto_cycle(
    *,
    enabled: bool,
    fixture: Path | None = None,
    send_tg: bool = True,
) -> dict[str, Any]:
    """Run one full auto-protection cycle.

    Returns a dict with the report text, protection results, and telegram status.
    """
    cache_path = Path(os.getenv("POSITION_CACHE_PATH", "state/last-successful-positions.json"))
    state_path = Path(os.getenv("PROTECTION_STATE_PATH", "state/protection-state.json"))
    failure_limit = int(os.getenv("PROTECTION_FAILURE_LIMIT", "3"))

    # --- 1. Fetch positions (fixture or live) ---
    if fixture:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        positions = payload.get("positions", [payload.get("position")])
        positions = [row for row in positions if isinstance(row, dict)]
        cached_at = payload.get("cached_at")
        realtime_receipt = None
    else:
        try:
            positions, cached_at, realtime_receipt = fetch_positions(cache_path, include_receipt=True)
        except TypeError:
            positions, cached_at = fetch_positions(cache_path)
            realtime_receipt = None

    if not positions:
        report_text = "【OKX模拟盘量化风险报告】\n当前OKX实际永续持仓为空；不执行交易。"
        tg_result = send_telegram(_format_telegram_report(report_text, [])) if send_tg else {"ok": False, "skipped": True}
        return {"report": report_text, "protection_results": [], "telegram": tg_result}

    # --- 2. For each position: build snapshot + report ---
    all_reports: list[str] = []
    all_protection_results: list[dict[str, Any]] = []
    candidates: dict[str, dict[str, float]] = {}

    for position in positions:
        instrument = position["instrument"]

        # Fetch 4H candles for Fib + MA calculations
        try:
            candles = fetch_candles(instrument, limit=100, bar="4H")
        except (TypeError, OSError, KeyError, ValueError, RuntimeError):
            try:
                candles = fetch_candles(instrument, limit=100)
            except (TypeError, OSError, KeyError, ValueError, RuntimeError):
                candles = []

        # Fetch 1m candles for short-term structure
        try:
            candles_1m = fetch_candles(instrument, limit=100, bar="1m")
        except (TypeError, OSError, KeyError, ValueError, RuntimeError):
            candles_1m = []

        # Build news + model analysis
        news = grok_news(position)
        captured_at = datetime.now(timezone.utc).isoformat()

        try:
            pos_obj = position_from_snapshot(position)
            fib = fib_targets(pos_obj, candles)
            fib["as_of"] = captured_at
        except (ValueError, TypeError):
            fib = {"valid": False, "reason": "行情证据无效", "as_of": captured_at}

        from protection_engine import evaluate_fib_close_candidate
        fib_close_candidate = evaluate_fib_close_candidate(
            position, fib, {}, now=datetime.fromisoformat(captured_at)
        )

        model = risk_analysis(position, fib, news)
        second_model = independent_risk_analysis(position, fib, news)

        from decision_layer import evaluate_decision
        snapshot = {
            "captured_at": captured_at,
            "timestamp": captured_at,
            "position": position,
            "candles": candles_1m,
            "fib_candles": candles,
            "fib": fib,
            "fib_close_candidate": fib_close_candidate,
            "fib_timeframe": "4H（局部摆动点）",
            "grok": news,
            "models": [model, second_model],
            "risk_model": model,
            "independent_risk_model": second_model,
        }
        decision = evaluate_decision(
            position, news, [model, second_model],
            candles=candles_1m, evidence_snapshot=snapshot,
        )
        snapshot["decision"] = decision
        snapshot["cached_at"] = cached_at

        report = build_report(snapshot)
        all_reports.append(report)

        # --- 3. Extract validated TP/SL ---
        stop_loss, take_profit = _extract_validated_prices(report)

        if stop_loss is not None and take_profit is not None:
            candidates[instrument] = {
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        elif stop_loss is not None:
            # TP not validated — place stop-loss only.
            candidates[instrument] = {
                "stop_loss": stop_loss,
                "take_profit": None,  # type: ignore
            }

    # --- 4. Execute protection ---
    if enabled and not cached_at and candidates:
        transport = DemoTransport(
            OKX_BASE_URL,
            os.getenv("OKX_DEMO_API_KEY", ""),
            os.getenv("OKX_DEMO_API_SECRET", ""),
            os.getenv("OKX_DEMO_PASSPHRASE", ""),
            timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        )
        engine = ProtectionEngine(
            transport,
            state_path,
            enabled=True,
            failure_limit=failure_limit,
        )
        # Separate TP+SL candidates from SL-only candidates
        full_candidates = {
            inst: c for inst, c in candidates.items() if c.get("take_profit") is not None
        }
        sl_only_candidates = {
            inst: c["stop_loss"] for inst, c in candidates.items() if c.get("take_profit") is None
        }

        if full_candidates:
            all_protection_results.extend(
                engine.reconcile_dynamic(positions, full_candidates, cached_at=cached_at)
            )

        # For SL-only positions, use reconcile with fixed stop override
        # We need a custom approach: use reconcile_dynamic with a synthetic TP
        # that will be rejected, or better: extend reconcile to handle SL-only.
        # For now, SL-only uses the fixed reconcile path.
        if sl_only_candidates:
            # Place stop-loss only orders via direct engine calls
            for inst, sl_price in sl_only_candidates.items():
                pos_snapshot = next(
                    (p for p in positions if p.get("instrument") == inst), None
                )
                if not pos_snapshot:
                    continue
                try:
                    pos_obj = position_from_snapshot(pos_snapshot)
                    order = build_stop_order(pos_obj, sl_price)
                    pending = transport.get_pending(inst)
                    from protection_engine import ProtectionEngine as PE
                    matches = [
                        row for row in pending
                        if PE._matching(row, order)
                    ]
                    if matches:
                        all_protection_results.append({
                            "instrument": inst, "status": "PROTECTED",
                            "algoId": matches[0].get("algoId", ""),
                        })
                        continue
                    old_ids = [
                        str(row["algoId"]) for row in pending
                        if PE._owned_protection(row, order)
                    ]
                    new_id = transport.place_stop(order)
                    if old_ids:
                        transport.cancel(inst, old_ids)
                        all_protection_results.append({
                            "instrument": inst, "status": "REPLACED",
                            "algoId": new_id, "stop_loss": order["slTriggerPx"],
                        })
                    else:
                        all_protection_results.append({
                            "instrument": inst, "status": "CREATED",
                            "algoId": new_id, "stop_loss": order["slTriggerPx"],
                        })
                except (OSError, KeyError, TypeError, ValueError, ProtectionError) as exc:
                    all_protection_results.append({
                        "instrument": inst, "status": "ERROR",
                        "error": type(exc).__name__,
                    })

    elif enabled and cached_at:
        all_protection_results = [
            {"instrument": p.get("instrument"), "status": "STALE_POSITION_BLOCKED"}
            for p in positions
        ]
    elif not enabled:
        all_protection_results = [
            {"instrument": p.get("instrument"), "status": "DISABLED"}
            for p in positions
        ]

    # --- 5. Format and send Telegram ---
    combined_report = "\n\n".join(all_reports)
    tg_message = _format_telegram_report(combined_report, all_protection_results)
    tg_result = send_telegram(tg_message) if send_tg else {"ok": False, "skipped": True}

    return {
        "report": combined_report,
        "protection_results": all_protection_results,
        "telegram": tg_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OKX demo GPT auto TP/SL protection runner (30-min cycle)"
    )
    parser.add_argument("--fixture", type=Path, help="offline position fixture for testing")
    parser.add_argument("--no-telegram", action="store_true", help="skip Telegram push")
    parser.add_argument("--no-protection", action="store_true", help="skip order placement (report only)")
    args = parser.parse_args()

    enabled = env_enabled(os.getenv("PROTECTION_EXECUTION_ENABLED", "false")) and not args.no_protection
    send_tg = not args.no_telegram

    result = run_auto_cycle(
        enabled=enabled,
        fixture=args.fixture,
        send_tg=send_tg,
    )

    print(result["report"])
    if result["protection_results"]:
        print("\n" + json.dumps(result["protection_results"], ensure_ascii=False, indent=2))
    tg = result["telegram"]
    if tg.get("ok"):
        print("\n✅ Telegram 推送成功")
    elif tg.get("skipped"):
        print("\n⏸️ Telegram 推送已跳过")
    else:
        print(f"\n⚠️ Telegram 推送失败: {tg.get('error', 'unknown')}")


if __name__ == "__main__":
    main()
