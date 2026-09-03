from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from okx_demo_risk_reporter import position_from_snapshot
import protection_engine
from protection_engine import (
    CircuitBreaker,
    DemoTransport,
    ProtectionEngine,
    ProtectionError,
    build_stop_order,
    evaluate_fib_close_candidate,
)
from dynamic_protection_service import validate_protection_request

build_protection_order = getattr(protection_engine, "build_protection_order", None)


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
    def __init__(self, pending=None, fail=False):
        self.pending = list(pending or [])
        self.fail = fail
        self.calls = []

    def get_pending(self, instrument):
        self.calls.append(("pending", instrument))
        if self.fail:
            raise OSError("offline")
        return self.pending

    def place_stop(self, order):
        self.calls.append(("place", order))
        if self.fail:
            raise OSError("offline")
        return "new-algo"

    def cancel(self, instrument, algo_ids):
        self.calls.append(("cancel", instrument, tuple(algo_ids)))
        return None


class FailingCancelTransport(FakeTransport):
    def cancel(self, instrument, algo_ids):
        super().cancel(instrument, algo_ids)
        raise OSError("cancel failed")


class FailingReplacementCancelTransport(FakeTransport):
    def get_pending(self, instrument):
        self.calls.append(("pending", instrument))
        return [{
            "instId": instrument, "algoId": "old-algo", "side": "buy", "posSide": "short",
            "ordType": "conditional", "sz": "0.26", "slTriggerPx": "1900",
            "slOrdPx": "-1", "reduceOnly": "true",
        }]

    def cancel(self, instrument, algo_ids):
        self.calls.append(("cancel", instrument, tuple(algo_ids)))
        raise OSError("cancel failed")


