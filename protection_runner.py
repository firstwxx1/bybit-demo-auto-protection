"""Single-cycle entrypoint for existing-position stop protection on OKX demo."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from live_reporter import fetch_positions
from protection_engine import DemoTransport, ProtectionEngine, Transport

OKX_BASE_URL = "https://www.okx.com"


class DisabledTransport:
    def get_pending(self, instrument: str) -> list[dict[str, Any]]:
        raise RuntimeError("disabled transport must not be called")

    def place_stop(self, order: dict[str, str]) -> str:
        raise RuntimeError("disabled transport must not be called")

    def cancel(self, instrument: str, algo_ids: list[str]) -> None:
        raise RuntimeError("disabled transport must not be called")


def env_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def run_protection_cycle(
    *,
    positions: list[dict[str, Any]],
    cached_at: str | None,
    state_path: Path,
    enabled: bool,
    transport: Transport,
    stop_pct: float = 0.04855847842644323,
    take_profit_pct: float | None = None,
    failure_limit: int = 3,
) -> list[dict[str, Any]]:
    engine = ProtectionEngine(
        transport,
        state_path,
        enabled=enabled,
        stop_pct=stop_pct,
        take_profit_pct=take_profit_pct,
        failure_limit=failure_limit,
    )
    return engine.reconcile(positions, cached_at=cached_at)


def main() -> None:
    parser = argparse.ArgumentParser(description="OKX demo existing-position stop protection")
    parser.add_argument("--fixture", type=Path, help="offline position fixture for verification")
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

    if args.fixture:
        payload = json.loads(args.fixture.read_text(encoding="utf-8"))
        positions = payload.get("positions", [payload.get("position")])
        positions = [row for row in positions if isinstance(row, dict)]
        cached_at = payload.get("cached_at")
    else:
        positions, cached_at = fetch_positions(cache_path)

    if enabled:
        transport: Transport = DemoTransport(
            OKX_BASE_URL,
            os.getenv("OKX_DEMO_API_KEY", ""),
            os.getenv("OKX_DEMO_API_SECRET", ""),
            os.getenv("OKX_DEMO_PASSPHRASE", ""),
            timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        )
    else:
        transport = DisabledTransport()
    result = run_protection_cycle(
        positions=positions,
        cached_at=cached_at,
        state_path=state_path,
        enabled=enabled,
        transport=transport,
        stop_pct=stop_pct,
        take_profit_pct=take_profit_pct,
        failure_limit=failure_limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
