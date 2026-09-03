from __future__ import annotations

import tempfile
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from decision_layer import Action
from live_reporter import run_active_close_cycle
import live_reporter


POSITION = {
    "instrument": "ETH-USDT-SWAP", "side": "short", "size": 0.26,
    "leverage": 10, "entry_price": 1877.53, "mark_price": 1938.31,
    "unrealized_pnl": -1.58, "liquidation_price": 2059.56,
    "margin_mode": "cross", "position_side": "short",
}


def now_iso(minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def news() -> dict[str, object]:
    return {"available": True, "source": "grok", "evidence_verified": True, "as_of": now_iso(), "items": [
        {"id": "g1", "published_at": now_iso(), "headline": "Material event", "severity": "high",
         "source_url": "https://example.com/material-event"},
    ]}


def sol(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {"available": True, "source": "gpt-5.6-sol", "model": "gpt-5.6-sol",
                                "as_of": now_iso(), "confidence": 0.95, "recommendation": "CLOSE_POSITION"}
    value.update(overrides)
    return value


def independent(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {"available": True, "source": "independent-risk", "model": "independent-risk-v1",
                                "as_of": now_iso(), "confidence": 0.95, "recommendation": "CLOSE_POSITION"}
    value.update(overrides)
    return value


CANDLES = [{"ts": now_iso(), "open": "1900", "high": "1950", "low": "1850", "close": "1938", "volume": "10"}]


class FakeCloseTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get_position(self, instrument: str) -> dict[str, object]:
        self.calls.append(("get_position", instrument))
        return {"instId": POSITION["instrument"], "pos": "-0.26", "posSide": "short", "mgnMode": "cross"}

    def close_position(self, order: dict[str, object]) -> dict[str, object]:
        self.calls.append(("close_position", order))
        assert order["ordType"] == "market"
        assert order["reduceOnly"] is True
        assert set(order) == {"instId", "tdMode", "side", "posSide", "ordType", "sz", "reduceOnly"}
        return {"ordId": "close-1"}

    def get_order(self, instrument: str, order_id: str) -> dict[str, object]:
        self.calls.append(("get_order", order_id))
        return {"state": "filled", "accFillSz": "0.26"}


def run(*, enabled: bool = False, second=independent, transport=None):
    return run_active_close_cycle(
        POSITION, candles=CANDLES, active_close_execution_enabled=enabled, transport=transport,
        audit_path=Path(tempfile.gettempdir()) / "active-close-orchestration-test.json",
        news_client=lambda _: news(), sol_risk_client=lambda _p, _f, _n: sol(),
        second_risk_client=lambda _p, _f, _n: second(),
    )


def test_consistent_grok_sol_and_independent_evidence_can_close() -> None:
    result = run()
    assert result["decision"]["action"] == Action.CLOSE_POSITION.value
    assert result["execution"]["status"] == "DRY_RUN"


def test_missing_conflicting_stale_or_low_confidence_source_never_closes() -> None:
    cases = [
        lambda: {"available": False, "source": "independent-risk"},
        lambda: independent(recommendation="HOLD"),
        lambda: independent(as_of=now_iso(60)),
        lambda: independent(confidence=0.2),
    ]
    for second in cases:
        result = run(second=second)
        assert result["decision"]["action"] in {Action.HOLD.value, Action.MANUAL_REVIEW_REQUIRED.value}
        assert result["decision"]["action"] != Action.CLOSE_POSITION.value


def test_default_active_close_switch_does_not_call_transport() -> None:
    transport = FakeCloseTransport()
    result = run(enabled=False, transport=transport)
    assert result["execution"]["status"] == "DRY_RUN"
    assert transport.calls == []


def test_enabled_fake_transport_only_receives_reduce_only_market_close() -> None:
    transport = FakeCloseTransport()
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(live_reporter, "okx_get", return_value={"data": [{
            "instId": POSITION["instrument"], "pos": "-0.26", "posSide": "short", "lever": "10",
            "avgPx": "1877.53", "markPx": "1938.31", "upl": "-1.58", "liqPx": "2059.56", "mgnMode": "cross",
        }]}):
            positions, cached_at, receipt = live_reporter.fetch_positions(Path(directory) / "positions.json", include_receipt=True)
        result = run_active_close_cycle(
            positions[0], candles=CANDLES, active_close_execution_enabled=True, transport=transport,
            position_source="realtime",
            cached_at=cached_at, realtime_receipt=receipt,
            audit_path=Path(directory) / "audit.json", news_client=lambda _: news(),
            sol_risk_client=lambda _p, _f, _n: sol(), second_risk_client=lambda _p, _f, _n: independent(),
        )
    assert result["execution"]["status"] == "CLOSED"
    assert [name for name, _ in transport.calls] == ["get_position", "close_position", "get_order"]


def test_mutated_position_with_valid_receipt_fails_closed() -> None:
    transport = FakeCloseTransport()
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(live_reporter, "okx_get", return_value={"data": [{
            "instId": POSITION["instrument"], "pos": "-0.26", "posSide": "short", "lever": "10",
            "avgPx": "1877.53", "markPx": "1938.31", "upl": "-1.58", "liqPx": "2059.56", "mgnMode": "cross",
        }]}):
            positions, cached_at, receipt = live_reporter.fetch_positions(Path(directory) / "positions.json", include_receipt=True)
        positions[0]["size"] = 999
        result = run_active_close_cycle(
            positions[0], candles=CANDLES, active_close_execution_enabled=True, transport=transport,
            position_source="realtime", cached_at=cached_at, realtime_receipt=receipt,
            audit_path=Path(directory) / "audit.json", news_client=lambda _: news(),
            sol_risk_client=lambda _p, _f, _n: sol(), second_risk_client=lambda _p, _f, _n: independent(),
        )
    assert result["execution"]["status"] == "POSITION_SNAPSHOT_MISMATCH"
    assert transport.calls == []


def test_enabled_direct_caller_cannot_forge_realtime_position_source() -> None:
    transport = FakeCloseTransport()
    result = run_active_close_cycle(
        POSITION, candles=CANDLES, enabled=True, transport=transport,
        position_source="realtime", cached_at=None,
        audit_path=Path(tempfile.gettempdir()) / "active-close-unverified-source-test.json",
        news_client=lambda _: news(), sol_risk_client=lambda _p, _f, _n: sol(),
        second_risk_client=lambda _p, _f, _n: independent(),
    )
    assert (result["execution"]["status"], transport.calls) == ("POSITION_SOURCE_UNVERIFIED", [])


def test_direct_receipt_factory_cannot_authorize_forged_position() -> None:
    transport = FakeCloseTransport()
    receipt = live_reporter._issue_realtime_receipt([POSITION])
    result = run_active_close_cycle(
        POSITION, candles=CANDLES, enabled=True, transport=transport,
        cached_at=None, realtime_receipt=receipt,
        audit_path=Path(tempfile.gettempdir()) / "active-close-forged-receipt-test.json",
        news_client=lambda _: news(), sol_risk_client=lambda _p, _f, _n: sol(),
        second_risk_client=lambda _p, _f, _n: independent(),
    )
    assert result["execution"]["status"] == "POSITION_SOURCE_UNVERIFIED"
    assert transport.calls == []


def test_mutable_receipt_globals_cannot_authorize_directly_forged_receipt() -> None:
    transport = FakeCloseTransport()
    receipt = live_reporter._issue_realtime_receipt([POSITION])
    live_reporter._REALTIME_RECEIPTS.add(receipt._token)
    live_reporter._CURRENT_REALTIME_RECEIPT_TOKEN = receipt._token
    result = run_active_close_cycle(
        POSITION, candles=CANDLES, enabled=True, transport=transport,
        cached_at=None, realtime_receipt=receipt,
        audit_path=Path(tempfile.gettempdir()) / "active-close-mutated-global-test.json",
        news_client=lambda _: news(), sol_risk_client=lambda _p, _f, _n: sol(),
        second_risk_client=lambda _p, _f, _n: independent(),
    )
    assert (result["execution"]["status"], transport.calls) == ("POSITION_SOURCE_UNVERIFIED", [])


def test_cached_position_blocks_execution_even_when_enabled() -> None:
    transport = FakeCloseTransport()
    result = run_active_close_cycle(
        POSITION, candles=CANDLES, active_close_execution_enabled=True, transport=transport,
        position_source="cache", cached_at=now_iso(),
        audit_path=Path(tempfile.gettempdir()) / "active-close-stale-test.json",
        news_client=lambda _: news(), sol_risk_client=lambda _p, _f, _n: sol(),
        second_risk_client=lambda _p, _f, _n: independent(),
    )
    assert result["decision"]["action"] == Action.CLOSE_POSITION.value
    assert result["execution"]["status"] == "STALE_POSITION_BLOCKED"
    assert transport.calls == []


def test_main_never_constructs_transport_for_cached_positions(monkeypatch) -> None:
    monkeypatch.setattr(live_reporter, "fetch_positions", lambda _cache: ([POSITION], now_iso()))
    monkeypatch.setattr(live_reporter, "fetch_candles", lambda _instrument: CANDLES)
    monkeypatch.setattr(live_reporter, "build_report", lambda _snapshot: "report")
    monkeypatch.setattr(live_reporter, "run_active_close_cycle", lambda *args, **kwargs: {
        "snapshot": {}, "decision": {"decision_id": "d", "rule_version": "v", "action": "HOLD", "reason": "r"},
        "execution": {"status": "STALE_POSITION_BLOCKED"},
    })
    with patch.object(live_reporter, "DemoCloseTransport", side_effect=AssertionError("must not construct")), \
         patch.object(sys, "argv", ["live_reporter.py", "--active-close-execution"]), \
         patch.dict(live_reporter.os.environ, {"ACTIVE_CLOSE_EXECUTION_ENABLED": "true"}):
        live_reporter.main()
