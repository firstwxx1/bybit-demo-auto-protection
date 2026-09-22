"""Single-cycle entrypoint for existing-position Bybit Demo protection.

This compatibility entrypoint keeps the fixed stop fallback that existed in the
original project, but sends the protection through Bybit V5 ``Set Trading Stop``.
It never opens or increases a position.  The default is report-only and offline
fixtures are always blocked from execution.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from bybit_adapter import BybitDemoClient, BybitProtectionEngine
from bybit_live_reporter import fetch_positions

BYBIT_BASE_URL = "https://api-demo.bybit.com"


class DisabledTransport:
    """Test double that makes accidental disabled-mode API calls fail loudly."""

    def set_trading_stop(self, *_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("disabled transport must not be called")


def env_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _fixture_positions(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixture must be an object")
    positions = payload.get("positions", [payload.get("position")])
    return [row for row in positions if isinstance(row, dict)]


def run_protection_cycle(
    *,
    positions: list[dict[str, Any]],
    cached_at: str | None,
    state_path: Path,
    enabled: bool,
    transport: BybitDemoClient | DisabledTransport,
    stop_pct: float = 0.04855847842644323,
    take_profit_pct: float | None = None,
    failure_limit: int = 3,
) -> list[dict[str, Any]]:
    """Run one idempotent Bybit protection reconciliation cycle."""
    engine = BybitProtectionEngine(
        transport,  # type: ignore[arg-type]
        state_path,
        enabled=enabled,
        failure_limit=failure_limit,
    )
    return engine.reconcile(
        positions,
        cached_at=cached_at,
        stop_pct=stop_pct,
        take_profit_pct=take_profit_pct,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Demo existing-position TP/SL protection")
    parser.add_argument("--fixture", type=Path, help="offline position fixture; execution is always blocked")
    args = parser.parse_args()

    enabled = env_enabled(os.getenv("PROTECTION_EXECUTION_ENABLED", "false"))
    state_path = Path(os.getenv("PROTECTION_STATE_PATH", "state/protection-state.json"))
    cache_path = Path(os.getenv("POSITION_CACHE_PATH", "state/last-successful-positions.json"))
    stop_pct = float(os.getenv("FIXED_STOP_ENTRY_PCT", "0.04855847842644323"))
    take_profit_raw = os.getenv("FIXED_TAKE_PROFIT_MARK_PCT", "").strip()
    take_profit_pct = float(take_profit_raw) if take_profit_raw else None
    failure_limit = int(os.getenv("PROTECTION_FAILURE_LIMIT", "3"))

    if not enabled:
        print(json.dumps([], ensure_ascii=False, indent=2))
        return

    if os.getenv("BYBIT_TRADING_MODE", "demo").strip().lower() != "demo":
        print(json.dumps([{"status": "BLOCKED", "reason": "BYBIT_TRADING_MODE must be demo"}], ensure_ascii=False, indent=2))
        return

    if args.fixture:
        positions = _fixture_positions(args.fixture)
        result = [
            {"instrument": row.get("instrument"), "status": "FIXTURE_EXECUTION_BLOCKED"}
            for row in positions
        ]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    positions, cached_at = fetch_positions(cache_path)
    client = BybitDemoClient(
        os.getenv("BYBIT_DEMO_API_KEY", ""),
        os.getenv("BYBIT_DEMO_API_SECRET", ""),
        os.getenv("BYBIT_API_BASE", BYBIT_BASE_URL),
        timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
    )
    result = run_protection_cycle(
        positions=positions,
        cached_at=cached_at,
        state_path=state_path,
        enabled=True,
        transport=client,
        stop_pct=stop_pct,
        take_profit_pct=take_profit_pct,
        failure_limit=failure_limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
