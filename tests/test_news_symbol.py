from __future__ import annotations

import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
