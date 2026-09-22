"""Bybit Demo live collector, evidence receipt, and active-close orchestration.

This is the Bybit replacement for the original read-only live reporter.  A
successful uncached position read issues an opaque receipt.  Active close is
allowed only when the same position object and its content still match that
receipt, so a cache fallback or a mutated snapshot can never authorize an
order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from active_close_adapter import ActiveCloseAdapter, BybitCloseTransport, CloseTransport
from bybit_adapter import BybitDemoClient, BybitError
from bybit_protection_core import evaluate_fib_close_candidate
from bybit_risk_reporter import ValidationError, build_report, fib_targets, position_from_snapshot
from decision_layer import evaluate_decision
from model_clients import grok_news, independent_risk_analysis, risk_analysis

REPORT_VERSION = "risk-report-v1"
REPORT_SOURCE = "live_reporter"
DEMO_BASE_URL = "https://api-demo.bybit.com"
_REALTIME_RECEIPT_MAX_AGE_SECONDS = 300


class _RealtimePositionReceipt:
    __slots__ = ("_token", "_positions", "_snapshot_hash", "_issued_monotonic")

    def __init__(self, positions: list[dict[str, Any]]) -> None:
        self._token = object()
        self._positions = positions
        self._snapshot_hash = _position_snapshot_hashes(positions)
        self._issued_monotonic = time.monotonic()


def _position_snapshot_hashes(positions: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        hashlib.sha256(
            json.dumps(position, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        for position in positions
    )


def _receipt_snapshot_matches(receipt: object, position: dict[str, Any]) -> bool:
    if not isinstance(receipt, _RealtimePositionReceipt):
        return False
    for candidate, snapshot_hash in zip(receipt._positions, receipt._snapshot_hash):
        if candidate is position:
            current = hashlib.sha256(
                json.dumps(position, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()
            return current == snapshot_hash
    return False


def write_cache(path: Path, positions: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"cached_at": datetime.now(timezone.utc).isoformat(), "positions": positions}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def read_cache(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        raise BybitError("Bybit持仓缓存格式无效")
    cached = datetime.fromisoformat(str(payload["cached_at"]).replace("Z", "+00:00"))
    if cached.tzinfo is None:
        cached = cached.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - cached).total_seconds()
    if age < -30 or age > int(os.getenv("CACHE_MAX_AGE_SECONDS", "3600")):
        raise BybitError("Bybit持仓查询失败且缓存已过期")
    return payload


def get_client() -> BybitDemoClient:
    return BybitDemoClient(
        os.getenv("BYBIT_DEMO_API_KEY", ""),
        os.getenv("BYBIT_DEMO_API_SECRET", ""),
        os.getenv("BYBIT_API_BASE", DEMO_BASE_URL),
        float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
    )


def _fetch_positions(cache_path: Path, *, include_receipt: bool = False):
    try:
        positions = get_client().positions(os.getenv("BYBIT_SETTLE_COIN", "USDT"))
        write_cache(cache_path, positions)
        receipt = _RealtimePositionReceipt(positions)
        return (positions, None, receipt) if include_receipt else (positions, None)
    except (BybitError, OSError, ValueError, KeyError, TypeError):
        payload = read_cache(cache_path)
        return (payload["positions"], payload["cached_at"], None) if include_receipt else (payload["positions"], payload["cached_at"])


def _receipt_valid(receipt: object, position: dict[str, Any], cached_at: str | None) -> bool:
    if cached_at is not None or not isinstance(receipt, _RealtimePositionReceipt):
        return False
    if not _receipt_snapshot_matches(receipt, position):
        return False
    age = time.monotonic() - receipt._issued_monotonic
    return 0 <= age <= _REALTIME_RECEIPT_MAX_AGE_SECONDS


# Public function is kept as a module-level callable so tests and integrations
# can inject/patch it exactly as they did with the original reporter.
fetch_positions = _fetch_positions


def fetch_candles(instrument: str, limit: int = 100, *, bar: str = "1m") -> list[dict[str, Any]]:
    interval = "240" if str(bar).lower() in {"4h", "240"} else bar
    return get_client().candles(instrument, limit=limit, interval=interval)


def build_protection_envelope(
    report: str,
    instrument: str,
    captured_at: str,
    position_source: str,
) -> dict[str, Any]:
    """Extract only deterministic, explicitly validated TP/SL values."""
    if position_source != "realtime" or not instrument.endswith("USDT"):
        raise ValueError("dynamic protection requires a realtime Bybit linear position")
    stop_match = re.search(r"(?:安全修正止损价|止损保护建议)[：:]\s*([0-9]+(?:\.[0-9]+)?)", report)
    tp_match = re.search(r"止盈建议[：:]\s*([0-9]+(?:\.[0-9]+)?)\s*[；;]\s*通过", report)
    if not stop_match or "止损校验：通过" not in report:
        raise ValueError("validated stop-loss candidate is missing")
    stop_loss = float(stop_match.group(1))
    take_profit = float(tp_match.group(1)) if tp_match else None
    if not math.isfinite(stop_loss) or stop_loss <= 0 or (take_profit is not None and (not math.isfinite(take_profit) or take_profit <= 0)):
        raise ValueError("validated TP/SL candidates are invalid")
    return {
        "paper_only": True,
        "trading_mode": "demo",
        "report_version": REPORT_VERSION,
        "source": REPORT_SOURCE,
        "captured_at": captured_at,
        "position_source": position_source,
        "candidates": {instrument: {"stop_loss": stop_loss, "take_profit": take_profit}},
    }


def build_decision_snapshot(
    position: dict[str, Any],
    *,
    candles: list[dict[str, Any]] | None = None,
    fib_candles: list[dict[str, Any]] | None = None,
    news_client=grok_news,
    risk_client=risk_analysis,
    second_risk_client=independent_risk_analysis,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candles = [] if candles is None else candles
    fib_candles = candles if fib_candles is None else fib_candles
    news = news_client(position)
    captured_at = datetime.now(timezone.utc).isoformat()
    try:
        fib = fib_targets(position_from_snapshot(position), fib_candles)
        fib["as_of"] = captured_at
    except (ValidationError, TypeError, ValueError):
        fib = {"valid": False, "reason": "行情证据无效", "as_of": captured_at}
    fib_close_candidate = evaluate_fib_close_candidate(position, fib, {}, now=datetime.fromisoformat(captured_at))
    model = risk_client(position, fib, news)
    second_model = second_risk_client(position, fib, news)
    snapshot = {
        "captured_at": captured_at,
        "timestamp": captured_at,
        "position": position,
        "candles": candles,
        "fib_candles": fib_candles,
        "fib": fib,
        "fib_close_candidate": fib_close_candidate,
        "fib_timeframe": "4H（局部摆动点）",
        "grok": news,
        "models": [model, second_model],
        "risk_model": model,
        "independent_risk_model": second_model,
    }
    decision = evaluate_decision(position, news, [model, second_model], candles=candles, evidence_snapshot=snapshot)
    return snapshot, decision


def run_active_close_cycle(
    position: dict[str, Any],
    *,
    candles: list[dict[str, Any]],
    active_close_execution_enabled: bool = False,
    fib_candles: list[dict[str, Any]] | None = None,
    transport: CloseTransport | None = None,
    audit_path: Path | None = None,
    news_client=grok_news,
    sol_risk_client=risk_analysis,
    second_risk_client=independent_risk_analysis,
    position_source: str | None = None,
    cached_at: str | None = None,
    realtime_receipt: object | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    snapshot, decision = build_decision_snapshot(
        position,
        candles=candles,
        news_client=news_client,
        risk_client=sol_risk_client,
        second_risk_client=second_risk_client,
        fib_candles=fib_candles,
    )
    result: dict[str, Any] = {"snapshot": snapshot, "decision": decision}
    if enabled is not None:
        active_close_execution_enabled = enabled
    if not active_close_execution_enabled:
        result["execution"] = {"status": "DRY_RUN", "decision_id": decision["decision_id"]}
        return result
    if cached_at is not None:
        result["execution"] = {"status": "STALE_POSITION_BLOCKED", "decision_id": decision["decision_id"]}
        return result
    if isinstance(realtime_receipt, _RealtimePositionReceipt) and not _receipt_snapshot_matches(realtime_receipt, position):
        result["execution"] = {"status": "POSITION_SNAPSHOT_MISMATCH", "decision_id": decision["decision_id"]}
        return result
    if not _receipt_valid(realtime_receipt, position, cached_at):
        result["execution"] = {"status": "POSITION_SOURCE_UNVERIFIED", "decision_id": decision["decision_id"]}
        return result
    if transport is None:
        raise ValueError("active close transport required when execution is enabled")
    adapter = ActiveCloseAdapter(transport, audit_path or Path("state/active-close-audit.json"), enabled=True)
    result["execution"] = adapter.execute(decision)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Demo live risk report and active-close gate")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--active-close-execution", action="store_true")
    args = parser.parse_args()
    if args.fixture:
        print(build_report(json.loads(args.fixture.read_text(encoding="utf-8"))))
        return
    cache = Path(os.getenv("POSITION_CACHE_PATH", "state/last-successful-positions.json"))
    positions, cached_at, receipt = fetch_positions(cache, include_receipt=True)
    if not positions:
        print("【Bybit Demo量化风险报告】\n当前Bybit线性永续持仓为空；不执行交易。")
        return
    for position in positions:
        try:
            candles_1m = fetch_candles(position["instrument"], bar="1m")
        except (BybitError, OSError, KeyError, TypeError, ValueError):
            candles_1m = []
        try:
            candles_4h = fetch_candles(position["instrument"], bar="4H")
        except (BybitError, OSError, KeyError, TypeError, ValueError):
            candles_4h = []
        source = "cache" if cached_at is not None else "realtime"
        enabled = args.active_close_execution and os.getenv("ACTIVE_CLOSE_EXECUTION_ENABLED", "false").strip().lower() == "true"
        transport = None
        if enabled and source == "realtime":
            transport = BybitCloseTransport(
                os.getenv("BYBIT_DEMO_API_KEY", ""),
                os.getenv("BYBIT_DEMO_API_SECRET", ""),
                os.getenv("BYBIT_API_BASE", DEMO_BASE_URL),
                timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
            )
        cycle = run_active_close_cycle(
            position,
            candles=candles_1m,
            fib_candles=candles_4h,
            active_close_execution_enabled=enabled,
            transport=transport,
            position_source=source,
            cached_at=cached_at,
            realtime_receipt=receipt,
            audit_path=Path(os.getenv("ACTIVE_CLOSE_AUDIT_PATH", "state/active-close-audit.json")),
        )
        snapshot, decision = cycle["snapshot"], cycle["decision"]
        snapshot["decision"] = decision
        snapshot["cached_at"] = cached_at
        report = build_report(snapshot)
        print(report)
        metadata: dict[str, Any] = {
            "decision_id": decision["decision_id"],
            "rule_version": decision["rule_version"],
            "action": decision["action"],
            "reason": decision["reason"],
        }
        try:
            metadata["protection_request"] = build_protection_envelope(report, position["instrument"], snapshot["captured_at"], source)
        except (KeyError, TypeError, ValueError):
            metadata["protection_request"] = None
        print(json.dumps(metadata, ensure_ascii=False))
        print(json.dumps(cycle["execution"], ensure_ascii=False))


if __name__ == "__main__":
    main()
