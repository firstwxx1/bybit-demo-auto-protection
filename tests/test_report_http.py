from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from report_http_service import response_payload


class ReportHttpSafetyTests(unittest.TestCase):
    def test_active_close_request_stays_dry_run_when_switch_is_disabled(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"status":"DRY_RUN"}', "stderr": ""})()
        with patch.dict(os.environ, {
            "ACTIVE_CLOSE_EXECUTION_ENABLED": "false",
            "OKX_TRADING_MODE": "demo",
        }, clear=False), patch("report_http_service.subprocess.run", return_value=completed) as run:
            result = response_payload({
                "paper_only": True,
                "trading_mode": "demo",
                "request_active_close": True,
            })
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(run.call_args.args[0][-1], "--active-close-execution")
        self.assertEqual(run.call_args.kwargs["env"]["ACTIVE_CLOSE_EXECUTION_ENABLED"], "false")

    def test_non_demo_active_close_request_is_blocked_before_process(self):
        with patch.dict(os.environ, {
            "ACTIVE_CLOSE_EXECUTION_ENABLED": "true",
            "OKX_TRADING_MODE": "live",
        }, clear=False):
            result = response_payload({
                "paper_only": True,
                "trading_mode": "demo",
                "request_active_close": True,
            })
        self.assertEqual(result["exitCode"], 1)
        self.assertIn("demo", result["stderr"])
