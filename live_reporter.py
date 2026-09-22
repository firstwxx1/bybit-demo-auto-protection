"""Compatibility entry point for the Bybit live reporter."""
from bybit_live_reporter import (
    DEMO_BASE_URL,
    REPORT_SOURCE,
    REPORT_VERSION,
    build_decision_snapshot,
    build_protection_envelope,
    fetch_candles,
    fetch_positions,
    get_client,
    main,
    read_cache,
    run_active_close_cycle,
    write_cache,
)
# Private helpers are intentionally re-exported for legacy integrations and
# tests that used the old live_reporter module as their patch point.
from bybit_live_reporter import (
    _RealtimePositionReceipt,
    _fetch_positions,
    _position_snapshot_hashes,
    _receipt_snapshot_matches,
    _receipt_valid,
)

__all__ = [
    "DEMO_BASE_URL", "REPORT_SOURCE", "REPORT_VERSION", "build_decision_snapshot",
    "build_protection_envelope", "fetch_candles", "fetch_positions", "get_client",
    "main", "read_cache", "run_active_close_cycle", "write_cache",
    "_RealtimePositionReceipt", "_fetch_positions", "_position_snapshot_hashes",
    "_receipt_snapshot_matches", "_receipt_valid",
]

if __name__ == "__main__":
    main()
