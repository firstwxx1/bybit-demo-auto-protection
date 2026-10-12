from __future__ import annotations

import os
import unittest

import decision_layer
import model_clients
from model_clients import _base_symbol, risk_analysis


class BaseSymbolTests(unittest.TestCase):
    def test_derives_base_asset(self):
        self.assertEqual(_base_symbol("ETHUSDT"), "eth")
        self.assertEqual(_base_symbol("ETH-USDT"), "eth")
        self.assertEqual(_base_symbol("BTCUSDC"), "btc")
        self.assertEqual(_base_symbol("SOL/USDT"), "sol")
        self.assertEqual(_base_symbol("1000PEPEUSDT"), "1000pepe")

    def test_non_quote_instrument_is_unchanged(self):
        self.assertEqual(_base_symbol("ETH"), "eth")


class RiskModelIdentityTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("RISK_MODEL", "RISK_MODEL_API_KEY", "RISK_MODEL_API_BASE")}
        os.environ["RISK_MODEL_API_KEY"] = "test-key"
        os.environ["RISK_MODEL_API_BASE"] = "https://example.invalid/v1"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_model_field_reports_configured_model(self):
        os.environ["RISK_MODEL"] = "global:gpt-6-sol"
        original = model_clients._chat
        model_clients._chat = lambda *a, **k: (
            '{"risk_level":"LOW","confidence":0.9,"recommendation":"HOLD",'
            '"take_profit":null,"stop_loss":null}'
        )
        try:
            result = risk_analysis({}, {}, {})
        finally:
            model_clients._chat = original
        self.assertIs(result["available"], True)
        self.assertEqual(result["model"], "global:gpt-6-sol")
        self.assertEqual(result["source"], model_clients.RISK_SOURCE_ID)


class GrokRssFallbackTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("GROK_API_KEY", "GROK_API_BASE", "GROK_MODEL", "NEWS_RSS_ENABLED")}
        os.environ.update({"GROK_API_KEY": "k", "GROK_API_BASE": "https://example.invalid/v1",
                           "GROK_MODEL": "global:grok-4.7", "NEWS_RSS_ENABLED": "true"})

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_prose_output_falls_back_to_publisher_rss(self):
        original_search = model_clients._grok_x_search
        original_rss = model_clients._rss_news
        model_clients._grok_x_search = lambda *a, **k: "I'll search X for recent posts."
        model_clients._rss_news = lambda inst: {
            "available": True, "source": "publisher-rss", "as_of": "2026-01-01T00:00:00+00:00",
            "items": [{"headline": "ETH news", "published_at": "2026-01-01T00:00:00+00:00",
                       "source_url": "https://example.invalid/eth"}],
            "evidence_verified": True,
        }
        try:
            result = model_clients.grok_news({"instrument": "ETHUSDT"})
        finally:
            model_clients._grok_x_search = original_search
            model_clients._rss_news = original_rss
        self.assertEqual(result["source"], "publisher-rss")
        self.assertIs(result["evidence_verified"], True)


class NewsMaxAgeWindowTests(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("NEWS_MAX_AGE_SECONDS")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("NEWS_MAX_AGE_SECONDS", None)
        else:
            os.environ["NEWS_MAX_AGE_SECONDS"] = self._orig

    def test_env_window_is_honored(self):
        os.environ["NEWS_MAX_AGE_SECONDS"] = "86400"
        self.assertEqual(decision_layer._configured_max_age_seconds(), 86400)

    def test_default_window_used_when_unset(self):
        os.environ.pop("NEWS_MAX_AGE_SECONDS", None)
        self.assertEqual(decision_layer._configured_max_age_seconds(),
                         decision_layer.DEFAULT_NEWS_MAX_AGE_SECONDS)


if __name__ == "__main__":
    unittest.main()
