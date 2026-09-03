import json
import math
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from live_reporter import fetch_positions
from protection_engine import DemoTransport, ProtectionEngine, ProtectionError


REPORT_VERSION = "risk-report-v1"
REPORT_SOURCE = "live_reporter"


def validate_protection_request(body: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Validate the signed-by-convention report envelope before any OKX call."""
    if not isinstance(body, dict) or body.get("paper_only") is not True or body.get("trading_mode") != "demo":
        raise ProtectionError("demo paper-only contract required")
    if body.get("report_version") != REPORT_VERSION or body.get("source") != REPORT_SOURCE:
        raise ProtectionError("report source or version is invalid")
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
    normalized: dict[str, dict[str, float]] = {}
    for instrument, candidate in candidates.items():
        if not isinstance(instrument, str) or not instrument.endswith("-SWAP") or not isinstance(candidate, dict):
            raise ProtectionError("candidate instrument or shape is invalid")
        try:
            stop = float(candidate["stop_loss"])
            take_profit = float(candidate["take_profit"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtectionError("candidate prices are invalid") from exc
        if not all(math.isfinite(value) and value > 0 for value in (stop, take_profit)):
            raise ProtectionError("candidate prices are invalid")
        normalized[instrument] = {"stop_loss": stop, "take_profit": take_profit}
    return {**body, "candidates": normalized, "captured_at": captured.isoformat()}


def protect_payload(body: dict[str, Any]) -> dict[str, Any]:
    if (os.getenv("PROTECTION_EXECUTION_ENABLED", "false").strip().lower() != "true"
            or os.getenv("OKX_TRADING_MODE", "").strip().lower() != "demo"
            or os.getenv("ACTIVE_CLOSE_EXECUTION_ENABLED", "false").strip().lower() == "true"):
        return {"status": "BLOCKED", "reason": "protection execution is disabled"}
    try:
        validated = validate_protection_request(body)
        cache = Path(os.getenv("POSITION_CACHE_PATH", "state/last-successful-positions.json"))
        fetched = fetch_positions(cache)
        positions, cached_at = fetched[0], fetched[1]
        if cached_at is not None:
            return {"status": "STALE_POSITION_BLOCKED"}
        transport = DemoTransport("https://www.okx.com", os.getenv("OKX_DEMO_API_KEY", ""), os.getenv("OKX_DEMO_API_SECRET", ""), os.getenv("OKX_DEMO_PASSPHRASE", ""))
        engine = ProtectionEngine(transport, Path(os.getenv("PROTECTION_STATE_PATH", "state/protection-state.json")), enabled=True)
        return {"status": "OK", "results": engine.reconcile_dynamic(positions, validated["candidates"])}
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, ProtectionError) as exc:
        return {"status": "BLOCKED", "reason": type(exc).__name__}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:
        if self.path != "/protect":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            raw = json.dumps(protect_payload(body), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (ValueError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.getenv("DYNAMIC_PROTECTION_PORT", "38636"))), Handler).serve_forever()
