from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import bybit_adapter
import dynamic_protection_service
import report_http_service
from active_close_adapter import ActiveCloseAdapter
from bybit_adapter import BybitDemoClient, BybitError, BybitProtectionEngine, normalize_position
from bybit_live_reporter import (
    _RealtimePositionReceipt,
    _receipt_valid,
    build_protection_envelope,
    fetch_positions,
    read_cache,
    write_cache,
)
from decision_layer import evaluate_decision


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return json.dumps(self.payload).encode("utf-8")


class _StopClient:
    def __init__(self):
        self.calls: list[tuple[dict, float, float | None]] = []

    def set_trading_stop(self, position, stop_loss, take_profit):
        self.calls.append((position, stop_loss, take_profit))
        return "trading-stop"


class _FailingStopClient:
    def set_trading_stop(self, *_args, **_kwargs):
        raise BybitError("simulated exchange failure")


def short_position(**overrides):
    position = normalize_position({
        "symbol": "ETHUSDT",
        "side": "Sell",
        "size": "1",
        "avgPrice": "3000",
        "markPrice": "2990",
        "liqPrice": "3300",
        "leverage": "10",
        "positionIdx": 0,
        "takeProfit": "2800",
        "stopLoss": "3050",
    })
    position.update(overrides)
    return position


class _CloseTransport:
    def __init__(self, actual):
        self.actual = actual
        self.close_calls = 0

    def get_position(self, instrument, position_idx=None):
        return self.actual

    def close_position(self, position, quantity=None):
        self.close_calls += 1
        return {"orderId": "demo-order-1"}

    def get_order(self, instrument, order_id):
        return {"orderStatus": "Filled", "cumExecQty": str(self.actual["size"])}


def close_decision(position: dict) -> dict:
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    candles = [{
        "ts": stamp,
        "open": 3000,
        "high": 3010,
        "low": 2980,
        "close": 2990,
        "volume": 100,
    }]
    news = {
        "available": True,
        "source": "grok",
        "evidence_verified": True,
        "as_of": stamp,
        "items": [{
            "headline": "material market event",
            "published_at": stamp,
            "source_url": "https://example.invalid/news",
            "severity": "high",
        }],
    }
    models = [
        {
            "source": "gpt-5.6-sol",
            "model": "gpt-5.6-sol",
            "available": True,
            "recommendation": "CLOSE_POSITION",
            "confidence": 0.95,
            "as_of": stamp,
        },
        {
            "source": "independent-risk",
            "model": "independent-risk",
            "available": True,
            "recommendation": "CLOSE_POSITION",
            "confidence": 0.95,
            "as_of": stamp,
        },
    ]
    snapshot = {"captured_at": stamp, "grok": news, "models": models, "candles": candles}
    return evaluate_decision(
        position,
        news,
        models,
        candles=candles,
        evidence_snapshot=snapshot,
        now=now,
    )


