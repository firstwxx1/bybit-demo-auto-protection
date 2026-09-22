"""Bybit Demo active-close transport and fail-closed execution adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from bybit_adapter import BybitDemoClient, BybitError
from decision_layer import Action, RULE_VERSION, decision_id_for, evaluate_decision


class CloseError(RuntimeError):
    pass


class CloseTransport(Protocol):
    def get_position(self, instrument: str, position_idx: int | None = None) -> dict[str, Any] | None: ...
    def close_position(self, position: dict[str, Any], quantity: float | str | None = None) -> dict[str, Any]: ...
    def get_order(self, instrument: str, order_id: str) -> dict[str, Any]: ...


class BybitCloseTransport:
    """Adapter over the already host-locked Bybit V5 client."""

    def __init__(self, api_key: str, secret: str, base_url: str, *, timeout: float = 20) -> None:
        try:
            self.client = BybitDemoClient(api_key, secret, base_url, timeout=timeout)
        except BybitError as exc:
            raise CloseError(str(exc)) from exc

    def get_position(self, instrument: str, position_idx: int | None = None) -> dict[str, Any] | None:
        try:
            return self.client.get_position(instrument, position_idx=position_idx)
        except BybitError as exc:
            raise CloseError(str(exc)) from exc

    def close_position(self, position: dict[str, Any], quantity: float | str | None = None) -> dict[str, Any]:
        try:
            return self.client.create_close_order(position, quantity)
        except BybitError as exc:
            raise CloseError(str(exc)) from exc

    def get_order(self, instrument: str, order_id: str) -> dict[str, Any]:
        try:
            return self.client.get_order(instrument, order_id)
        except BybitError as exc:
            raise CloseError(str(exc)) from exc


# Backwards-friendly alias used by callers that only need to identify Demo.
DemoCloseTransport = BybitCloseTransport


class ActiveCloseAdapter:
    def __init__(self, transport: CloseTransport, audit_path: Path, *, enabled: bool = False, failure_limit: int = 3):
        self.transport = transport
        self.audit_path = Path(audit_path)
        self.enabled = bool(enabled)
        self.failure_limit = max(1, int(failure_limit))
        self._post_execution_state_unknown = False

    @property
    def _intent_path(self) -> Path:
        return self.audit_path.with_suffix(self.audit_path.suffix + ".intent")

    def _write(self, state: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.audit_path.with_suffix(self.audit_path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.audit_path)

    def _write_intent(self, decision_id: str, order: dict[str, Any]) -> None:
        self._intent_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._intent_path.with_suffix(self._intent_path.suffix + ".tmp")
        temporary.write_text(json.dumps({"decision_id": decision_id, "order": order}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self._intent_path)

    def _clear_intent(self) -> None:
        try:
            self._intent_path.unlink()
        except FileNotFoundError:
            pass

    def _read(self) -> tuple[dict[str, Any], bool]:
        if not self.audit_path.exists():
            return {"failures": 0, "circuit_open": False, "decisions": {}}, False
        try:
            payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state must be object")
            payload.setdefault("failures", 0)
            payload.setdefault("circuit_open", False)
            payload.setdefault("decisions", {})
            return payload, False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"failures": self.failure_limit, "circuit_open": True, "decisions": {}}, True

    @staticmethod
    def _text(value: Any) -> str:
        return str(value).strip().lower()

    def _validate_decision(self, decision: dict[str, Any]) -> None:
        if not isinstance(decision, dict):
            raise CloseError("decision must be an object")
        if decision.get("rule_version") != RULE_VERSION:
            raise CloseError("decision rule version invalid")
        if decision.get("decision_id") != decision_id_for(decision):
            raise CloseError("decision id mismatch")
        if decision.get("action") != Action.CLOSE_POSITION.value:
            return
        position = decision.get("position")
        if not isinstance(position, dict):
            raise CloseError("decision position missing")
        instrument = str(position.get("instrument", ""))
        if not instrument.endswith("USDT"):
            raise CloseError("only Bybit linear positions can be closed")
        side = str(position.get("side", "")).lower()
        if side not in {"long", "short"}:
            raise CloseError("position side invalid")
        try:
            size = float(position.get("size"))
        except (TypeError, ValueError) as exc:
            raise CloseError("position size invalid") from exc
        if not size > 0:
            raise CloseError("position size invalid")
        if position.get("position_idx") not in (0, 1, 2):
            raise CloseError("positionIdx missing or invalid")
        if not isinstance(decision.get("evidence_window"), dict) or not isinstance(decision.get("evidence_snapshot"), dict):
            raise CloseError("decision evidence missing")
        checked = evaluate_decision(
            position,
            decision["evidence_snapshot"].get("grok", {}),
            decision["evidence_snapshot"].get("models", []),
            candles=decision["evidence_snapshot"].get("candles", []),
            now=None,
            evidence_snapshot=decision["evidence_snapshot"],
        )
        if checked["action"] != decision["action"] or checked["reason"] != decision.get("reason"):
            raise CloseError("decision did not pass deterministic gate")

    def _persist_failure(self, state: dict[str, Any], decision_id: str, result: dict[str, Any], *, count: bool = True) -> dict[str, Any]:
        if count:
            state["failures"] = int(state.get("failures", 0)) + 1
            state["circuit_open"] = state["failures"] >= self.failure_limit
        state.setdefault("decisions", {})[decision_id or "INVALID"] = result
        self._write(state)
        return result

    def execute(self, decision: dict[str, Any]) -> dict[str, Any]:
        if self._post_execution_state_unknown:
            did = decision.get("decision_id", "") if isinstance(decision, dict) else ""
            return {"status": "CIRCUIT_OPEN", "decision_id": did, "error": "POST_EXECUTION_STATE_UNKNOWN"}
        state, corrupt = self._read()
        did = decision.get("decision_id", "") if isinstance(decision, dict) else ""
        if corrupt:
            try:
                self._write(state)
            except OSError:
                pass
            return {"status": "CIRCUIT_OPEN", "decision_id": did, "error": "STATE_UNREADABLE"}
        try:
            self._validate_decision(decision)
        except CloseError as exc:
            try:
                return self._persist_failure(state, str(did), {"status": "REJECTED", "decision_id": did, "error": str(exc)}, count=False)
            except OSError:
                return {"status": "REJECTED", "decision_id": did, "error": str(exc)}
        if did in state.get("decisions", {}):
            return {"status": "DUPLICATE", "decision_id": did}
        if not self.enabled:
            return {"status": "DISABLED", "decision_id": did}
        if state.get("circuit_open"):
            return {"status": "CIRCUIT_OPEN", "decision_id": did}
        if decision.get("action") != Action.CLOSE_POSITION.value:
            return {"status": decision.get("action", "UNKNOWN"), "decision_id": did}

        expected = decision["position"]
        instrument = str(expected["instrument"])
        side_effect_attempted = False
        try:
            actual = self.transport.get_position(instrument, position_idx=int(expected["position_idx"]))
            if not actual or str(actual.get("instrument")) != instrument:
                raise CloseError("position missing or instrument mismatch")
            if str(actual.get("side", "")).lower() != str(expected.get("side", "")).lower():
                raise CloseError("position direction changed")
            if abs(float(actual.get("size", 0)) - float(expected["size"])) > 1e-12:
                raise CloseError("position size changed")
            if int(actual.get("position_idx", -1)) != int(expected["position_idx"]):
                raise CloseError("positionIdx changed")
            order = {
                "instrument": instrument,
                "position_idx": int(expected["position_idx"]),
                "side": expected["side"],
                "size": expected["size"],
                "reduceOnly": True,
                "closeOnTrigger": True,
                "orderType": "Market",
            }
            self._write_intent(did, order)
            side_effect_attempted = True
            placed = self.transport.close_position(actual, float(expected["size"]))
            oid = str(placed.get("orderId") or placed.get("orderLinkId") or "")
            if not oid:
                raise CloseError("order id missing")
            status = self.transport.get_order(instrument, oid)
            order_status = self._text(status.get("orderStatus"))
            filled = float(status.get("cumExecQty") or status.get("cumExecValue", 0) or 0)
            if order_status != "filled" or abs(filled - float(expected["size"])) > 1e-12:
                raise CloseError("partial or failed close; circuit opened")
            result = {"status": "CLOSED", "decision_id": did, "order": order, "orderId": oid}
            state["failures"] = 0
            state["circuit_open"] = False
        except Exception as exc:
            state["failures"] = int(state.get("failures", 0)) + 1
            state["circuit_open"] = state["failures"] >= self.failure_limit or "partial" in str(exc).lower()
            result = {
                "status": "CIRCUIT_OPEN" if state["circuit_open"] else "ERROR",
                "decision_id": did,
                "error": type(exc).__name__,
            }
        state.setdefault("decisions", {})[did] = result
        try:
            self._write(state)
        except Exception:
            if side_effect_attempted:
                self._post_execution_state_unknown = True
                return {"status": "POST_EXECUTION_STATE_UNKNOWN", "decision_id": did, "error": "AUDIT_PERSISTENCE_FAILURE"}
            raise
        if result.get("status") == "CLOSED":
            self._clear_intent()
        return result


__all__ = ["ActiveCloseAdapter", "BybitCloseTransport", "CloseError", "CloseTransport", "DemoCloseTransport"]
