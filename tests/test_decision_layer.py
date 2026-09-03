from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decision_layer import Action, DecisionError, evaluate_decision
from active_close_adapter import ActiveCloseAdapter, CloseError, DemoCloseTransport
from live_reporter import build_decision_snapshot


POSITION = {
    "instrument": "ETH-USDT-SWAP", "side": "short", "size": 0.26,
    "leverage": 10, "entry_price": 1877.53, "mark_price": 1938.31,
    "unrealized_pnl": -1.58, "liquidation_price": 2059.56,
    "margin_mode": "cross", "position_side": "short",
}


def iso(minutes_ago: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def news(**overrides):
    value = {"available": True, "as_of": iso(), "source": "grok",
             "evidence_verified": True,
             "items": [{"id": "n1", "published_at": iso(), "headline": "urgent", "severity": "high", "source_url": "https://example.invalid/n1"}]}
    value.update(overrides)
    return value


def model(**overrides):
    value = {"available": True, "source": "gpt-5.6-sol", "model": "gpt-5.6-sol", "as_of": iso(),
             "confidence": 0.95, "recommendation": "CLOSE_POSITION"}
    value.update(overrides)
    return value


def independent_model(**overrides):
    value = {"available": True, "source": "independent-risk", "model": "independent-risk-v1", "as_of": iso(),
             "confidence": 0.95, "recommendation": "CLOSE_POSITION"}
    value.update(overrides)
    return value


def valid_decision(**overrides):
    candles = [{"ts": iso(), "open": "1900", "high": "1950", "low": "1850", "close": "1938", "volume": "10"}]
    result = evaluate_decision(POSITION, news(), [model(), independent_model()], candles=candles)
    result.update(overrides)
    return result


    def test_independent_source_unavailable_is_distinguished_from_gpt_failure(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(minutes=1)).isoformat()
        candles = [{"ts": recent, "open": "1900", "high": "1950", "low": "1850", "close": "1938", "volume": "10"}]
        unavailable = {"available": False, "source": "independent-risk", "error": "独立风险来源未注入"}
        result = evaluate_decision(
            POSITION, news(as_of=recent, items=[{"headline": "urgent", "published_at": recent,
                                                  "source_url": "https://example.invalid/n1", "severity": "high"}]),
            [model(as_of=recent), unavailable], now=now, candles=candles,
        )
        self.assertEqual(result["action"], Action.MANUAL_REVIEW_REQUIRED.value)
        self.assertEqual(result["reason"], "INDEPENDENT_RISK_SOURCE_UNAVAILABLE")

    def test_gpt_unavailable_is_distinguished_from_independent_source_failure(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(minutes=1)).isoformat()
        candles = [{"ts": recent, "open": "1900", "high": "1950", "low": "1850", "close": "1938", "volume": "10"}]
        unavailable = {"available": False, "source": "gpt-5.6-sol", "error": "GPT不可用：TimeoutError"}
        result = evaluate_decision(
            POSITION, news(as_of=recent, items=[{"headline": "urgent", "published_at": recent,
                                                  "source_url": "https://example.invalid/n1", "severity": "high"}]),
            [unavailable, independent_model(as_of=recent)], now=now, candles=candles,
        )
        self.assertEqual(result["action"], Action.MANUAL_REVIEW_REQUIRED.value)
        self.assertEqual(result["reason"], "GPT_MODEL_UNAVAILABLE")

    def test_rss_news_older_than_15_minutes_but_within_24_hours_is_accepted(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(minutes=30)).isoformat()
        rss = news(source="publisher-rss", as_of=recent,
                   items=[{"id": "rss-1", "published_at": recent, "headline": "ETH update",
                           "severity": "high", "source": "publisher-rss",
                           "source_url": "https://example.invalid/rss-1"}])
        candles = [{"ts": recent, "open": "1900", "high": "1950", "low": "1850",
                    "close": "1938", "volume": "10"}]
        result = evaluate_decision(
            POSITION, rss,
            [model(as_of=recent), independent_model(as_of=recent)],
            now=now, candles=candles,
        )
        self.assertEqual(result["action"], Action.CLOSE_POSITION.value)
        self.assertEqual(result["reason"], "CONSENSUS_VALIDATED")
        self.assertEqual(result["evidence_window"]["max_age_seconds"], 86400)

    def test_future_or_missing_rss_item_evidence_is_rejected(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        fresh = (now - timedelta(minutes=1)).isoformat()
        future = (now + timedelta(minutes=1)).isoformat()
        candles = [{"ts": fresh, "open": "1900", "high": "1950", "low": "1850",
                    "close": "1938", "volume": "10"}]
        for item in (
            {"headline": "future", "published_at": future, "source_url": "https://example.invalid/future"},
            {"headline": "missing url", "published_at": fresh},
        ):
            result = evaluate_decision(
                POSITION, news(source="publisher-rss", as_of=fresh, items=[item]),
                [model(as_of=fresh), independent_model(as_of=fresh)],
                now=now, candles=candles,
            )
            self.assertEqual(result["action"], Action.HOLD.value)
            self.assertEqual(result["reason"], "NEWS_ITEM_STALE_OR_INVALID")

    def test_historical_window_is_valid_when_latest_candle_is_fresh(self):
        candles = [
            {"ts": iso(), "open": "1900", "high": "1950", "low": "1850", "close": "1938", "volume": "10"},
            {"ts": iso(60), "open": "1880", "high": "1920", "low": "1840", "close": "1900", "volume": "12"},
        ]
        result = evaluate_decision(POSITION, news(), [model(), independent_model()], candles=candles)
        self.assertEqual(result["action"], Action.CLOSE_POSITION.value)
        self.assertEqual(result["reason"], "CONSENSUS_VALIDATED")

    def test_missing_grok_or_gpt_evidence_never_closes(self):
        candles = [{"ts": iso(), "open": "1900", "high": "1950", "low": "1850", "close": "1938", "volume": "10"}]
        _, no_grok = build_decision_snapshot(POSITION, candles=candles, news_client=lambda p: {"available": False}, risk_client=lambda p, f, n: model())
        _, no_gpt = build_decision_snapshot(POSITION, candles=candles, news_client=lambda p: news(), risk_client=lambda p, f, n: {"available": False})
        self.assertNotEqual(no_grok["action"], Action.CLOSE_POSITION.value)
        self.assertNotEqual(no_gpt["action"], Action.CLOSE_POSITION.value)
    def test_empty_market_evidence_cannot_close_and_both_models_are_called(self):
        calls = []
        def grok(position):
            calls.append("grok")
            return news()
        def gpt(position, fib, evidence):
            calls.append("gpt")
            return model()
        snapshot, result = build_decision_snapshot(POSITION, candles=[], news_client=grok, risk_client=gpt)
        self.assertEqual(calls, ["grok", "gpt"])
        self.assertIn(result["action"], {Action.HOLD.value, Action.MANUAL_REVIEW_REQUIRED.value})
        self.assertNotEqual(result["action"], Action.CLOSE_POSITION.value)
        self.assertEqual(snapshot["candles"], [])

    def test_stale_market_evidence_cannot_close(self):
        stale = [{"ts": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "o": "1", "h": "2", "l": "0", "c": "1", "vol": "1"}]
        snapshot, result = build_decision_snapshot(POSITION, candles=stale, news_client=lambda p: news(), risk_client=lambda p, f, n: model())
        self.assertNotEqual(result["action"], Action.CLOSE_POSITION.value)
    def test_missing_news_is_hold(self):
        result = evaluate_decision(POSITION, {}, [model()])
        self.assertEqual(result["action"], Action.HOLD.value)

    def test_stale_news_never_closes(self):
        result = evaluate_decision(POSITION, news(as_of=iso(60)), [model()])
        self.assertIn(result["action"], {Action.HOLD.value, Action.MANUAL_REVIEW_REQUIRED.value})

    def test_conflicting_models_require_manual_review(self):
        result = evaluate_decision(POSITION, news(), [model(), model(model="second", recommendation="HOLD")])
        self.assertIn(result["action"], {Action.HOLD.value, Action.MANUAL_REVIEW_REQUIRED.value})

    def test_low_confidence_cannot_close(self):
        result = evaluate_decision(POSITION, news(), [model(confidence=0.4)])
        self.assertNotEqual(result["action"], Action.CLOSE_POSITION.value)

    def test_unparseable_model_is_fail_closed(self):
        result = evaluate_decision(POSITION, news(), [{"available": True, "model": "gpt-5.6-sol", "raw": "garbage"}])
        self.assertIn(result["action"], {Action.HOLD.value, Action.MANUAL_REVIEW_REQUIRED.value})

    def test_invalid_action_is_rejected(self):
        result = evaluate_decision(POSITION, news(), [model(recommendation="BUY_MORE")])
        self.assertEqual(result["action"], Action.MANUAL_REVIEW_REQUIRED.value)
        self.assertEqual(result["reason"], "MODEL_ACTION_INVALID")


class CloseAdapterRejectTests(unittest.TestCase):
    def test_missing_or_tampered_structured_decision_is_rejected_without_order(self):
        class Fake:
            def __init__(self): self.orders = 0
            def get_position(self, instrument): return POSITION
            def close_position(self, order): self.orders += 1; return {"ordId": "o1", "state": "filled"}
            def get_order(self, instrument, order_id): return {"state": "filled", "accFillSz": "0.26"}
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(); adapter = ActiveCloseAdapter(fake, Path(tmp) / "audit.json", enabled=True)
            result = adapter.execute({"decision_id": "bad", "action": "CLOSE_POSITION", "position": POSITION})
        self.assertIn(result["status"], {"REJECTED", "CIRCUIT_OPEN"})
        self.assertEqual(fake.orders, 0)

    def test_corrupt_state_fails_closed_and_persists_circuit(self):
        class Fake:
            def __init__(self): self.orders = 0
            def get_position(self, instrument): return POSITION
            def close_position(self, order): self.orders += 1; return {"ordId": "o1", "state": "filled"}
            def get_order(self, instrument, order_id): return {"state": "filled", "accFillSz": "0.26"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.json"; path.write_text("{broken", encoding="utf-8")
            fake = Fake(); result = ActiveCloseAdapter(fake, path, enabled=True).execute({})
            persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "CIRCUIT_OPEN")
        self.assertTrue(persisted["circuit_open"])
        self.assertEqual(fake.orders, 0)
    def test_transport_is_pinned_to_simulated_okx_and_allowlist(self):
        with self.assertRaises(CloseError):
            DemoCloseTransport("https://attacker.invalid", "k", "s", "p")
        transport = DemoCloseTransport("https://www.okx.com", "k", "s", "p")
        request = transport._build_request("POST", "/api/v5/trade/order", {})
        self.assertEqual(request.headers["X-simulated-trading"], "1")
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_close_requires_fresh_matching_position_and_reduce_only(self):
        class Fake:
            def __init__(self): self.calls = []
            def get_position(self, instrument): self.calls.append(("position", instrument)); return POSITION
            def close_position(self, order): self.calls.append(("close", order)); return {"ordId": "o1", "state": "filled"}
            def get_order(self, instrument, order_id): self.calls.append(("order", instrument, order_id)); return {"state": "filled", "accFillSz": "0.26"}
        with tempfile.TemporaryDirectory() as tmp:
            result = ActiveCloseAdapter(Fake(), Path(tmp) / "audit.json", enabled=True).execute(
                valid_decision())
        self.assertEqual(result["status"], "CLOSED")
        self.assertTrue(result["order"]["reduceOnly"])
        self.assertEqual(set(result["order"]), {"instId", "tdMode", "side", "posSide", "ordType", "sz", "reduceOnly"})

    def test_execution_is_disabled_by_default(self):
        class Fake:
            def get_position(self, instrument): raise AssertionError("must not query or place when disabled")
            def close_position(self, order): raise AssertionError("must not place when disabled")
            def get_order(self, instrument, order_id): raise AssertionError("must not query when disabled")
        with tempfile.TemporaryDirectory() as tmp:
            result = ActiveCloseAdapter(Fake(), Path(tmp) / "audit.json").execute(valid_decision())
        self.assertEqual(result["status"], "DISABLED")

    def test_idempotency_deduplicates_decision(self):
        class Fake:
            def get_position(self, instrument): return POSITION
            def close_position(self, order): return {"ordId": "o1", "state": "filled"}
            def get_order(self, instrument, order_id): return {"state": "filled", "accFillSz": "0.26"}
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ActiveCloseAdapter(Fake(), Path(tmp) / "audit.json", enabled=True)
            decision = valid_decision()
            first = adapter.execute(decision)
            second = adapter.execute(decision)
        self.assertEqual(first["status"], "CLOSED")
        self.assertEqual(second["status"], "DUPLICATE")

    def test_partial_fill_trips_failure_without_retrying_open(self):
        class Fake:
            def get_position(self, instrument): return POSITION
            def close_position(self, order): return {"ordId": "o1", "state": "partially_filled"}
            def get_order(self, instrument, order_id): return {"state": "partially_filled", "accFillSz": "0.1"}
        with tempfile.TemporaryDirectory() as tmp:
            result = ActiveCloseAdapter(Fake(), Path(tmp) / "audit.json", enabled=True).execute(
                valid_decision())
        self.assertEqual(result["status"], "CIRCUIT_OPEN")

    def test_successful_close_with_audit_write_failure_is_unknown_and_never_retried(self):
        class Fake:
            def __init__(self):
                self.close_calls = 0
                self.order_calls = 0
            def get_position(self, instrument):
                return POSITION
            def close_position(self, order):
                self.close_calls += 1
                return {"ordId": "o1"}
            def get_order(self, instrument, order_id):
                self.order_calls += 1
                return {"state": "filled", "accFillSz": "0.26"}

        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake()
            adapter = ActiveCloseAdapter(fake, Path(tmp) / "audit.json", enabled=True)
            original_write = adapter._write
            writes = 0

            def fail_first_write(state):
                nonlocal writes
                writes += 1
                if writes == 1:
                    raise OSError("disk full")
                original_write(state)

            adapter._write = fail_first_write
            first = adapter.execute(valid_decision())
            second = adapter.execute(valid_decision())
            restarted = ActiveCloseAdapter(fake, Path(tmp) / "audit.json", enabled=True).execute(valid_decision())

        self.assertIn(first["status"], {"PERSISTENCE_FAILURE", "POST_EXECUTION_STATE_UNKNOWN"})
        self.assertEqual(second["status"], "CIRCUIT_OPEN")
        self.assertEqual(restarted["status"], "CIRCUIT_OPEN")
        self.assertEqual(fake.close_calls, 1)
        self.assertEqual(fake.order_calls, 1)


if __name__ == "__main__":
    unittest.main()
