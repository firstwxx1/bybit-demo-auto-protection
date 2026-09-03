"""Strict, simulated-only adapter for intentional closing of an existing position."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from decision_layer import Action, RULE_VERSION, decision_id_for, evaluate_decision


class CloseError(RuntimeError):
    pass


class CloseTransport(Protocol):
    def get_position(self, instrument: str) -> dict[str, Any] | None: ...
    def close_position(self, order: dict[str, Any]) -> dict[str, Any]: ...
    def get_order(self, instrument: str, order_id: str) -> dict[str, Any]: ...


class DemoCloseTransport:
    BASE = "https://www.okx.com"
    ALLOWED = {("GET", "/api/v5/account/positions"), ("POST", "/api/v5/trade/order"),
               ("GET", "/api/v5/trade/order")}

    def __init__(self, base_url: str, api_key: str, secret: str, passphrase: str, *, timeout: float = 20):
        if base_url.rstrip("/") != self.BASE or not all((api_key, secret, passphrase)):
            raise CloseError("simulated OKX credentials/host required")
        self.api_key, self.secret, self.passphrase, self.timeout = api_key, secret, passphrase, timeout

    def _build_request(self, method: str, path: str, data: Any = None) -> Request:
        method = method.upper()
        if (method, path) not in self.ALLOWED:
            raise CloseError("request is outside active-close allowlist")
        query, body = "", b""
        if method == "GET" and isinstance(data, dict) and data:
            query = "?" + urlencode(data)
        elif method == "POST":
            body = json.dumps(data, separators=(",", ":")).encode()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        signature = base64.b64encode(hmac.new(self.secret.encode(), (timestamp + method + path + query).encode() + body, hashlib.sha256).digest()).decode()
        return Request(self.BASE + path + query, data=body or None, method=method, headers={
            "OK-ACCESS-KEY": self.api_key, "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp, "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json", "x-simulated-trading": "1",
        })

    def _request(self, method: str, path: str, data: Any = None) -> dict[str, Any]:
        with urlopen(self._build_request(method, path, data), timeout=self.timeout) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise CloseError("OKX simulated request failed")
        return payload

    def get_position(self, instrument: str) -> dict[str, Any] | None:
        rows = self._request("GET", "/api/v5/account/positions", {"instType": "SWAP", "instId": instrument}).get("data", [])
        return next((row for row in rows if float(row.get("pos") or 0) != 0), None)

    def close_position(self, order: dict[str, Any]) -> dict[str, Any]:
        return (self._request("POST", "/api/v5/trade/order", order).get("data") or [{}])[0]

    def get_order(self, instrument: str, order_id: str) -> dict[str, Any]:
        rows = self._request("GET", "/api/v5/trade/order", {"instId": instrument, "ordId": order_id}).get("data", [])
        if not rows:
            raise CloseError("order status missing")
        return rows[0]


class ActiveCloseAdapter:
    def __init__(self, transport: CloseTransport, audit_path: Path, *, enabled: bool = False, failure_limit: int = 3):
        self.transport, self.audit_path, self.enabled, self.failure_limit = transport, audit_path, enabled, failure_limit
        self._post_execution_state_unknown = False

    @property
    def _intent_path(self) -> Path:
        return self.audit_path.with_suffix(self.audit_path.suffix + ".intent")

    def _write(self, state: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.audit_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.audit_path)

    def _write_intent(self, decision_id: str, order: dict[str, Any]) -> None:
        self._intent_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._intent_path.with_suffix(self._intent_path.suffix + ".tmp")
        temp.write_text(json.dumps({"decision_id": decision_id, "order": order}, sort_keys=True), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self._intent_path)

    def _clear_intent(self) -> None:
        try:
            self._intent_path.unlink()
        except FileNotFoundError:
            pass

    def _read(self) -> tuple[dict[str, Any], bool]:
        if self._intent_path.exists():
            return {"decisions": {}, "failures": self.failure_limit, "circuit_open": True}, True
        if not self.audit_path.exists():
            return {"decisions": {}, "failures": 0, "circuit_open": False}, False
        try:
            state = json.loads(self.audit_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("decisions", {}), dict):
                raise ValueError("invalid state")
            return state, False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"decisions": {}, "failures": self.failure_limit, "circuit_open": True}, True

    @staticmethod
    def _validate_decision(decision: Any) -> None:
        if not isinstance(decision, dict):
            raise CloseError("decision must be an object")
        did = decision.get("decision_id")
        if not isinstance(did, str) or len(did) != 64 or any(c not in "0123456789abcdef" for c in did):
            raise CloseError("decision_id invalid")
        if decision.get("rule_version") != RULE_VERSION:
            raise CloseError("rule version invalid")
        if decision.get("action") not in {item.value for item in Action}:
            raise CloseError("action invalid")
        required = ("position", "created_at", "evidence_window", "evidence_snapshot", "evidence_hash")
        if any(field not in decision for field in required) or not isinstance(decision["evidence_snapshot"], dict):
            raise CloseError("evidence snapshot incomplete")
        snapshot = decision["evidence_snapshot"]
        if any(field not in snapshot for field in ("position", "candles", "grok", "models", "captured_at")):
            raise CloseError("evidence snapshot incomplete")
        if snapshot.get("position") != decision.get("position"):
            raise CloseError("evidence position mismatch")
        expected_hash = hashlib.sha256(json.dumps(decision["evidence_snapshot"], sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str).encode()).hexdigest()
        if decision.get("evidence_hash") != expected_hash or decision_id_for(decision) != did:
            raise CloseError("decision integrity invalid")
        if decision.get("action") == Action.CLOSE_POSITION.value and decision.get("reason") != "CONSENSUS_VALIDATED":
            raise CloseError("close action is not deterministic")
        try:
            boundary = datetime.fromisoformat(str(decision["created_at"]).replace("Z", "+00:00"))
            window = int(decision["evidence_window"]["max_age_seconds"])
            checked = evaluate_decision(decision["position"], snapshot["grok"], snapshot["models"],
                                        now=boundary, max_age_seconds=window, candles=snapshot["candles"],
                                        evidence_snapshot=snapshot)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise CloseError("decision time boundary invalid")
        if checked["action"] != decision["action"] or checked["reason"] != decision.get("reason"):
            raise CloseError("decision did not pass deterministic gate")

    def _persist_failure(self, state: dict[str, Any], did: str, result: dict[str, Any], *, count: bool = True) -> dict[str, Any]:
        if count:
            state["failures"] = int(state.get("failures", 0)) + 1
            state["circuit_open"] = state["failures"] >= self.failure_limit
        state.setdefault("decisions", {})[did or "INVALID"] = result
        self._write(state)
        return result

    def execute(self, decision: dict[str, Any]) -> dict[str, Any]:
        if self._post_execution_state_unknown:
            did = decision.get("decision_id", "") if isinstance(decision, dict) else ""
            return {"status": "CIRCUIT_OPEN", "decision_id": did, "error": "POST_EXECUTION_STATE_UNKNOWN"}
        state, corrupt = self._read()
        did = decision.get("decision_id", "") if isinstance(decision, dict) else ""
        if corrupt:
            result = {"status": "CIRCUIT_OPEN", "decision_id": did, "error": "STATE_UNREADABLE"}
            try:
                self._write(state)
            except OSError:
                pass
            return result
        try:
            self._validate_decision(decision)
        except CloseError as exc:
            return self._persist_failure(state, str(did), {"status": "REJECTED", "decision_id": did, "error": str(exc)}, count=False)
        if did in state.get("decisions", {}):
            return {"status": "DUPLICATE", "decision_id": did}
        if not self.enabled:
            return {"status": "DISABLED", "decision_id": did}
        if state.get("circuit_open"):
            return {"status": "CIRCUIT_OPEN", "decision_id": did}
        if decision["action"] != Action.CLOSE_POSITION.value:
            return {"status": decision["action"], "decision_id": did}
        expected = decision["position"]; instrument = str(expected["instrument"])
        side_effect_attempted = False
        try:
            actual = self.transport.get_position(instrument)
            if not actual or str(actual.get("instId", actual.get("instrument"))) != instrument:
                raise CloseError("position missing or instrument mismatch")
            actual_side = actual.get("side") or actual.get("posSide")
            actual_size = float(actual.get("size", actual.get("pos", 0)))
            if actual_side != expected.get("side") or abs(abs(actual_size) - float(expected["size"])) > 1e-12:
                raise CloseError("position direction or size changed")
            margin = actual.get("margin_mode", actual.get("mgnMode")); pside = actual.get("position_side", actual.get("posSide"))
            if margin != expected.get("margin_mode") or pside != expected.get("position_side"):
                raise CloseError("position mode changed")
            order = {"instId": instrument, "tdMode": margin, "side": "buy" if expected["side"] == "short" else "sell",
                     "posSide": pside, "ordType": "market", "sz": str(expected["size"]), "reduceOnly": True}
            self._write_intent(did, order)
            side_effect_attempted = True
            placed = self.transport.close_position(order); oid = str(placed.get("ordId", ""))
            if not oid:
                raise CloseError("order id missing")
            status = self.transport.get_order(instrument, oid)
            filled = float(status.get("accFillSz", 0) or 0)
            state_name = str(status.get("state", "")).lower()
            if state_name != "filled" or abs(filled - float(expected["size"])) > 1e-12:
                raise CloseError("partial or failed close; circuit opened")
            result = {"status": "CLOSED", "decision_id": did, "order": order, "ordId": oid}
            state["failures"] = 0
        except Exception as exc:
            state["failures"] = int(state.get("failures", 0)) + 1
            state["circuit_open"] = state["failures"] >= self.failure_limit
            result = {"status": "CIRCUIT_OPEN" if state["circuit_open"] or "partial" in str(exc).lower() else "ERROR", "decision_id": did, "error": type(exc).__name__}
        state.setdefault("decisions", {})[did] = result
        try:
            self._write(state)
        except Exception:
            if side_effect_attempted:
                self._post_execution_state_unknown = True
                return {"status": "POST_EXECUTION_STATE_UNKNOWN", "decision_id": did,
                        "error": "AUDIT_PERSISTENCE_FAILURE"}
            raise
        if result.get("status") == "CLOSED":
            self._clear_intent()
        return result
