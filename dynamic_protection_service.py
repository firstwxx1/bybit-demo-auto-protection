"""Loopback-only n8n boundary for Bybit Demo dynamic TP/SL protection."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bybit_adapter import BybitDemoClient, BybitProtectionEngine
from bybit_live_reporter import fetch_positions

REPORT_VERSION = "risk-report-v1"
REPORT_SOURCE = "live_reporter"
DEMO_BASE_URL = "https://api-demo.bybit.com"


class ProtectionError(RuntimeError):
    pass


def _is_demo_configured() -> bool:
    return (
        os.getenv("BYBIT_API_BASE", DEMO_BASE_URL).rstrip("/") == DEMO_BASE_URL
        and os.getenv("BYBIT_TRADING_MODE", "demo").strip().lower() == "demo"
    )


def validate_protection_request(body: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("paper_only") is not True or body.get("trading_mode") != "demo":
        raise ProtectionError("Bybit Demo paper-only contract required")
    if body.get("report_version") != REPORT_VERSION or body.get("source") != REPORT_SOURCE:
        raise ProtectionError("report source or version is invalid")
    if body.get("position_source") != "realtime":
        raise ProtectionError("dynamic protection requires a realtime report")
    try:
        captured = datetime.fromisoformat(str(body["captured_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtectionError("report timestamp is invalid") from exc
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = (now - captured).total_seconds()
    if age > 900 or age < -30:
        raise ProtectionError("report expired")
    candidates = body.get("candidates")
    if not isinstance(candidates, dict) or not candidates:
        raise ProtectionError("dynamic candidates missing")
    normalized: dict[str, dict[str, float | None]] = {}
    for instrument, candidate in candidates.items():
        if not isinstance(instrument, str) or not instrument.isupper() or not instrument.endswith("USDT") or not isinstance(candidate, dict):
            raise ProtectionError("candidate instrument or shape is invalid")
        try:
            stop = float(candidate["stop_loss"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtectionError("candidate stop loss is invalid") from exc
        raw_tp = candidate.get("take_profit")
        take_profit = None if raw_tp in (None, "", "0", 0) else float(raw_tp)
        values = [stop] + ([] if take_profit is None else [take_profit])
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ProtectionError("candidate prices are invalid")
        normalized[instrument] = {"stop_loss": stop, "take_profit": take_profit}
    return {**body, "candidates": normalized, "captured_at": captured.isoformat()}


def protect_payload(body: dict[str, Any]) -> dict[str, Any]:
    # Validate the envelope even in report-only mode.  This prevents n8n from
    # accidentally treating an unrelated payload as an execution request.
    if not _is_demo_configured():
        return {"status": "BLOCKED", "reason": "Bybit Demo configuration is required"}
    try:
        validated = validate_protection_request(body)
    except (KeyError, TypeError, ValueError, ProtectionError):
        return {"status": "BLOCKED", "reason": "PROTECTION_VALIDATION_OR_RUNTIME_ERROR"}

    if os.getenv("ACTIVE_CLOSE_EXECUTION_ENABLED", "false").strip().lower() == "true":
        return {"status": "BLOCKED", "reason": "active-close execution must be isolated from dynamic protection"}

    # Report-only is the safe default.  It is a successful, non-trading result
    # so the n8n workflow can still deliver the risk report and show that no
    # exchange mutation was attempted.
    if os.getenv("PROTECTION_EXECUTION_ENABLED", "false").strip().lower() != "true":
        return {
            "status": "OK",
            "mode": "REPORT_ONLY",
            "reason": "PROTECTION_EXECUTION_ENABLED is false; no Bybit request was sent",
            "results": [],
        }

    try:
        cache = Path(os.getenv("POSITION_CACHE_PATH", "state/last-successful-positions.json"))
        positions, cached_at = fetch_positions(cache)
        if cached_at is not None:
            return {"status": "STALE_POSITION_BLOCKED"}
        live_instruments = {str(row.get("instrument", "")) for row in positions}
        candidate_instruments = set(validated["candidates"])
        if not candidate_instruments or not candidate_instruments.issubset(live_instruments):
            return {"status": "POSITION_SNAPSHOT_MISMATCH"}
        client = BybitDemoClient(
            os.getenv("BYBIT_DEMO_API_KEY", ""),
            os.getenv("BYBIT_DEMO_API_SECRET", ""),
            DEMO_BASE_URL,
            timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        )
        engine = BybitProtectionEngine(
            client,
            Path(os.getenv("PROTECTION_STATE_PATH", "state/protection-state.json")),
            enabled=True,
        )
        return {"status": "OK", "mode": "EXECUTION", "results": engine.reconcile_dynamic(positions, validated["candidates"])}
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, ProtectionError):
        return {"status": "BLOCKED", "reason": "PROTECTION_VALIDATION_OR_RUNTIME_ERROR"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok", "paper_only": True, "trading_mode": "demo", "loopback_only": True})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/protect":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 256 * 1024:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("request must be an object")
            self._send_json(200, protect_payload(body))
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"status": "BLOCKED", "reason": "INVALID_JSON"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.getenv("DYNAMIC_PROTECTION_PORT", "38636"))), Handler).serve_forever()
