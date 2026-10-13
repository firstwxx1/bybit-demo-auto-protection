"""Fail-closed gate for the Bybit Demo dynamic TP/SL execution path."""
from __future__ import annotations

import unittest

from auto_runner import env_enabled, protection_gate


class ProtectionGateTests(unittest.TestCase):
    def test_report_only_when_disabled(self):
        self.assertEqual(
            protection_gate(enabled=False, fixture_mode=False, cached_at=None, demo_host_ok=True,
                            active_close_enabled=False, has_candidates=True),
            ("DISABLED", None),
        )

    def test_fixture_never_executes(self):
        status, _ = protection_gate(enabled=True, fixture_mode=True, cached_at=None, demo_host_ok=True,
                                    active_close_enabled=False, has_candidates=True)
        self.assertEqual(status, "FIXTURE_EXECUTION_BLOCKED")

    def test_cached_positions_block(self):
        status, _ = protection_gate(enabled=True, fixture_mode=False, cached_at="2026-10-13T00:00:00+00:00",
                                    demo_host_ok=True, active_close_enabled=False, has_candidates=True)
        self.assertEqual(status, "STALE_POSITION_BLOCKED")

    def test_non_demo_host_blocks(self):
        status, reason = protection_gate(enabled=True, fixture_mode=False, cached_at=None, demo_host_ok=False,
                                         active_close_enabled=False, has_candidates=True)
        self.assertEqual((status, reason), ("BLOCKED", "Bybit Demo host and trading mode are required"))

    def test_active_close_isolation_blocks_before_execution(self):
        status, reason = protection_gate(enabled=True, fixture_mode=False, cached_at=None, demo_host_ok=True,
                                         active_close_enabled=True, has_candidates=True)
        self.assertEqual(status, "BLOCKED")
        self.assertIn("isolated", reason or "")

    def test_missing_candidate_blocks(self):
        status, _ = protection_gate(enabled=True, fixture_mode=False, cached_at=None, demo_host_ok=True,
                                    active_close_enabled=False, has_candidates=False)
        self.assertEqual(status, "NO_VALID_CANDIDATE")

    def test_only_the_full_happy_path_executes(self):
        self.assertEqual(
            protection_gate(enabled=True, fixture_mode=False, cached_at=None, demo_host_ok=True,
                            active_close_enabled=False, has_candidates=True),
            ("EXECUTE", None),
        )

    def test_active_close_flag_beats_happy_path(self):
        """The mutex must win even when every other precondition is satisfied."""
        status, _ = protection_gate(enabled=True, fixture_mode=False, cached_at=None, demo_host_ok=True,
                                    active_close_enabled=True, has_candidates=True)
        self.assertNotEqual(status, "EXECUTE")


class EnvEnabledTests(unittest.TestCase):
    def test_truthy_values(self):
        for value in ("1", "true", "TRUE", "yes", "on", " true "):
            self.assertTrue(env_enabled(value))

    def test_falsy_values(self):
        for value in (None, "", "0", "false", "no", "off", "enabled"):
            self.assertFalse(env_enabled(value))


if __name__ == "__main__":
    unittest.main()