class BybitClientRuntimeTests(unittest.TestCase):
    def test_signed_request_is_demo_locked_and_canonical(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response({"retCode": 0, "result": {"list": []}})

        with patch.object(bybit_adapter, "urlopen", side_effect=fake_urlopen), patch.object(bybit_adapter.time, "time", return_value=1700000000.0):
            client = BybitDemoClient("demo-key", "demo-secret")
            result = client.request("GET", "/v5/market/kline", params={"symbol": "ETHUSDT", "category": "linear"})

        request = captured["request"]
        query = "category=linear&symbol=ETHUSDT"
        self.assertEqual(result["retCode"], 0)
        self.assertEqual(request.full_url, f"https://api-demo.bybit.com/v5/market/kline?{query}")
        self.assertEqual(request.get_header("X-bapi-api-key"), "demo-key")
        self.assertEqual(
            request.get_header("X-bapi-sign"),
            bybit_adapter._sign("demo-key", "demo-secret", "1700000000000", "5000", query),
        )
        self.assertEqual(captured["timeout"], 20)
        with self.assertRaises(BybitError):
            client.request("GET", "/v5/order/create")

    def test_positions_follow_cursor_and_candles_normalize(self):
        client = BybitDemoClient("key", "secret")
        client.request = Mock(side_effect=[
            {"retCode": 0, "result": {"list": [
                {"symbol": "ETHUSDT", "side": "Sell", "size": "1", "avgPrice": "3000", "markPrice": "2990", "liqPrice": "3300", "positionIdx": 0},
                {"symbol": "BTCUSDT", "side": "Buy", "size": "0", "positionIdx": 0},
            ], "nextPageCursor": "cursor-1"}},
            {"retCode": 0, "result": {"list": [
                {"symbol": "BTCUSDT", "side": "Buy", "size": "2", "avgPrice": "60000", "markPrice": "60100", "positionIdx": 1},
            ], "nextPageCursor": ""}},
        ])
        positions = client.positions()
        self.assertEqual([row["instrument"] for row in positions], ["ETHUSDT", "BTCUSDT"])
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(client.request.call_args_list[1].kwargs["params"]["cursor"], "cursor-1")

        client.request = Mock(return_value={"retCode": 0, "result": {"list": [[1700000000000, "1", "2", "0.5", "1.5", "10"]]}})
        candles = client.candles("ETHUSDT", interval="4H", limit=2)
        self.assertEqual(client.request.call_args.kwargs["params"]["interval"], "240")
        self.assertEqual(candles[0]["close"], "1.5")

    def test_close_order_and_sl_only_payload_are_fail_closed(self):
        client = BybitDemoClient("key", "secret")
        position = short_position()
        with patch.object(client, "request", return_value={"retCode": 0, "result": {"orderId": "x"}}) as request:
            self.assertEqual(client.set_trading_stop(position, 3050, None), "trading-stop")
        body = request.call_args.kwargs["body"]
        self.assertEqual(body["takeProfit"], "0")
        self.assertNotIn("tpTriggerBy", body)

        with patch.object(client, "request", return_value={"retCode": 0, "result": {"orderId": "close-1"}}) as request:
            result = client.create_close_order(position)
        self.assertEqual(result["side"], "Buy")
        self.assertTrue(result["reduceOnly"])
        self.assertTrue(result["closeOnTrigger"])
        self.assertEqual(request.call_args.args[1], "/v5/order/create")


class ProtectionEngineTests(unittest.TestCase):
    def test_update_then_idempotent_protected_and_sl_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protection.json"
            client = _StopClient()
            engine = BybitProtectionEngine(client, path, enabled=True)
            position = short_position(stop_loss=None, take_profit=None)
            candidate = {"ETHUSDT": {"stop_loss": 3050, "take_profit": 2800}}
            first = engine.reconcile_dynamic([position], candidate)
            self.assertEqual(first[0]["status"], "UPDATED")
            self.assertEqual(len(client.calls), 1)

            position.update(stop_loss=3050, take_profit=2800)
            second = engine.reconcile_dynamic([position], candidate)
            self.assertEqual(second[0]["status"], "PROTECTED")
            self.assertEqual(len(client.calls), 1)

            position["take_profit"] = "0"
            third = engine.reconcile_dynamic([position], {"ETHUSDT": {"stop_loss": 3050, "take_profit": None}})
            self.assertEqual(third[0]["status"], "PROTECTED")
            self.assertEqual(third[0]["protection_mode"], "SL_ONLY")

    def test_disabled_stale_invalid_and_circuit_paths_never_mutate(self):
        position = short_position(stop_loss=None, take_profit=None)
        candidate = {"ETHUSDT": {"stop_loss": 3050, "take_profit": 2800}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protection.json"
            client = _StopClient()
            self.assertEqual(BybitProtectionEngine(client, path, enabled=False).reconcile_dynamic([position], candidate)[0]["status"], "DISABLED")
            self.assertEqual(BybitProtectionEngine(client, path, enabled=True).reconcile_dynamic([position], candidate, cached_at="2026-09-22T00:00:00+00:00")[0]["status"], "STALE_POSITION_BLOCKED")
            invalid = BybitProtectionEngine(client, path, enabled=True).reconcile_dynamic([position], {"ETHUSDT": {"stop_loss": 2980, "take_profit": 3100}})
            self.assertEqual(invalid[0]["status"], "ERROR")
            self.assertEqual(len(client.calls), 0)

            failing = BybitProtectionEngine(_FailingStopClient(), Path(directory) / "failing.json", enabled=True, failure_limit=1)
            first = failing.reconcile_dynamic([position], candidate)
            second = failing.reconcile_dynamic([position], candidate)
            self.assertEqual(first[0]["status"], "ERROR")
            self.assertEqual(second[0]["status"], "CIRCUIT_OPEN")


class BoundaryTests(unittest.TestCase):
    def test_protection_bridge_validates_demo_realtime_and_report_only(self):
        captured = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        body = {
            "paper_only": True,
            "trading_mode": "demo",
            "report_version": "risk-report-v1",
            "source": "live_reporter",
            "captured_at": captured,
            "position_source": "realtime",
            "candidates": {"ETHUSDT": {"stop_loss": 3050, "take_profit": 2800}},
        }
        with patch.dict(os.environ, {
            "BYBIT_API_BASE": "https://api-demo.bybit.com",
            "BYBIT_TRADING_MODE": "demo",
            "PROTECTION_EXECUTION_ENABLED": "false",
            "ACTIVE_CLOSE_EXECUTION_ENABLED": "false",
        }, clear=False):
            result = dynamic_protection_service.protect_payload(body)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["mode"], "REPORT_ONLY")
            blocked = dynamic_protection_service.protect_payload({**body, "position_source": "cache"})
            self.assertEqual(blocked["status"], "BLOCKED")
            with self.assertRaises(dynamic_protection_service.ProtectionError):
                dynamic_protection_service.validate_protection_request({**body, "captured_at": "2025-01-01T00:00:00+00:00"})

    def test_protection_bridge_execution_uses_live_snapshot_and_rejects_cache(self):
        captured = datetime.now(timezone.utc).isoformat()
        body = {
            "paper_only": True,
            "trading_mode": "demo",
            "report_version": "risk-report-v1",
            "source": "live_reporter",
            "captured_at": captured,
            "position_source": "realtime",
            "candidates": {"ETHUSDT": {"stop_loss": 3050, "take_profit": 2800}},
        }
        position = {"instrument": "ETHUSDT"}
        with patch.dict(os.environ, {
            "BYBIT_API_BASE": "https://api-demo.bybit.com",
            "BYBIT_TRADING_MODE": "demo",
            "PROTECTION_EXECUTION_ENABLED": "true",
            "ACTIVE_CLOSE_EXECUTION_ENABLED": "false",
        }, clear=False), patch.object(dynamic_protection_service, "fetch_positions", return_value=([position], None)), patch.object(dynamic_protection_service, "BybitDemoClient"), patch.object(dynamic_protection_service, "BybitProtectionEngine") as engine_cls:
            engine_cls.return_value.reconcile_dynamic.return_value = [{"instrument": "ETHUSDT", "status": "UPDATED"}]
            result = dynamic_protection_service.protect_payload(body)
            self.assertEqual(result["mode"], "EXECUTION")
            engine_cls.return_value.reconcile_dynamic.assert_called_once()

        with patch.dict(os.environ, {
            "BYBIT_API_BASE": "https://api-demo.bybit.com",
            "BYBIT_TRADING_MODE": "demo",
            "PROTECTION_EXECUTION_ENABLED": "true",
            "ACTIVE_CLOSE_EXECUTION_ENABLED": "false",
        }, clear=False), patch.object(dynamic_protection_service, "fetch_positions", return_value=([position], "2026-09-22T00:00:00+00:00")):
            self.assertEqual(dynamic_protection_service.protect_payload(body)["status"], "STALE_POSITION_BLOCKED")

    def test_report_bridge_is_loopback_and_paper_only(self):
        with patch.dict(os.environ, {"BYBIT_API_BASE": "https://api-demo.bybit.com", "BYBIT_TRADING_MODE": "demo"}, clear=False):
            with patch.object(report_http_service.subprocess, "run", return_value=CompletedProcess(["python"], 0, stdout="report", stderr="")) as run:
                result = report_http_service.response_payload({"paper_only": True, "trading_mode": "demo", "request_active_close": False})
            self.assertEqual(result["exitCode"], 0)
            command = run.call_args.args[0]
            self.assertNotIn("--active-close-execution", command)
            self.assertEqual(run.call_args.kwargs["env"]["BYBIT_API_BASE"], "https://api-demo.bybit.com")
            self.assertEqual(report_http_service.response_payload({"paper_only": False, "trading_mode": "demo"})["exitCode"], 1)


class ReporterAndActiveCloseTests(unittest.TestCase):
    def test_receipt_invalidates_on_mutation_or_cache(self):
        position = short_position()
        receipt = _RealtimePositionReceipt([position])
        self.assertTrue(_receipt_valid(receipt, position, None))
        position["size"] = 2.0
        self.assertFalse(_receipt_valid(receipt, position, None))
        self.assertFalse(_receipt_valid(receipt, position, "2026-09-22T00:00:00+00:00"))
        old_receipt = _RealtimePositionReceipt([position])
        old_receipt._issued_monotonic = time.monotonic() - 301
        self.assertFalse(_receipt_valid(old_receipt, position, None))

    def test_cache_fallback_is_read_only_and_envelope_requires_realtime(self):
        position = short_position()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.json"
            write_cache(path, [position])
            with patch("bybit_live_reporter.get_client", side_effect=BybitError("offline")):
                positions, cached_at = fetch_positions(path)
            self.assertEqual(positions[0]["instrument"], "ETHUSDT")
            self.assertIsNotNone(cached_at)
            with self.assertRaises(ValueError):
                build_protection_envelope("止损保护建议：3050\n止损校验：通过", "ETHUSDT", datetime.now(timezone.utc).isoformat(), "cache")
            envelope = build_protection_envelope("止损保护建议：3050\n止损校验：通过\n止盈建议：2800；通过", "ETHUSDT", datetime.now(timezone.utc).isoformat(), "realtime")
            self.assertEqual(envelope["trading_mode"], "demo")
            self.assertEqual(envelope["candidates"]["ETHUSDT"]["take_profit"], 2800.0)

    def test_active_close_is_reduce_only_and_idempotent(self):
        position = short_position()
        decision = close_decision(position)
        self.assertEqual(decision["action"], "CLOSE_POSITION")
        with tempfile.TemporaryDirectory() as directory:
            transport = _CloseTransport(position)
            adapter = ActiveCloseAdapter(transport, Path(directory) / "audit.json", enabled=True)
            result = adapter.execute(decision)
            self.assertEqual(result["status"], "CLOSED")
            self.assertEqual(transport.close_calls, 1)
            duplicate = adapter.execute(decision)
            self.assertEqual(duplicate["status"], "DUPLICATE")
            self.assertEqual(transport.close_calls, 1)

    def test_active_close_partial_fill_opens_circuit(self):
        position = short_position()
        decision = close_decision(position)

        class Partial(_CloseTransport):
            def get_order(self, instrument, order_id):
                return {"orderStatus": "PartiallyFilled", "cumExecQty": "0.5"}

        with tempfile.TemporaryDirectory() as directory:
            adapter = ActiveCloseAdapter(Partial(position), Path(directory) / "audit.json", enabled=True, failure_limit=1)
            self.assertEqual(adapter.execute(decision)["status"], "CIRCUIT_OPEN")


if __name__ == "__main__":
    unittest.main()
