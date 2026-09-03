"""Read-only OKX collector with last-successful cache fallback."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from okx_demo_risk_reporter import ValidationError, build_report, fib_targets, position_from_snapshot
from protection_engine import evaluate_fib_close_candidate
from decision_layer import evaluate_decision
from model_clients import grok_news, independent_risk_analysis, risk_analysis
from active_close_adapter import ActiveCloseAdapter, CloseTransport, DemoCloseTransport

ALLOWED_PATHS = {"/api/v5/account/positions", "/api/v5/market/candles"}
_REALTIME_RECEIPTS: set[object] = set()
_CURRENT_REALTIME_RECEIPT_TOKEN: object | None = None
_REALTIME_RECEIPT_MAX_AGE_SECONDS = 300


class _RealtimePositionReceipt:
    """Opaque, module-issued proof of one successful uncached position read."""

    __slots__ = ("_token", "_positions", "_snapshot_hash", "_issued_monotonic")

    def __init__(self, positions: list[dict[str, Any]]) -> None:
        self._token = object()
        self._positions = positions
        self._snapshot_hash = _position_snapshot_hashes(positions)
        self._issued_monotonic = time.monotonic()


def _position_snapshot_hashes(positions: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        hashlib.sha256(json.dumps(position, sort_keys=True, ensure_ascii=True,
                                   separators=(",", ":"), default=str).encode()).hexdigest()
        for position in positions
    )


def _issue_realtime_receipt(
    positions: list[dict[str, Any]], context: object | None = None,
) -> _RealtimePositionReceipt:
    receipt = _RealtimePositionReceipt(positions)
    return receipt


def _receipt_snapshot_matches(receipt: object, position: dict[str, Any]) -> bool:
    if not isinstance(receipt, _RealtimePositionReceipt):
        return False
    for candidate, snapshot_hash in zip(receipt._positions, receipt._snapshot_hash):
        if candidate is position:
            return hashlib.sha256(json.dumps(position, sort_keys=True, ensure_ascii=True,
                                             separators=(",", ":"), default=str).encode()).hexdigest() == snapshot_hash
    return False


def okx_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    if path not in ALLOWED_PATHS:
        raise ValidationError("OKX路径不在只读允许清单")
    api_key = os.environ["OKX_DEMO_API_KEY"]
    secret = os.environ["OKX_DEMO_API_SECRET"]
    passphrase = os.environ["OKX_DEMO_PASSPHRASE"]
    query = "?" + urlencode(params) if params else ""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    signature = base64.b64encode(hmac.new(secret.encode(), f"{timestamp}GET{path}{query}".encode(), hashlib.sha256).digest()).decode()
    request = Request(os.getenv("OKX_API_BASE", "https://www.okx.com") + path + query, method="GET", headers={
        "OK-ACCESS-KEY": api_key, "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp, "OK-ACCESS-PASSPHRASE": passphrase,
        "x-simulated-trading": "1", "User-Agent": "okx-read-only-risk-reporter/1.0",
    })
    with urlopen(request, timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))) as response:
        payload = json.load(response)
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX只读查询失败：{payload.get('msg', 'unknown')}")
    return payload


def normalize_position(raw: dict[str, Any]) -> dict[str, Any]:
    pos = float(raw.get("pos") or 0)
    side = raw.get("posSide")
    if side not in {"long", "short"}:
        side = "long" if pos > 0 else "short"
    return {
        "instrument": raw["instId"], "side": side, "size": abs(pos), "size_unit": "张",
        "leverage": raw["lever"], "entry_price": raw["avgPx"],
        "mark_price": raw["markPx"], "unrealized_pnl": raw.get("upl", 0),
        "liquidation_price": raw.get("liqPx") or None,
        "margin_mode": raw.get("mgnMode"),
        "position_side": raw.get("posSide"),
    }


def write_cache(path: Path, positions: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"cached_at": datetime.now(timezone.utc).isoformat(), "positions": positions}, ensure_ascii=False), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def read_cache(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cached = datetime.fromisoformat(payload["cached_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - cached).total_seconds()
    if age > int(os.getenv("CACHE_MAX_AGE_SECONDS", "3600")):
        raise RuntimeError("持仓查询失败且最近成功缓存已过期")
    return payload


def _build_realtime_fetch_and_validator():
    """Bind authorization to the successful fetch path, not module state."""
    authorized: set[object] = set()

    def fetch(cache_path: Path, *, include_receipt: bool = False):
        try:
            rows = okx_get("/api/v5/account/positions", {"instType": "SWAP"})["data"]
            positions = [normalize_position(row) for row in rows if float(row.get("pos") or 0) != 0]
            write_cache(cache_path, positions)
            receipt = _RealtimePositionReceipt(positions)
            authorized.add(receipt._token)
            return (positions, None, receipt) if include_receipt else (positions, None)
        except (OSError, KeyError, ValueError, RuntimeError, URLError):
            cached = read_cache(cache_path)
            return (cached["positions"], cached["cached_at"], None) if include_receipt else (cached["positions"], cached["cached_at"])

    def valid(receipt: object, position: dict[str, Any], cached_at: str | None) -> bool:
        if not isinstance(receipt, _RealtimePositionReceipt) or cached_at is not None:
            return False
        if receipt._token not in authorized:
            return False
        if not any(candidate is position for candidate in receipt._positions):
            return False
        if not _receipt_snapshot_matches(receipt, position):
            return False
        age = time.monotonic() - receipt._issued_monotonic
        return 0 <= age <= _REALTIME_RECEIPT_MAX_AGE_SECONDS

    return fetch, valid


fetch_positions, _receipt_valid = _build_realtime_fetch_and_validator()


def build_protection_envelope(
    report: str, instrument: str, captured_at: str, position_source: str,
) -> dict[str, Any]:
    """Extract only explicitly validated prices into the bridge envelope."""
    if position_source != "realtime" or not instrument.endswith("-SWAP"):
        raise ValueError("dynamic protection requires a realtime SWAP position")
    stop_match = re.search(r"(?:安全修正止损价|止损保护建议)：([0-9]+(?:\.[0-9]+)?)", report)
    tp_match = re.search(r"止盈建议：([0-9]+(?:\.[0-9]+)?)；通过", report)
    if not stop_match or not tp_match or "止损校验：通过" not in report:
        raise ValueError("validated TP/SL candidates are missing")
    stop_loss = float(stop_match.group(1))
    take_profit = float(tp_match.group(1))
    if not all(math.isfinite(value) and value > 0 for value in (stop_loss, take_profit)):
        raise ValueError("validated TP/SL candidates are invalid")
    return {
        "paper_only": True,
        "trading_mode": "demo",
        "report_version": "risk-report-v1",
        "source": "live_reporter",
        "captured_at": captured_at,
        "candidates": {instrument: {"stop_loss": stop_loss, "take_profit": take_profit}},
    }


def fetch_candles(instrument: str, limit: int = 100, *, bar: str = "1m") -> list[dict[str, Any]]:
    """Read-only market candles normalized for the deterministic evidence gate."""
    payload = okx_get("/api/v5/market/candles", {"instId": instrument, "bar": bar, "limit": str(limit)})
    rows = payload.get("data", [])
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        result.append({"ts": datetime.fromtimestamp(float(row[0]) / 1000, timezone.utc).isoformat(),
                       "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5]})
    return result


def build_decision_snapshot(position: dict[str, Any], *, candles: list[dict[str, Any]] | None = None,
                            fib_candles: list[dict[str, Any]] | None = None,
                            news_client=grok_news, risk_client=risk_analysis,
                            second_risk_client=independent_risk_analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    """Orchestrate market evidence and separately sourced 4H Fib evidence."""
    candles = [] if candles is None else candles
    fib_candles = candles if fib_candles is None else fib_candles
    news = news_client(position)
    captured_at = datetime.now(timezone.utc).isoformat()
    try:
        fib = fib_targets(position_from_snapshot(position), fib_candles)
        fib["as_of"] = captured_at
    except (ValidationError, TypeError, ValueError):
        fib = {"valid": False, "reason": "行情证据无效", "as_of": captured_at}
    fib_close_candidate = evaluate_fib_close_candidate(
        position, fib, {}, now=datetime.fromisoformat(captured_at)
    )
    model = risk_client(position, fib, news)
    second_model = second_risk_client(position, fib, news)
    snapshot = {"captured_at": captured_at, "position": position, "candles": candles,
                "fib_candles": fib_candles, "fib": fib,
                "fib_close_candidate": fib_close_candidate,
                "fib_timeframe": "4H（局部摆动点）",
                "grok": news, "models": [model, second_model], "risk_model": model,
                "independent_risk_model": second_model}
    decision = evaluate_decision(position, news, [model, second_model], candles=candles, evidence_snapshot=snapshot)
    return snapshot, decision


def run_active_close_cycle(
    position: dict[str, Any], *, candles: list[dict[str, Any]], active_close_execution_enabled: bool = False,
    fib_candles: list[dict[str, Any]] | None = None,
    transport: CloseTransport | None = None, audit_path: Path | None = None, news_client=grok_news,
    sol_risk_client=risk_analysis, second_risk_client=independent_risk_analysis,
    position_source: str | None = None, cached_at: str | None = None,
    realtime_receipt: object | None = None, enabled: bool | None = None,
) -> dict[str, Any]:
    """Build evidence and execute only after an explicit, independently enabled close."""
    snapshot, decision = build_decision_snapshot(
        position, candles=candles, news_client=news_client, risk_client=sol_risk_client,
        second_risk_client=second_risk_client, fib_candles=fib_candles,
    )
    result = {"snapshot": snapshot, "decision": decision}
    if enabled is not None:
        active_close_execution_enabled = enabled
    if not active_close_execution_enabled:
        result["execution"] = {"status": "DRY_RUN", "decision_id": decision["decision_id"]}
        return result
    if cached_at is not None:
        result["execution"] = {
            "status": "STALE_POSITION_BLOCKED", "decision_id": decision["decision_id"],
        }
        return result
    if isinstance(realtime_receipt, _RealtimePositionReceipt) and not _receipt_snapshot_matches(realtime_receipt, position):
        result["execution"] = {
            "status": "POSITION_SNAPSHOT_MISMATCH", "decision_id": decision["decision_id"],
        }
        return result
    if not _receipt_valid(realtime_receipt, position, cached_at):
        result["execution"] = {
            "status": "POSITION_SOURCE_UNVERIFIED", "decision_id": decision["decision_id"],
        }
        return result
    if transport is None:
        raise ValueError("active close transport required when execution is enabled")
    adapter = ActiveCloseAdapter(transport, audit_path or Path("state/active-close-audit.json"), enabled=True)
    result["execution"] = adapter.execute(decision)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--active-close-execution", action="store_true")
    args = parser.parse_args()
    if args.fixture:
        print(build_report(json.loads(args.fixture.read_text(encoding="utf-8"))))
        return
    cache = Path(os.getenv("POSITION_CACHE_PATH", "state/last-successful-positions.json"))
    try:
        positions, cached_at, realtime_receipt = fetch_positions(cache, include_receipt=True)
    except TypeError:
        # Preserve compatibility with injected legacy fetchers; active execution then fails closed.
        positions, cached_at = fetch_positions(cache)
        realtime_receipt = None
    if not positions:
        print("【量化风险报告】\n当前OKX实际永续持仓为空；不执行交易。")
        return
    for position in positions:
        try:
            candles = fetch_candles(position["instrument"], bar="1m")
        except TypeError:
            # Preserve compatibility with injected legacy fetchers.
            candles = fetch_candles(position["instrument"])
        except (OSError, KeyError, ValueError, RuntimeError, URLError):
            candles = []
        fib_candles = []
        enabled = args.active_close_execution and os.getenv("ACTIVE_CLOSE_EXECUTION_ENABLED", "false").strip().lower() == "true"
        source = "cache" if cached_at is not None else "realtime"
        transport = None
        if enabled and source == "realtime":
            transport = DemoCloseTransport(
                os.getenv("OKX_API_BASE", "https://www.okx.com"), os.getenv("OKX_DEMO_API_KEY", ""),
                os.getenv("OKX_DEMO_API_SECRET", ""), os.getenv("OKX_DEMO_PASSPHRASE", ""),
                timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
            )
        cycle = run_active_close_cycle(
            position, candles=candles, active_close_execution_enabled=enabled, transport=transport,
            position_source=source, cached_at=cached_at, realtime_receipt=realtime_receipt,
            fib_candles=fib_candles,
            audit_path=Path(os.getenv("ACTIVE_CLOSE_AUDIT_PATH", "state/active-close-audit.json")),
        )
        snapshot, decision = cycle["snapshot"], cycle["decision"]
        snapshot["decision"] = decision
        snapshot["cached_at"] = cached_at
        print(build_report(snapshot))
        metadata = {"decision_id": decision["decision_id"], "rule_version": decision["rule_version"],
                    "action": decision["action"], "reason": decision["reason"]}
        try:
            metadata["protection_request"] = build_protection_envelope(
                build_report(snapshot), position["instrument"], snapshot["captured_at"], source
            )
        except (KeyError, TypeError, ValueError):
            metadata["protection_request"] = None
        print(json.dumps(metadata, ensure_ascii=False))
        print(json.dumps(cycle["execution"], ensure_ascii=False))


if __name__ == "__main__":
    main()
