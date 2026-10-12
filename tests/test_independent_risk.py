from __future__ import annotations

import os
import unittest

import model_clients


KEYS = ("INDEPENDENT_RISK_API_BASE", "INDEPENDENT_RISK_API_KEY", "INDEPENDENT_RISK_MODEL",
        "RISK_MODEL_API_BASE", "RISK_MODEL_API_KEY")


class IndependentRiskTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _configure(self, base="https://independent.example.invalid/v1", key="ind-key",
                   model="glm-5.3-workbuddy", main_base="https://main.example.invalid/v1",
                   main_key="main-key"):
        os.environ["INDEPENDENT_RISK_API_BASE"] = base
        os.environ["INDEPENDENT_RISK_API_KEY"] = key
        os.environ["INDEPENDENT_RISK_MODEL"] = model
        if main_base is not None:
            os.environ["RISK_MODEL_API_BASE"] = main_base
        if main_key is not None:
            os.environ["RISK_MODEL_API_KEY"] = main_key

    def test_unconfigured_stays_unavailable_and_fail_closed(self):
        result = model_clients.independent_risk_analysis({}, {}, {})
        self.assertIs(result["available"], False)
        self.assertEqual(result["source"], "independent-risk")

    def test_configured_source_reports_own_model_and_source(self):
        self._configure()
        original = model_clients._chat
        model_clients._chat = lambda *a, **k: (
            '{"risk_level":"LOW","confidence":0.8,"recommendation":"HOLD",'
            '"take_profit":null,"stop_loss":null}'
        )
        try:
            result = model_clients.independent_risk_analysis({"instrument": "ETHUSDT"}, {}, {})
        finally:
            model_clients._chat = original
        self.assertIs(result["available"], True)
        self.assertEqual(result["source"], "independent-risk")
        self.assertEqual(result["model"], "glm-5.3-workbuddy")
        self.assertEqual(result["recommendation"], "HOLD")

    def test_shared_credential_is_refused(self):
        self._configure(key="same-key", main_key="same-key")
        result = model_clients.independent_risk_analysis({}, {}, {})
        self.assertIs(result["available"], False)
        self.assertIn("共用凭据", result["error"])

    def test_shared_endpoint_is_refused(self):
        self._configure(base="https://shared.example.invalid/v1",
                        main_base="https://shared.example.invalid/v1")
        result = model_clients.independent_risk_analysis({}, {}, {})
        self.assertIs(result["available"], False)
        self.assertIn("端点相同", result["error"])


if __name__ == "__main__":
    unittest.main()
