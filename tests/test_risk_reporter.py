from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import live_reporter
import model_clients
import terminal_menu
from okx_demo_risk_reporter import (
    Position, ValidationError, build_report, fib_targets, fixed_stop,
    liquidation_distance, position_from_snapshot, validate_stop,
)

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "examples/eth-short.json").read_text(encoding="utf-8"))


class CoreTests(unittest.TestCase):
    def test_protection_envelope_is_structured_and_fail_closed(self):
        report = build_report(FIXTURE)
        envelope = live_reporter.build_protection_envelope(
            report, "ETH-USDT-SWAP", "2026-08-27T03:25:17+00:00", "realtime"
        )
        self.assertTrue(envelope["paper_only"])
        self.assertEqual(envelope["report_version"], "risk-report-v1")
        self.assertEqual(envelope["candidates"]["ETH-USDT-SWAP"]["take_profit"], 1877.5281)

    def test_protection_envelope_rejects_cached_position(self):
        with self.assertRaises(ValueError):
            live_reporter.build_protection_envelope(
                build_report(FIXTURE), "ETH-USDT-SWAP", "2026-08-27T03:25:17+00:00", "cache"
            )

    def test_position_parses_actual_instrument(self):
        self.assertEqual(position_from_snapshot(FIXTURE["position"]).instrument, "ETH-USDT-SWAP")

    def test_rejects_non_swap(self):
        raw = FIXTURE["position"] | {"instrument": "ETH-USDT"}
        with self.assertRaises(ValidationError): position_from_snapshot(raw)

    def test_rejects_invalid_side(self):
        with self.assertRaises(ValidationError): position_from_snapshot(FIXTURE["position"] | {"side": "net"})

    def test_rejects_zero_size(self):
        with self.assertRaises(ValidationError): position_from_snapshot(FIXTURE["position"] | {"size": 0})

    def test_historical_fixed_stop(self):
        pos = position_from_snapshot(FIXTURE["position"])
        self.assertEqual(fixed_stop(pos, 0.04855847842644323), 1968.7)

    def test_long_fixed_stop_below_entry(self):
        pos = position_from_snapshot(FIXTURE["position"] | {"side": "long"})
        self.assertLess(fixed_stop(pos, 0.05), pos.entry_price)

    def test_rejects_excessive_stop_pct(self):
        with self.assertRaises(ValidationError): fixed_stop(position_from_snapshot(FIXTURE["position"]), 0.3)

    def test_short_stop_direction(self):
        pos = position_from_snapshot(FIXTURE["position"])
        self.assertTrue(validate_stop(pos, 1968.7)[0])
        self.assertFalse(validate_stop(pos, 1900)[0])

    def test_long_stop_direction(self):
        pos = position_from_snapshot(FIXTURE["position"] | {"side": "long", "liquidation_price": 1600})
        self.assertTrue(validate_stop(pos, 1800)[0])
        self.assertFalse(validate_stop(pos, 2000)[0])

    def test_short_stop_cannot_cross_liquidation(self):
        pos = position_from_snapshot(FIXTURE["position"])
        self.assertFalse(validate_stop(pos, 2100)[0])

    def test_report_clamps_short_stop_below_liquidation_boundary(self):
        report = build_report(FIXTURE | {"position": FIXTURE["position"] | {
            "entry_price": 77832.8,
            "mark_price": 77285.2,
            "liquidation_price": 81404.3,
        }})
        self.assertIn("止损候选价：81612.2", report)
        self.assertIn("安全修正止损价：78444.5", report)
        self.assertIn("止损保护建议：78444.5", report)

    def test_historical_fib_values_and_invalidity(self):
        fib = fib_targets(position_from_snapshot(FIXTURE["position"]), FIXTURE["candles"])
        self.assertEqual(fib["fib_1272"], 1830.4437)
        self.assertEqual(fib["fib_1618"], 1817.0154)
        self.assertFalse(fib["valid"])

    def test_short_fib_valid_inside_structure(self):
        raw = FIXTURE["position"] | {"mark_price": 1860}
        self.assertTrue(fib_targets(position_from_snapshot(raw), FIXTURE["candles"])["valid"])

    def test_long_fib_invalid_below_structure(self):
        raw = FIXTURE["position"] | {"side": "long", "mark_price": 1830, "liquidation_price": 1600}
        self.assertFalse(fib_targets(position_from_snapshot(raw), FIXTURE["candles"])["valid"])

    def test_missing_candles_never_fabricates_targets(self):
        fib = fib_targets(position_from_snapshot(FIXTURE["position"]), [])
        self.assertFalse(fib["valid"])
        self.assertNotIn("fib_1272", fib)

    def test_fib_targets_ignore_candles_older_than_local_window(self):
        recent = [
            {"high": 101 + index, "low": 99 + index, "close": 100 + index}
            for index in range(20)
        ]
        old_outlier = {"high": 200, "low": 20, "close": 100}
        raw = FIXTURE["position"] | {
            "side": "long",
            "entry_price": 100,
            "mark_price": 110,
            "liquidation_price": 80,
        }

        fib = fib_targets(position_from_snapshot(raw), recent + [old_outlier])

        self.assertEqual(fib["high"], 120.0)
        self.assertEqual(fib["low"], 99.0)
        self.assertEqual(fib["window_candles"], 20)

    def test_liquidation_distance(self):
        distance = liquidation_distance(position_from_snapshot(FIXTURE["position"]))
        self.assertAlmostEqual(distance, 6.255432215855583)

    def test_report_matches_historical_safety_facts(self):
        report = build_report(FIXTURE)
        for expected in ["ETH-USDT-SWAP", "止损保护建议：1967.38", "Fib 1.272：1830.4437",
                         "Fib 1.618：1817.0154", "风险收益比（当前标记价基准）：2.0905", "不执行交易"]:
            self.assertIn(expected, report)

    def test_model_failure_keeps_fixed_stop(self):
        report = build_report(FIXTURE | {"risk_model": {"available": False, "error": "超时"}})
        self.assertIn("模型不可用（固定风控运行中）", report)
        self.assertIn("止损保护建议：1967.38", report)

    def test_profitable_short_uses_fib_retracement_to_lock_profit(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "entry_price": 2481.59,
                "mark_price": 2447.10,
                "liquidation_price": 2635.65,
                "unrealized_pnl": 18686.8,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "timeframe": "4H（局部摆动点）",
                "high": 2549.76,
                "low": 2351.0,
                "fib_1272": 2296.9373,
                "fib_1618": 2228.1663,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot)

        self.assertIn("安全修正止损价：2473.83", report)
        self.assertIn("Fib 0.618盈利回撤保护", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选（Fib回撤保护后）", report)
        self.assertIn("止盈建议：2420.3663；通过", report)
        self.assertNotIn("止盈建议：2373.687", report)

    def test_report_explains_profitable_short_stop_relative_to_entry(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "entry_price": 2481.59,
                "mark_price": 2464.90,
                "liquidation_price": 2635.65,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "high": 2549.76,
                "low": 2351.0,
                "fib_1272": 2296.9373,
                "fib_1618": 2228.1663,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot)

        self.assertIn("止损逻辑：空单盈利保护", report)
        self.assertIn("止损高于当前价8.93点", report)
        self.assertIn("止损低于开仓价7.76点", report)
        self.assertIn("触发后仍锁定约0.31%价格利润", report)
        self.assertIn("止损来源：固定程序兜底 + Fib 0.618盈利回撤保护", report)
        self.assertIn("**止盈建议：", report)
        self.assertIn("**安全修正止损价：", report)
        self.assertIn("**止损校验：通过", report)

    def test_missing_model_take_profit_uses_near_target_instead_of_fib(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "entry_price": 77832.8,
                "mark_price": 77717.2,
                "liquidation_price": 81419.7,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "timeframe": "4H（局部摆动点）",
                "high": 79898.0,
                "low": 62246.0,
                "fib_1272": 57444.656,
                "fib_1618": 51337.064,
            },
            "risk_model": {
                "available": True,
                "risk_level": "HIGH",
                "confidence": 0.86,
                "recommendation": "MANUAL_REVIEW_REQUIRED",
                "take_profit": None,
            },
        }

        report = build_report(snapshot)

        self.assertIn("止盈建议：76551.442；通过", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertNotIn("止盈建议：57444.656", report)

    def test_valid_gpt_take_profit_has_priority_over_fib_candidate(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "side": "long",
                "entry_price": 100.0,
                "mark_price": 101.0,
                "liquidation_price": 90.0,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 106.0,
                "fib_1618": 112.0,
            },
            "risk_model": {
                "available": True,
                "recommendation": "HOLD",
                "take_profit": 108.0,
            },
        }

        report = build_report(snapshot, stop_pct=0.02)

        self.assertIn("止盈建议：108.0；通过", report)
        self.assertIn("止盈来源：GPT仓位分析", report)
        self.assertNotIn("止盈建议：106.0", report)
        self.assertNotIn("止盈来源：固定程序Fib 1.272候选", report)

    def test_near_target_replaces_low_reward_fib_candidate(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "entry_price": 100.0,
                "mark_price": 99.0,
                "liquidation_price": 110.0,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 98.5,
                "fib_1618": 97.0,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("止盈建议：97.515；通过", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertNotIn("止盈建议：80.0", report)

    def test_fixed_fib_take_profit_too_far_from_mark_is_rejected(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "entry_price": 100.0,
                "mark_price": 100.0,
                "liquidation_price": 110.0,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 80.0,
                "fib_1618": 70.0,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot, stop_pct=0.02)

        self.assertIn("止盈建议：98.5；通过", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertNotIn("止盈建议：80.0", report)

    def test_report_caps_fixed_stop_at_one_point_five_percent_from_mark(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "entry_price": 100.0,
                "mark_price": 95.0,
                "liquidation_price": 110.0,
            },
            "risk_model": {"available": False},
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("安全修正止损价：96.425", report)
        self.assertIn("原候选距离现价超过1.5%", report)
        self.assertIn("止损保护建议：96.425", report)

    def test_twenty_x_risk_profile_tightens_stop_and_builds_near_target(self):
        candles = [
            {"high": 100.2, "low": 99.8, "close": 100.0},
            {"high": 100.1, "low": 99.9, "close": 100.0},
        ]
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "side": "short",
                "leverage": 20.0,
                "entry_price": 100.0,
                "mark_price": 100.0,
                "liquidation_price": 106.0,
            },
            "candles": candles,
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 90.0,
                "fib_1618": 85.0,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("安全修正止损价：101.25", report)
        self.assertIn("杠杆风险上限1.25%", report)
        self.assertIn("止盈建议：98.75；通过（固定程序ATR近端1R候选）", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertIn("波动率参考（ATR）：0.3%", report)
        self.assertIn("风险收益比（当前标记价基准）：1", report)

    def test_twenty_x_low_volatility_uses_one_r_near_target(self):
        candles = [
            {"high": 100.03, "low": 99.97, "close": 100.0},
            {"high": 100.02, "low": 99.98, "close": 100.0},
        ]
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "side": "long",
                "leverage": 20.0,
                "entry_price": 100.0,
                "mark_price": 100.0,
                "liquidation_price": 94.0,
            },
            "candles": candles,
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 110.0,
                "fib_1618": 115.0,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("安全修正止损价：98.75", report)
        self.assertIn("止盈建议：101.25；通过（固定程序ATR近端1R候选）", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertIn("风险收益比（当前标记价基准）：1", report)

    def test_twenty_x_mid_volatility_uses_one_point_two_five_r_near_target(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "side": "long",
                "leverage": 20.0,
                "entry_price": 2450.87,
                "mark_price": 2477.6,
                "liquidation_price": 2339.61,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 2632.9897,
                "fib_1618": 2709.6944,
            },
            "risk_model": {"available": False, "error": "TimeoutError"},
            "candles": [
                {"high": 2478.9745, "low": 2476.2255, "close": 2477.6},
                {"high": 2478.9745, "low": 2476.2255, "close": 2477.6},
            ],
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("建议：TimeoutError，保留固定止损保护；不采用GPT止盈建议，固定程序候选仅供人工参考", report)
        self.assertIn("止盈建议：2508.57；通过（固定程序ATR近端1R候选）", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertIn("风险收益比（当前标记价基准）：1", report)

    def test_high_leverage_unverified_news_blocks_remote_gpt_take_profit(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "side": "long",
                "leverage": 20.0,
                "entry_price": 2450.87,
                "mark_price": 2492.09,
                "liquidation_price": 2339.61,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 2632.9897,
                "fib_1618": 2709.6944,
            },
            "risk_model": {"available": True, "take_profit": 2632.9897},
            "grok": {"evidence_verified": False},
            "candles": [
                {"high": 2494.9615, "low": 2489.2185, "close": 2492.09},
                {"high": 2494.9615, "low": 2489.2185, "close": 2492.09},
            ],
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("止盈建议：2523.2411；通过（固定程序ATR近端1R候选）", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertNotIn("止盈建议：2632.9897", report)

    def test_unverified_news_clamps_high_leverage_short_term_target_to_one_r(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "side": "long",
                "leverage": 20.0,
                "entry_price": 2450.87,
                "mark_price": 2495.18,
                "liquidation_price": 2339.61,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 2632.9897,
                "fib_1618": 2709.6944,
            },
            "risk_model": {"available": True, "take_profit": 2632.9897},
            "grok": {"evidence_verified": False},
            "candles": [
                {"high": 2497.459, "low": 2492.901, "close": 2495.18},
                {"high": 2497.459, "low": 2492.901, "close": 2495.18},
            ],
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("止盈建议：2526.3698；通过（固定程序ATR近端1R候选）", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)

    def test_twenty_x_long_uses_nearby_two_r_target_before_remote_fib(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "instrument": "ETH-USDT-SWAP",
                "side": "long",
                "leverage": 20.0,
                "entry_price": 2450.87,
                "mark_price": 2456.59,
                "liquidation_price": 2338.86,
            },
            "candles": [
                {"high": 2462.0, "low": 2452.0, "close": 2456.0},
                {"high": 2461.0, "low": 2452.0, "close": 2455.0},
            ],
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 2640.3337,
                "fib_1618": 2726.3804,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("止盈建议：2487.2974；通过（固定程序ATR近端1R候选）", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertIn("风险收益比（当前标记价基准）：1", report)

    def test_report_distinguishes_mark_and_entry_risk_reward_bases(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "instrument": "ETH-USDT-SWAP",
                "side": "long",
                "leverage": 20.0,
                "entry_price": 2450.87,
                "mark_price": 2465.16,
                "liquidation_price": 2339.11,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 2635.7151,
                "fib_1618": 2715.8868,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot, stop_pct=0.05)

        self.assertIn("止盈建议：2495.9745；通过（固定程序ATR近端1R候选）", report)
        self.assertIn("风险收益比（当前标记价基准）：1", report)
        self.assertIn("风险收益比（开仓均价基准）：2.7296", report)

    def test_long_missing_model_take_profit_uses_near_target_instead_of_fib(self):
        snapshot = FIXTURE | {
            "position": FIXTURE["position"] | {
                "side": "long",
                "entry_price": 100.0,
                "mark_price": 101.0,
                "liquidation_price": 90.0,
            },
            "fib": {
                "valid": True,
                "reason": "结构有效",
                "fib_1272": 106.0,
                "fib_1618": 112.0,
            },
            "risk_model": {"available": True, "take_profit": None},
        }

        report = build_report(snapshot, stop_pct=0.02)

        self.assertIn("止盈建议：102.515；通过", report)
        self.assertIn("止盈来源：固定程序ATR近端1R候选", report)
        self.assertIn("止盈校验：通过", report)

    def test_grok_news_retries_with_configured_fallback_model_after_503(self):
        with patch.dict(os.environ, {
            "GROK_API_KEY": "x",
            "GROK_API_BASE": "https://example.invalid/v1",
            "GROK_MODEL": "grok-4.5",
            "GROK_FALLBACK_MODEL": "grok-4.6",
        }), patch("model_clients._chat", side_effect=[HTTPError("https://example.invalid", 503, "unavailable", {}, None), json.dumps({
            "as_of": datetime.now(timezone.utc).isoformat(),
            "summary": "ETH短线波动扩大",
            "items": [{"headline": "ETH short-term volatility expands", "published_at": datetime.now(timezone.utc).isoformat(), "source_url": "https://example.invalid/news"}],
        })]) as chat:
            result = model_clients.grok_news({"instrument": "ETH-USDT-SWAP"})

        self.assertTrue(result["available"])
        self.assertTrue(result["evidence_verified"])
        self.assertEqual(result["model"], "grok-4.6")
        self.assertEqual(chat.call_count, 2)

    def test_model_http_failure_reports_status_without_body(self):
        error = HTTPError("https://example.invalid", 403, "secret-response", {}, None)
        with patch("model_clients._chat", side_effect=error):
            with patch.dict(os.environ, {"GROK_API_KEY": "x", "GROK_API_BASE": "https://example.invalid"}):
                result = model_clients.grok_news(FIXTURE["position"])
        self.assertEqual(result["summary"], "Grok不可用：HTTP 403")
        self.assertNotIn("secret-response", result["summary"])

    def test_unstructured_grok_text_is_not_execution_evidence(self):
        with patch("model_clients._chat", return_value="未经来源核验的市场摘要"):
            with patch.dict(os.environ, {"GROK_API_KEY": "x"}):
                result = model_clients.grok_news(FIXTURE["position"])
        self.assertTrue(result["available"])
        self.assertEqual(result["items"], [])
        self.assertFalse(result["evidence_verified"])

    def test_grok_future_dated_items_are_not_verified(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        payload = {"as_of": datetime.now(timezone.utc).isoformat(), "summary": "future",
                   "items": [{"headline": "future event", "published_at": future,
                              "source_url": "https://example.invalid/future"}]}
        with patch("model_clients._chat", return_value=json.dumps(payload)):
            with patch.dict(os.environ, {"GROK_API_KEY": "x"}):
                result = model_clients.grok_news(FIXTURE["position"])
        self.assertFalse(result["evidence_verified"])
        self.assertIn("时间校验", result["summary"])

    def test_grok_stale_items_are_not_verified(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        payload = {"as_of": datetime.now(timezone.utc).isoformat(), "summary": "stale",
                   "items": [{"headline": "stale event", "published_at": stale,
                              "source_url": "https://example.invalid/stale"}]}
        with patch("model_clients._chat", return_value=json.dumps(payload)):
            with patch.dict(os.environ, {"GROK_API_KEY": "x", "NEWS_MAX_AGE_SECONDS": "86400"}):
                result = model_clients.grok_news(FIXTURE["position"])
        self.assertFalse(result["evidence_verified"])
        self.assertIn("过期", result["summary"])

    def test_model_timeout_is_reported_without_being_misclassified(self):
        with patch("model_clients._chat", side_effect=TimeoutError()):
            with patch.dict(os.environ, {"RISK_MODEL_API_KEY": "x", "RISK_MODEL_API_BASE": "https://example.invalid/v1"}):
                result = model_clients.risk_analysis(FIXTURE["position"], {}, {})
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "GPT不可用：TimeoutError")

    def test_report_does_not_display_unverified_news_summary(self):
        data = FIXTURE | {"grok": {"available": True, "summary": "未来日期伪造新闻",
                                   "evidence_verified": False, "items": []}}
        report = build_report(data)
        self.assertIn("新闻证据未通过校验", report)
        self.assertNotIn("未来日期伪造新闻", report)

    def test_report_computes_risk_reward_instead_of_leaving_placeholder(self):
        data = FIXTURE | {
            "position": FIXTURE["position"] | {"mark_price": 1860, "liquidation_price": 2100},
            "risk_model": {"available": True, "risk_level": "MEDIUM", "confidence": 0.8,
                           "recommendation": "HOLD", "take_profit": 1830.4437},
        }
        report = build_report(data)
        self.assertNotIn("风险收益比：待确定性校验", report)
        self.assertIn("止盈建议：1830.4437；通过", report)
        self.assertIn("风险收益比（当前标记价基准）：5.9295", report)

    def test_report_separates_model_advice_from_deterministic_decision(self):
        data = FIXTURE | {
            "risk_model": {"available": True, "risk_level": "MEDIUM", "confidence": 0.8,
                           "recommendation": "HOLD", "take_profit": None},
            "decision": {"action": "MANUAL_REVIEW_REQUIRED", "reason": "MODEL_SOURCE_CONFLICT"},
        }
        report = build_report(data)
        self.assertIn("GPT建议：HOLD", report)
        self.assertIn("确定性决策：MANUAL_REVIEW_REQUIRED", report)
        self.assertIn("决策原因：MODEL_SOURCE_CONFLICT", report)

    def test_live_snapshot_uses_separate_4h_fib_candles(self):
        market = FIXTURE["candles"][:2]
        fib_candles = FIXTURE["candles"]
        snapshot, _ = live_reporter.build_decision_snapshot(
            FIXTURE["position"], candles=market, fib_candles=fib_candles,
            news_client=lambda p: {"available": False, "summary": "none"},
            risk_client=lambda p, f, n: {"available": False},
        )
        self.assertEqual(snapshot["candles"], market)
        self.assertEqual(snapshot["fib_candles"], fib_candles)
        self.assertEqual(snapshot["fib_timeframe"], "4H（局部摆动点）")

    def test_live_snapshot_exposes_paper_only_fib_close_candidate(self):
        position = FIXTURE["position"] | {"mark_price": 1810, "liquidation_price": 2059.56}
        snapshot, _ = live_reporter.build_decision_snapshot(
            position, candles=FIXTURE["candles"][:2], fib_candles=FIXTURE["candles"],
            news_client=lambda p: {"available": False, "summary": "none"},
            risk_client=lambda p, f, n: {"available": False},
            second_risk_client=lambda p, f, n: {"available": False},
        )
        candidate = snapshot["fib_close_candidate"]
        self.assertEqual(candidate["status"], "FIB_CLOSE_CANDIDATE")
        self.assertEqual(candidate["stage"], "fib_1272")
        self.assertEqual(candidate["execution"], "PAPER_ONLY")
        self.assertIn("Fib浮动平仓候选：FIB_CLOSE_CANDIDATE", build_report(snapshot))

    def test_invalid_fib_rejects_model_take_profit(self):
        data = FIXTURE | {"risk_model": {"available": True, "take_profit": 1800, "risk_level": "高"}}
        self.assertIn("止盈建议：暂不采用", build_report(data))

    def test_dynamic_non_btc_instrument(self):
        data = FIXTURE | {"position": FIXTURE["position"] | {"instrument": "SOL-USDT-SWAP"}}
        report = build_report(data)
        self.assertIn("SOL-USDT-SWAP", report)
        self.assertNotIn("BTC-USDT-SWAP", report)

    def test_swap_size_is_explicitly_contracts(self):
        report = build_report(FIXTURE)
        self.assertIn("数量=0.26张（OKX合约张数，非BTC数量）", report)

    def test_stop_report_explains_candidate_and_liquidation_bound(self):
        data = FIXTURE | {"position": FIXTURE["position"] | {"liquidation_price": 1900}}
        report = build_report(data)
        self.assertIn("止损候选价：1968.7", report)
        self.assertIn("止损允许上限：1898.1", report)
        self.assertIn("止损保护告警：空单止损不得越过强平价", report)

    def test_invalid_stop_is_safely_clamped_before_risk_reward(self):
        data = FIXTURE | {
            "position": FIXTURE["position"] | {"liquidation_price": 1900, "mark_price": 1860},
            "risk_model": {"available": True, "risk_level": "MEDIUM", "confidence": 0.8,
                           "recommendation": "HOLD", "take_profit": 1830.4437},
        }
        report = build_report(data)
        self.assertIn("安全修正止损价：1864.98", report)
        self.assertIn("止损校验：通过", report)
        self.assertIn("风险收益比（当前标记价基准）：5.9295", report)

    def test_cached_report_marks_staleness(self):
        report = build_report(FIXTURE | {"cached_at": "2026-07-21T08:50:00Z"})
        self.assertIn("可能过期", report)


class CollectorTests(unittest.TestCase):
    def test_allowlist_blocks_trade_endpoint_before_network(self):
        with self.assertRaises(ValidationError): live_reporter.okx_get("/api/v5/trade/order")

    def test_normalize_short_position(self):
        raw = {"instId": "ETH-USDT-SWAP", "pos": "-0.26", "posSide": "short", "lever": "10",
               "avgPx": "1877.53", "markPx": "1938.31", "upl": "-1.58", "liqPx": "2059.5"}
        result = live_reporter.normalize_position(raw)
        self.assertEqual(result["side"], "short")
        self.assertEqual(result["size"], 0.26)

    def test_cache_roundtrip_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            live_reporter.write_cache(path, [FIXTURE["position"]])
            self.assertEqual(live_reporter.read_cache(path)["positions"][0]["instrument"], "ETH-USDT-SWAP")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stale_cache_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            path.write_text(json.dumps({"cached_at": stale, "positions": []}), encoding="utf-8")
            with patch.dict(os.environ, {"CACHE_MAX_AGE_SECONDS": "60"}):
                with self.assertRaises(RuntimeError): live_reporter.read_cache(path)

    def test_live_failure_uses_recent_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            live_reporter.write_cache(path, [FIXTURE["position"]])
            with patch("live_reporter.okx_get", side_effect=OSError("offline")):
                positions, cached_at = live_reporter.fetch_positions(path)
            self.assertEqual(positions[0]["instrument"], "ETH-USDT-SWAP")
            self.assertIsNotNone(cached_at)

    def test_success_replaces_cache(self):
        row = {"instId": "SOL-USDT-SWAP", "pos": "1", "posSide": "long", "lever": "3",
               "avgPx": "100", "markPx": "101", "upl": "1", "liqPx": "70"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            with patch("live_reporter.okx_get", return_value={"data": [row]}):
                positions, cached_at = live_reporter.fetch_positions(path)
            self.assertEqual(positions[0]["instrument"], "SOL-USDT-SWAP")
            self.assertIsNone(cached_at)


class ArtifactSafetyTests(unittest.TestCase):
    def test_n8n_is_inactive_and_30_minutes(self):
        workflow = json.loads((ROOT / "n8n/okx-demo-risk-report.template.json").read_text())
        self.assertFalse(workflow["active"])
        self.assertEqual(workflow["nodes"][0]["parameters"]["rule"]["interval"][0]["minutesInterval"], 30)

    def test_n8n_contains_no_trade_endpoint(self):
        text = (ROOT / "n8n/okx-demo-risk-report.template.json").read_text()
        self.assertNotIn("/api/v5/trade/", text)
        self.assertNotIn("/api/v5/account/set-leverage", text)

    def test_reconstructed_telegram_escapes_html_entities(self):
        workflow = json.loads(
            (ROOT / "n8n/okx-demo-risk-report.15-node-reconstructed.json").read_text()
        )
        guard = next(node for node in workflow["nodes"] if node["id"] == "14-output-guard")
        telegram = next(node for node in workflow["nodes"] if node["id"] == "15-telegram")
        code = guard["parameters"]["jsCode"]
        self.assertIn("&amp;", code)
        self.assertIn("&lt;", code)
        self.assertIn("&gt;", code)
        telegram_text = telegram["parameters"]["text"]
        self.assertIn("$json.telegram_text", telegram_text)
        self.assertIn("$json.protection_request.candidates", telegram_text)
        self.assertEqual(
            telegram["parameters"]["additionalFields"]["parse_mode"], "HTML"
        )

    def test_no_literal_secret_in_tracked_artifacts(self):
        candidates = [
            path for path in ROOT.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.name != "test_risk_reporter.py"
        ]
        text = "\n".join(path.read_text(errors="ignore") for path in candidates)
        self.assertNotIn("Bearer ey", text)
        self.assertNotIn('"api_key": "[', text)
        self.assertNotIn('"secret": "[', text)


class TerminalMenuTests(unittest.TestCase):
    def test_render_menu_explains_read_only_boundary(self):
        menu = terminal_menu.render_menu()
        self.assertIn("API配置", menu)
        self.assertIn("只读", menu)
        self.assertIn("不执行交易", menu)

    def test_render_menu_exposes_protection_controls(self):
        menu = terminal_menu.render_menu()
        for text in ("保护执行", "保护对账", "单周期", "模拟盘"):
            self.assertIn(text, menu)

    def test_protection_defaults_are_disabled(self):
        values = terminal_menu.load_env(Path("/nonexistent/.env"))
        self.assertEqual(values.get("PROTECTION_EXECUTION_ENABLED", "false"), "false")
        self.assertEqual(values.get("ACTIVE_CLOSE_EXECUTION_ENABLED", "false"), "false")

    def test_active_close_toggle_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            terminal_menu.save_env(path, {"PROTECTION_EXECUTION_ENABLED": "true", "ACTIVE_CLOSE_EXECUTION_ENABLED": "false"})
            terminal_menu.set_active_close_enabled(path, True)
            values = terminal_menu.load_env(path)
            self.assertEqual(values["ACTIVE_CLOSE_EXECUTION_ENABLED"], "true")
            self.assertEqual(values["PROTECTION_EXECUTION_ENABLED"], "true")

    def test_set_protection_enabled_updates_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            terminal_menu.save_env(path, {"PROTECTION_EXECUTION_ENABLED": "false"})
            terminal_menu.set_protection_enabled(path, True)
            self.assertEqual(terminal_menu.load_env(path)["PROTECTION_EXECUTION_ENABLED"], "true")

    def test_menu_enable_requires_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            terminal_menu.save_env(path, {"PROTECTION_EXECUTION_ENABLED": "false"})
            terminal_menu._set_protection(path, lambda _: "yes")
            self.assertEqual(terminal_menu.load_env(path)["PROTECTION_EXECUTION_ENABLED"], "false")
            terminal_menu._set_protection(path, lambda _: "ENABLE")
            self.assertEqual(terminal_menu.load_env(path)["PROTECTION_EXECUTION_ENABLED"], "true")

    def test_save_env_writes_private_file_without_printing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            terminal_menu.save_env(path, {"OKX_DEMO_API_KEY": "key-value", "OKX_DEMO_API_SECRET": "secret-value"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertIn("OKX_DEMO_API_KEY=key-value", text)
            self.assertIn("OKX_DEMO_API_SECRET=secret-value", text)

    def test_config_status_reports_missing_required_values(self):
        status = terminal_menu.config_status({"OKX_DEMO_API_KEY": "", "OKX_DEMO_API_SECRET": "x"})
        self.assertFalse(status["complete"])
        self.assertIn("OKX_DEMO_API_KEY", status["missing"])

    def test_credential_fields_cover_models_and_telegram(self):
        self.assertIn("GROK_API_KEY", terminal_menu.CREDENTIAL_KEYS)
        self.assertIn("RISK_MODEL_API_KEY", terminal_menu.CREDENTIAL_KEYS)
        self.assertIn("TELEGRAM_BOT_TOKEN", terminal_menu.CREDENTIAL_KEYS)
        self.assertIn("TELEGRAM_CHAT_ID", terminal_menu.CREDENTIAL_KEYS)

    def test_dependency_status_uses_current_python(self):
        status = terminal_menu.dependency_status()
        self.assertTrue(status["python"])
        self.assertIn("pytest", status)


if __name__ == "__main__":
    unittest.main()
