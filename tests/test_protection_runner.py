from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from protection_runner import run_protection_cycle


POSITION = {
    "instrument": "ETH-USDT-SWAP",
    "side": "short",
    "size": 0.26,
    "leverage": 10,
    "entry_price": 1877.53,
    "mark_price": 1938.31,
    "unrealized_pnl": -1.58,
    "liquidation_price": 2059.56,
    "margin_mode": "cross",
    "position_side": "short",
}


class FakeTransport:
    def __init__(self):
        self.calls = []

    def get_pending(self, instrument):
        self.calls.append(("pending", instrument))
        return []

    def place_stop(self, order):
        self.calls.append(("place", order))
        return "algo-1"

    def cancel(self, instrument, algo_ids):
        self.calls.append(("cancel", instrument, algo_ids))


class RunnerTests(unittest.TestCase):
    def test_cached_positions_are_blocked_before_engine(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_protection_cycle(
                positions=[POSITION], cached_at="2026-08-19T00:00:00Z",
                state_path=Path(tmp) / "state.json", enabled=True,
                transport=transport,
            )
        self.assertEqual(result[0]["status"], "STALE_POSITION_BLOCKED")
        self.assertEqual(transport.calls, [])

    def test_live_positions_reconcile_through_offline_transport(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_protection_cycle(
                positions=[POSITION], cached_at=None,
                state_path=Path(tmp) / "state.json", enabled=True,
                transport=transport,
            )
        self.assertEqual(result[0]["status"], "CREATED")
        self.assertEqual(transport.calls[0][0], "pending")
        self.assertEqual(transport.calls[1][0], "place")

    def test_combined_tp_sl_is_forwarded_to_transport(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_protection_cycle(
                positions=[POSITION], cached_at=None,
                state_path=Path(tmp) / "state.json", enabled=True,
                transport=transport, take_profit_pct=0.1,
            )
        self.assertEqual(result[0]["status"], "CREATED")
        order = transport.calls[1][1]
        self.assertEqual(order["tpTriggerPx"], "1744.479")
        self.assertEqual(order["slTriggerPx"], "1968.7")
        self.assertEqual(order["reduceOnly"], "true")

    def test_disabled_cycle_does_not_call_transport(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_protection_cycle(
                positions=[POSITION], cached_at=None,
                state_path=Path(tmp) / "state.json", enabled=False,
                transport=transport,
            )
        self.assertEqual(result[0]["status"], "DISABLED")
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