class StopOrderTests(unittest.TestCase):
    def test_short_stop_is_reduce_only_buy_conditional(self):
        order = build_stop_order(position_from_snapshot(POSITION), 1968.7)
        self.assertEqual(order["instId"], "ETH-USDT-SWAP")
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["posSide"], "short")
        self.assertEqual(order["ordType"], "conditional")
        self.assertEqual(order["slTriggerPx"], "1968.7")
        self.assertEqual(order["slOrdPx"], "-1")
        self.assertEqual(order["reduceOnly"], "true")
        self.assertEqual(order["sz"], "0.26")
        self.assertEqual(order["tdMode"], "cross")
        self.assertEqual(order["posSide"], "short")

    def test_net_position_preserves_okx_position_mode(self):
        order = build_stop_order(
            position_from_snapshot(POSITION | {"position_side": "net"}), 1968.7
        )
        self.assertEqual(order["posSide"], "net")

    def test_protection_order_contains_reduce_only_take_profit_and_stop(self):
        order = build_protection_order(position_from_snapshot(POSITION), 1968.7, 1800.0)
        self.assertEqual(order["tpTriggerPx"], "1800")
        self.assertEqual(order["tpOrdPx"], "-1")
        self.assertEqual(order["slTriggerPx"], "1968.7")
        self.assertEqual(order["slOrdPx"], "-1")
        self.assertEqual(order["reduceOnly"], "true")

    def test_protection_order_rejects_wrong_direction_take_profit(self):
        with self.assertRaises(ProtectionError):
            build_protection_order(position_from_snapshot(POSITION), 1968.7, 2000.0)

    def test_dynamic_reconcile_uses_report_prices_and_reduce_only(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            result = ProtectionEngine(transport, Path(tmp) / "state.json", enabled=True).reconcile_dynamic(
                [POSITION], {"ETH-USDT-SWAP": {"stop_loss": 1968.7, "take_profit": 1744.479}}
            )
        self.assertEqual(result[0]["status"], "CREATED")
        order = transport.calls[1][1]
        self.assertEqual(order["tpTriggerPx"], "1744.479")
        self.assertEqual(order["slTriggerPx"], "1968.7")
        self.assertEqual(order["reduceOnly"], "true")

    def test_dynamic_request_requires_bound_report_metadata(self):
        request = {
            "paper_only": True,
            "trading_mode": "demo",
            "captured_at": "2026-08-27T03:25:17+00:00",
            "report_version": "risk-report-v1",
            "source": "live_reporter",
            "candidates": {"ETH-USDT-SWAP": {"stop_loss": 2558.4, "take_profit": 2421.12}},
        }
        self.assertEqual(validate_protection_request(request, now=datetime(2026, 8, 27, 3, 25, 20, tzinfo=timezone.utc)), request)

    def test_dynamic_request_rejects_future_timestamp(self):
        request = {
            "paper_only": True, "trading_mode": "demo", "captured_at": "2026-08-27T04:00:00+00:00",
            "report_version": "risk-report-v1", "source": "live_reporter",
            "candidates": {"ETH-USDT-SWAP": {"stop_loss": 2558.4, "take_profit": 2421.12}},
        }
        with self.assertRaises(ProtectionError):
            validate_protection_request(request, now=datetime(2026, 8, 27, 3, 25, 20, tzinfo=timezone.utc))

        request = {
            "paper_only": True,
            "trading_mode": "demo",
            "captured_at": "2026-08-27T03:25:17+00:00",
            "candidates": {"ETH-USDT-SWAP": {"stop_loss": 2558.4, "take_profit": 2421.12}},
        }
        with self.assertRaises(ProtectionError):
            validate_protection_request(request, now=datetime(2026, 8, 27, 3, 25, 20, tzinfo=timezone.utc))

        with self.assertRaises(ProtectionError):
            build_stop_order(position_from_snapshot(POSITION | {"margin_mode": None}), 1968.7)

    def test_demo_transport_rejects_non_okx_base(self):
        with self.assertRaises(ProtectionError):
            DemoTransport("https://attacker.invalid", "k", "s", "p")

    def test_demo_transport_always_sets_simulated_header(self):
        transport = DemoTransport("https://www.okx.com", "k", "s", "p")
        request = transport._build_request("GET", "/api/v5/trade/orders-algo-pending", {"ordType": "conditional"})
        self.assertEqual(request.headers["X-simulated-trading"], "1")


class EngineTests(unittest.TestCase):
    def test_fib_1272_returns_paper_candidate_for_partial_close(self):
        fib = {"valid": True, "fib_1272": 1800, "fib_1618": 1750, "as_of": "2026-07-21T09:00:00+00:00"}
        result = evaluate_fib_close_candidate(
            POSITION | {"mark_price": 1795}, fib, {},
            now=datetime(2026, 7, 21, 9, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "FIB_CLOSE_CANDIDATE")
        self.assertEqual(result["stage"], "fib_1272")
        self.assertEqual(result["close_fraction"], 0.25)
        self.assertEqual(result["execution"], "PAPER_ONLY")

    def test_fib_stage_is_idempotent(self):
        fib = {"valid": True, "fib_1272": 1800, "fib_1618": 1750, "as_of": "2026-07-21T09:00:00Z"}
        state = {"fib_1272_closed": True}
        result = evaluate_fib_close_candidate(
            POSITION | {"mark_price": 1795}, fib, state,
            now=datetime(2026, 7, 21, 9, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "NO_TRIGGER")

    def test_fib_candidate_without_evidence_time_is_blocked(self):
        fib = {"valid": True, "fib_1272": 1800, "fib_1618": 1750}
        result = evaluate_fib_close_candidate(POSITION | {"mark_price": 1795}, fib, {})
        self.assertEqual(result["status"], "FIB_CLOSE_BLOCKED")
        self.assertEqual(result["reason"], "FIB_EVIDENCE_TIME_MISSING")

    def test_expired_fib_evidence_is_blocked(self):
        fib = {"valid": True, "fib_1272": 1800, "fib_1618": 1750, "as_of": "2026-07-21T09:00:00Z"}
        result = evaluate_fib_close_candidate(
            POSITION | {"mark_price": 1795}, fib, {},
            now=datetime(2026, 7, 21, 9, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "FIB_CLOSE_BLOCKED")
        self.assertEqual(result["reason"], "FIB_EVIDENCE_STALE")

    def test_invalid_or_expired_fib_blocks_candidate(self):
        fib = {"valid": False, "reason": "Fib结构已失效"}
        result = evaluate_fib_close_candidate(POSITION, fib, {})
        self.assertEqual(result["status"], "FIB_CLOSE_BLOCKED")
        self.assertEqual(result["reason"], "Fib结构已失效")

    def test_fib_candidate_blocks_when_stop_boundary_is_breached(self):
        fib = {"valid": True, "fib_1272": 1800, "fib_1618": 1750,
               "as_of": "2026-07-21T09:00:00Z"}
        result = evaluate_fib_close_candidate(
            POSITION | {"mark_price": 1795, "liquidation_price": 1790}, fib, {},
            now=datetime(2026, 7, 21, 9, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "FIB_CLOSE_BLOCKED")
        self.assertEqual(result["reason"], "LIQUIDATION_BOUNDARY_BREACHED")

    def test_execution_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = ProtectionEngine(FakeTransport(), Path(tmp) / "state.json", enabled=False)
            result = engine.reconcile([POSITION])
        self.assertEqual(result[0]["status"], "DISABLED")
        self.assertEqual(engine.transport.calls, [])

    def test_cached_positions_fail_closed_without_transport_calls(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            engine = ProtectionEngine(transport, Path(tmp) / "state.json", enabled=True)
            result = engine.reconcile([POSITION], cached_at="2026-07-21T08:50:00Z")
        self.assertEqual(result[0]["status"], "STALE_POSITION_BLOCKED")
        self.assertEqual(transport.calls, [])

    def test_matching_combined_protection_order_is_idempotent(self):
        pending = [{"algoId": "tp-sl-1", "instId": "ETH-USDT-SWAP", "posSide": "short", "side": "buy",
                    "ordType": "conditional", "sz": "0.26", "slTriggerPx": "1968.7",
                    "slOrdPx": "-1", "tpTriggerPx": "1744.479", "tpOrdPx": "-1",
                    "reduceOnly": "true"}]
        transport = FakeTransport(pending)
        with tempfile.TemporaryDirectory() as tmp:
            result = ProtectionEngine(
                transport, Path(tmp) / "state.json", enabled=True, take_profit_pct=0.1
            ).reconcile([POSITION])
        self.assertEqual(result[0]["status"], "PROTECTED")
        self.assertEqual([call[0] for call in transport.calls], ["pending"])

    def test_matching_order_is_idempotent(self):
        pending = [{"algoId": "a1", "instId": "ETH-USDT-SWAP", "posSide": "short", "side": "buy",
                    "ordType": "conditional", "sz": "0.26", "slTriggerPx": "1968.7",
                    "slOrdPx": "-1", "reduceOnly": "true"}]
        transport = FakeTransport(pending)
        with tempfile.TemporaryDirectory() as tmp:
            result = ProtectionEngine(transport, Path(tmp) / "state.json", enabled=True).reconcile([POSITION])
        self.assertEqual(result[0]["status"], "PROTECTED")
        self.assertEqual([call[0] for call in transport.calls], ["pending"])

    def test_replacement_places_before_cancel(self):
        old = [{"algoId": "old", "instId": "ETH-USDT-SWAP", "posSide": "short", "side": "buy",
                "ordType": "conditional", "sz": "0.26", "slTriggerPx": "1970",
                "slOrdPx": "-1", "reduceOnly": "true"}]
        transport = FakeTransport(old)
        with tempfile.TemporaryDirectory() as tmp:
            result = ProtectionEngine(transport, Path(tmp) / "state.json", enabled=True).reconcile([POSITION])
        self.assertEqual(result[0]["status"], "REPLACED")
        self.assertEqual([call[0] for call in transport.calls], ["pending", "place", "cancel"])

    def test_failure_opens_persistent_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = FakeTransport(fail=True)
            engine = ProtectionEngine(transport, state, enabled=True, failure_limit=2)
            engine.reconcile([POSITION])
            result = engine.reconcile([POSITION])
            self.assertEqual(result[0]["status"], "ERROR")
            payload = json.loads(state.read_text())
            self.assertTrue(payload["circuit_open"])
            calls = len(transport.calls)
            blocked = engine.reconcile([POSITION])
            self.assertEqual(blocked[0]["status"], "CIRCUIT_OPEN")
            self.assertEqual(len(transport.calls), calls)

    def test_stop_invalid_at_current_mark_fails_closed(self):
        breached = POSITION | {"mark_price": 1970}
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            result = ProtectionEngine(transport, Path(tmp) / "state.json", enabled=True).reconcile([breached])
        self.assertEqual(result[0]["status"], "MANUAL_INTERVENTION_REQUIRED")
        self.assertEqual(transport.calls, [])

    def test_disappeared_position_cancels_only_persisted_managed_stop(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            engine = ProtectionEngine(transport, state, enabled=True)
            created = engine.reconcile([POSITION])
            self.assertEqual(created[0]["status"], "CREATED")
            transport.calls.clear()
            result = engine.reconcile([])
        self.assertEqual(result, [{"instrument": "ETH-USDT-SWAP", "status": "CANCELLED_CLOSED_POSITION"}])
        self.assertEqual(transport.calls, [("cancel", "ETH-USDT-SWAP", ("new-algo",))])

    def test_failure_stops_processing_later_positions(self):
        transport = FailingCancelTransport()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"managed_orders": {"BTC-USDT-SWAP": "old"}}))
            result = ProtectionEngine(transport, state, enabled=True).reconcile(
                [POSITION, POSITION | {"instrument": "SOL-USDT-SWAP"}]
            )
        self.assertEqual(result[0]["status"], "ERROR")
        self.assertEqual(len(result), 1)
        self.assertEqual([call[0] for call in transport.calls], ["cancel"])

    def test_new_stop_is_persisted_when_old_stop_cancel_fails(self):
        transport = FailingReplacementCancelTransport()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            result = ProtectionEngine(transport, state, enabled=True).reconcile([POSITION])
            persisted = json.loads(state.read_text())
        self.assertEqual(result[0]["status"], "ERROR")
        self.assertEqual(persisted["managed_orders"], {"ETH-USDT-SWAP": "new-algo"})


class CircuitBreakerTests(unittest.TestCase):
    def test_state_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            breaker = CircuitBreaker(path, failure_limit=3)
            breaker.record_failure("x")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
