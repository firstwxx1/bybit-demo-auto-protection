"""Deterministic news severity classification and its effect on the close gate."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from decision_layer import evaluate_decision
from bybit_adapter import normalize_position
from model_clients import _classify_severity, _normalize_severity, SEVERITY_LEVELS


def _position() -> dict:
    return normalize_position({
        "symbol": "ETHUSDT",
        "side": "Sell",
        "size": "1",
        "avgPrice": "3000",
        "markPrice": "2990",
        "liqPrice": "3300",
        "leverage": "10",
        "positionIdx": 0,
    })


def _snapshot(severity: str) -> tuple[dict, dict]:
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    candles = [{"ts": stamp, "open": 3000, "high": 3010, "low": 2980, "close": 2990, "volume": 100}]
    news = {
        "available": True,
        "source": "publisher-rss",
        "model": "deterministic",
        "evidence_verified": True,
        "as_of": stamp,
        "summary": "rss",
        "items": [{
            "headline": "ETH event",
            "published_at": stamp,
            "source_url": "https://example.invalid/a",
            "severity": severity,
        }],
    }
    models = [{
        "source": source,
        "model": "gpt-5.6-sol",
        "available": True,
        "recommendation": "CLOSE_POSITION",
        "confidence": 0.95,
        "as_of": stamp,
    } for source in ("gpt-5.6-sol", "independent-risk")]
    snapshot = {"captured_at": stamp, "grok": news, "models": models, "candles": candles}
    return news, snapshot


class SeverityClassifierTests(unittest.TestCase):
    def test_levels_are_bounded(self):
        self.assertEqual(SEVERITY_LEVELS, ("critical", "high", "medium", "low"))

    def test_material_events_are_high_or_critical(self):
        self.assertEqual(_classify_severity("Binance hot wallet hacked, funds drained"), "critical")
        self.assertEqual(_classify_severity("SEC sues major crypto exchange"), "high")
        self.assertEqual(_classify_severity("交易所被黑，资金被盗"), "critical")
        self.assertEqual(_classify_severity("主流交易所遭起诉"), "high")

    def test_benign_headlines_stay_low(self):
        self.assertEqual(_classify_severity("Analyst shares 2026 price outlook"), "low")
        self.assertEqual(_classify_severity("Weekly market recap"), "low")

    def test_normalize_keeps_valid_and_derives_otherwise(self):
        self.assertEqual(_normalize_severity("HIGH", "nothing"), "high")
        self.assertEqual(_normalize_severity("bogus", "ETH plunges 20%"), "high")
        self.assertEqual(_normalize_severity(None, "routine update"), "low")


class CloseGateSeverityTests(unittest.TestCase):
    def test_high_severity_allows_close(self):
        news, snapshot = _snapshot("high")
        decision = evaluate_decision(_position(), news, snapshot["models"],
                                     candles=snapshot["candles"], evidence_snapshot=snapshot, now=None)
        self.assertEqual(decision["action"], "CLOSE_POSITION")
        self.assertEqual(decision["reason"], "CONSENSUS_VALIDATED")

    def test_low_severity_downgrades_to_hold(self):
        news, snapshot = _snapshot("low")
        decision = evaluate_decision(_position(), news, snapshot["models"],
                                     candles=snapshot["candles"], evidence_snapshot=snapshot, now=None)
        self.assertEqual(decision["action"], "HOLD")
        self.assertEqual(decision["reason"], "NEWS_NOT_MATERIAL")


if __name__ == "__main__":
    unittest.main()
