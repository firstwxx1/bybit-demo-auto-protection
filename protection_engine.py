"""Fail-closed OKX demo stop-protection reconciler.

This module never opens positions. It only maintains reduce-only conditional
stop orders for positions already returned by OKX demo account queries.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from okx_demo_risk_reporter import Position, fixed_stop, position_from_snapshot, validate_stop

OKX_BASE_URL = "https://www.okx.com"
_ALLOWED_REQUESTS = {
    ("GET", "/api/v5/trade/orders-algo-pending"),
    ("POST", "/api/v5/trade/order-algo"),
    ("POST", "/api/v5/trade/cancel-algos"),
}


class ProtectionError(RuntimeError):
    pass


FIB_CLOSE_FRACTIONS = {"fib_1272": 0.25, "fib_1618": 0.35}


def evaluate_fib_close_candidate(
    position: dict[str, Any], fib: dict[str, Any], state: dict[str, Any] | None = None,
    *, now: datetime | None = None, max_age_seconds: int = 900,
) -> dict[str, Any]:
    """Return a paper-only partial-close candidate from deterministic Fib levels.

    This function has no transport or filesystem side effects. The caller must
    persist a filled stage only after independently verifying execution.
    """
    state = state if isinstance(state, dict) else {}
    side = str(position.get("side", "")).lower()
    if side not in {"long", "short"}:
        return {"status": "FIB_CLOSE_BLOCKED", "reason": "POSITION_SIDE_INVALID", "execution": "PAPER_ONLY"}
    if fib.get("valid") is not True:
        return {"status": "FIB_CLOSE_BLOCKED", "reason": fib.get("reason", "FIB_INVALID"), "execution": "PAPER_ONLY"}
    now = now or datetime.now(timezone.utc)
    if fib.get("as_of") in (None, ""):
        return {"status": "FIB_CLOSE_BLOCKED", "reason": "FIB_EVIDENCE_TIME_MISSING", "execution": "PAPER_ONLY"}
    try:
        mark = float(position["mark_price"])
        size = float(position["size"])
        liq = float(position["liquidation_price"]) if position.get("liquidation_price") not in (None, "") else None
        levels = {name: float(fib[name]) for name in FIB_CLOSE_FRACTIONS}
        as_of = datetime.fromisoformat(str(fib["as_of"]).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        age = (now - as_of).total_seconds()
        if age > max_age_seconds or age < -30:
            return {"status": "FIB_CLOSE_BLOCKED", "reason": "FIB_EVIDENCE_STALE", "execution": "PAPER_ONLY"}
    except (KeyError, TypeError, ValueError):
        return {"status": "FIB_CLOSE_BLOCKED", "reason": "FIB_OR_POSITION_INVALID", "execution": "PAPER_ONLY"}
    if mark <= 0 or size <= 0 or any(level <= 0 for level in levels.values()):
        return {"status": "FIB_CLOSE_BLOCKED", "reason": "FIB_OR_POSITION_INVALID", "execution": "PAPER_ONLY"}
    if liq is not None and ((side == "short" and mark >= liq) or (side == "long" and mark <= liq)):
        return {"status": "FIB_CLOSE_BLOCKED", "reason": "LIQUIDATION_BOUNDARY_BREACHED", "execution": "PAPER_ONLY"}
    if (side == "short" and levels["fib_1272"] <= levels["fib_1618"]
            or side == "long" and levels["fib_1272"] >= levels["fib_1618"]):
        return {"status": "FIB_CLOSE_BLOCKED", "reason": "FIB_STRUCTURE_INVALID", "execution": "PAPER_ONLY"}
    ordered = ("fib_1272", "fib_1618")
    for stage in ordered:
        if state.get(f"{stage}_closed") is True:
            continue
        target = levels[stage]
        reached = mark <= target if side == "short" else mark >= target
        if not reached:
            return {"status": "NO_TRIGGER", "stage": stage, "target": target, "execution": "PAPER_ONLY"}
        fraction = FIB_CLOSE_FRACTIONS[stage]
        close_size = round(size * fraction, 8)
        if close_size <= 0 or close_size > size:
            return {"status": "FIB_CLOSE_BLOCKED", "reason": "CLOSE_SIZE_INVALID", "execution": "PAPER_ONLY"}
        return {"status": "FIB_CLOSE_CANDIDATE", "stage": stage, "target": target,
                "close_fraction": fraction, "close_size": close_size, "execution": "PAPER_ONLY"}
    return {"status": "NO_TRIGGER", "stage": "COMPLETE", "execution": "PAPER_ONLY"}


class Transport(Protocol):
    def get_pending(self, instrument: str) -> list[dict[str, Any]]: ...
    def place_stop(self, order: dict[str, str]) -> str: ...
    def cancel(self, instrument: str, algo_ids: list[str]) -> None: ...


def _decimal(value: float) -> str:
    if not math.isfinite(value) or value <= 0:
        raise ProtectionError("order numeric value must be finite and positive")
    return format(value, ".15g")


def build_stop_order(position: Position, stop: float) -> dict[str, str]:
    valid, reason = validate_stop(position, stop)
    if not valid:
        raise ProtectionError(reason)
    if position.margin_mode not in {"cross", "isolated"}:
        raise ProtectionError("actual position margin mode is required for execution")
    if position.position_side not in {"net", position.side}:
        raise ProtectionError("actual OKX position side is required for execution")
    return {
        "instId": position.instrument,
        "tdMode": position.margin_mode,
        "side": "buy" if position.side == "short" else "sell",
        "posSide": position.position_side,
        "ordType": "conditional",
        "sz": _decimal(position.size),
        "slTriggerPx": _decimal(stop),
        "slOrdPx": "-1",
        "reduceOnly": "true",
    }


def build_protection_order(position: Position, stop: float, take_profit: float) -> dict[str, str]:
    """Build one reduce-only OCO order carrying both TP and SL."""
    order = build_stop_order(position, stop)
    tp = _decimal(take_profit)
    if position.side == "short":
        valid = take_profit < min(position.mark_price, position.entry_price)
    else:
        valid = take_profit > max(position.mark_price, position.entry_price)
    if not valid:
        raise ProtectionError("take-profit direction is invalid")
    # OKX ignores TP fields on a plain conditional order. OCO is required
    # for a linked take-profit/stop-loss pair.
    order.update({"ordType": "oco", "tpTriggerPx": tp, "tpOrdPx": "-1"})
    return order


class DemoTransport:
    """Narrow OKX REST transport that is permanently pinned to demo trading."""

    def __init__(self, base_url: str, api_key: str, secret: str, passphrase: str, *, timeout: float = 20):
        if base_url.rstrip("/") != OKX_BASE_URL:
            raise ProtectionError("OKX demo transport host must be https://www.okx.com")
        if not all((api_key, secret, passphrase)):
            raise ProtectionError("OKX demo credentials are incomplete")
        self.base_url = OKX_BASE_URL
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.timeout = timeout

    def _build_request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> Request:
        method = method.upper()
        if (method, path) not in _ALLOWED_REQUESTS:
            raise ProtectionError("request is outside the stop-protection allowlist")
        query = ""
        body = b""
        if method == "GET" and isinstance(data, dict) and data:
            query = "?" + urlencode(data)
        elif method == "POST":
            body = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        prehash = f"{timestamp}{method}{path}{query}".encode("utf-8") + body
        signature = base64.b64encode(
            hmac.new(self.secret.encode("utf-8"), prehash, hashlib.sha256).digest()
        ).decode("ascii")
        return Request(
            self.base_url + path + query,
            data=body if method == "POST" else None,
            method=method,
            headers={
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
                "x-simulated-trading": "1",
                "User-Agent": "okx-demo-stop-protector/1.0",
            },
        )

    def _request(self, method: str, path: str, data: Any = None) -> dict[str, Any]:
        request = self._build_request(method, path, data)
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise ProtectionError(f"OKX demo request failed: {payload.get('msg', 'unknown')}")
        return payload

    def get_pending(self, instrument: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for order_type in ("conditional", "oco"):
            payload = self._request(
                "GET",
                "/api/v5/trade/orders-algo-pending",
                {"ordType": order_type, "instType": "SWAP", "instId": instrument},
            )
            batch = payload.get("data")
            if not isinstance(batch, list):
                raise ProtectionError("OKX pending-order response has invalid data")
            rows.extend(row for row in batch if isinstance(row, dict))
        return rows

    def place_stop(self, order: dict[str, str]) -> str:
        payload = self._request("POST", "/api/v5/trade/order-algo", order)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != 1 or not rows[0].get("algoId"):
            raise ProtectionError("OKX did not return an algoId")
        if str(rows[0].get("sCode", "0")) != "0":
            raise ProtectionError(f"OKX rejected stop order: {rows[0].get('sMsg', 'unknown')}")
        return str(rows[0]["algoId"])

    def cancel(self, instrument: str, algo_ids: list[str]) -> None:
        if not algo_ids:
            return
        request_rows = [{"instId": instrument, "algoId": algo_id} for algo_id in algo_ids]
        payload = self._request("POST", "/api/v5/trade/cancel-algos", request_rows)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != len(algo_ids):
            raise ProtectionError("OKX cancel response is incomplete")
        rejected = [row for row in rows if str(row.get("sCode", "0")) != "0"]
        if rejected:
            raise ProtectionError("OKX rejected one or more stop cancellations")


class CircuitBreaker:
    def __init__(self, path: Path, failure_limit: int = 3):
        if failure_limit < 1:
            raise ValueError("failure_limit must be positive")
        self.path = path
        self.failure_limit = failure_limit

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "consecutive_failures": 0,
                "circuit_open": False,
                "last_error": None,
                "managed_orders": {},
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProtectionError("circuit state is unreadable") from exc
        if not isinstance(payload, dict):
            raise ProtectionError("circuit state is invalid")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def record_failure(self, error: str) -> dict[str, Any]:
        state = self.read()
        failures = int(state.get("consecutive_failures", 0)) + 1
        state = {
            "consecutive_failures": failures,
            "circuit_open": failures >= self.failure_limit,
            "last_error": error[:500],
            "managed_orders": state.get("managed_orders", {}),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(state)
        return state

    def record_success(self, managed_orders: dict[str, str] | None = None) -> None:
        if managed_orders is None:
            managed_orders = self.read().get("managed_orders", {})
        self._write({
            "consecutive_failures": 0,
            "circuit_open": False,
            "last_error": None,
            "managed_orders": managed_orders,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })


class ProtectionEngine:
    def __init__(
        self,
        transport: Transport,
        state_path: Path,
        *,
        enabled: bool = False,
        stop_pct: float = 0.04855847842644323,
        take_profit_pct: float | None = None,
        failure_limit: int = 3,
    ):
        self.transport = transport
        self.enabled = enabled
        self.stop_pct = stop_pct
        self.take_profit_pct = take_profit_pct
        self.breaker = CircuitBreaker(state_path, failure_limit)

    @staticmethod
    def _matching(row: dict[str, Any], order: dict[str, str]) -> bool:
        # Keep compatibility with pre-OCO fixtures/orders that carried both
        # trigger fields. Real OKX combined protection must be OCO; a plain
        # conditional order with no TP fields must never match an OCO target.
        if order.get("ordType") == "oco" and row.get("ordType") == "conditional":
            if not row.get("tpTriggerPx") or not row.get("tpOrdPx"):
                return False
        fields = ["instId", "side", "posSide", "sz", "slTriggerPx", "slOrdPx", "reduceOnly"]
        if "ordType" in order:
            fields.append("ordType")
        if order.get("ordType") == "oco" and row.get("ordType") == "conditional":
            fields.remove("ordType")
        if "tpTriggerPx" in order:
            fields.extend(("tpTriggerPx", "tpOrdPx"))
        return all(str(row.get(field, "")) == order[field] for field in fields)

    @staticmethod
    def _owned_protection(row: dict[str, Any], order: dict[str, str]) -> bool:
        return (
            str(row.get("instId")) == order["instId"]
            and str(row.get("posSide")) == order["posSide"]
            and str(row.get("side")) == order["side"]
            and str(row.get("ordType")) in {"conditional", "oco"}
            and str(row.get("reduceOnly", "")).lower() == "true"
            and bool(row.get("algoId"))
            and bool(row.get("slTriggerPx"))
        )

    def reconcile(self, positions: list[dict[str, Any]], cached_at: str | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return [{"instrument": row.get("instrument"), "status": "DISABLED"} for row in positions]
        if cached_at:
            return [{"instrument": row.get("instrument"), "status": "STALE_POSITION_BLOCKED"} for row in positions]
        if self.breaker.read().get("circuit_open"):
            return [{"instrument": row.get("instrument"), "status": "CIRCUIT_OPEN"} for row in positions]

        state = self.breaker.read()
        managed_orders = dict(state.get("managed_orders", {}))
        results: list[dict[str, Any]] = []
        had_error = False
        active_instruments = {str(row.get("instrument", "")) for row in positions}
        for instrument, algo_id in list(managed_orders.items()):
            if instrument in active_instruments:
                continue
            try:
                self.transport.cancel(instrument, [str(algo_id)])
                managed_orders.pop(instrument, None)
                results.append({"instrument": instrument, "status": "CANCELLED_CLOSED_POSITION"})
            except (OSError, KeyError, TypeError, ValueError, ProtectionError) as exc:
                had_error = True
                self.breaker.record_failure(f"{instrument}: {type(exc).__name__}: {exc}")
                results.append({"instrument": instrument, "status": "ERROR", "error": type(exc).__name__})
                break
        if had_error:
            return results
        for snapshot in positions:
            instrument = str(snapshot.get("instrument", "unknown"))
            try:
                position = position_from_snapshot(snapshot)
                stop = fixed_stop(position, self.stop_pct)
                valid, reason = validate_stop(position, stop)
                if not valid:
                    results.append({
                        "instrument": position.instrument,
                        "status": "MANUAL_INTERVENTION_REQUIRED",
                        "reason": reason,
                    })
                    continue
                if self.take_profit_pct is None:
                    desired = build_stop_order(position, stop)
                else:
                    if not math.isfinite(self.take_profit_pct) or not 0 < self.take_profit_pct <= 0.5:
                        raise ProtectionError("take-profit ratio is outside safety bounds")
                    target = (
                        position.mark_price * (1 - self.take_profit_pct)
                        if position.side == "short"
                        else position.mark_price * (1 + self.take_profit_pct)
                    )
                    desired = build_protection_order(position, stop, target)
                pending = self.transport.get_pending(position.instrument)
                matches = [row for row in pending if self._matching(row, desired)]
                if matches:
                    managed_orders[position.instrument] = str(matches[0]["algoId"])
                    results.append({"instrument": position.instrument, "status": "PROTECTED", "algoId": matches[0]["algoId"]})
                    continue
                old_ids = [str(row["algoId"]) for row in pending if self._owned_protection(row, desired)]
                new_id = self.transport.place_stop(desired)
                managed_orders[position.instrument] = new_id
                self.breaker.record_success(managed_orders)
                if old_ids:
                    self.transport.cancel(position.instrument, old_ids)
                    status = "REPLACED"
                else:
                    status = "CREATED"
                results.append({"instrument": position.instrument, "status": status, "algoId": new_id, "stop": stop})
            except (OSError, KeyError, TypeError, ValueError, ProtectionError) as exc:
                had_error = True
                self.breaker.record_failure(f"{instrument}: {type(exc).__name__}: {exc}")
                results.append({"instrument": instrument, "status": "ERROR", "error": type(exc).__name__})
                break
        if not had_error:
            self.breaker.record_success(managed_orders)
        return results

    def reconcile_dynamic(self, positions: list[dict[str, Any]], candidates: dict[str, dict[str, Any]], *, cached_at: str | None = None) -> list[dict[str, Any]]:
        """Apply fresh, report-supplied TP/SL prices; never derive defaults."""
        if not self.enabled:
            return [{"instrument": row.get("instrument"), "status": "DISABLED"} for row in positions]
        if cached_at:
            return [{"instrument": row.get("instrument"), "status": "STALE_POSITION_BLOCKED"} for row in positions]
        if not isinstance(candidates, dict) or self.breaker.read().get("circuit_open"):
            return [{"instrument": row.get("instrument"), "status": "CIRCUIT_OPEN"} for row in positions]
        managed = dict(self.breaker.read().get("managed_orders", {})); results = []
        try:
            for snapshot in positions:
                instrument = str(snapshot.get("instrument", "unknown")); candidate = candidates.get(instrument)
                if not isinstance(candidate, dict): raise ProtectionError("dynamic candidate missing")
                position = position_from_snapshot(snapshot)
                desired = build_protection_order(position, float(candidate["stop_loss"]), float(candidate["take_profit"]))
                pending = self.transport.get_pending(instrument)
                matches = [row for row in pending if self._matching(row, desired)]
                if matches:
                    managed[instrument] = str(matches[0]["algoId"]); results.append({"instrument": instrument, "status": "PROTECTED", "algoId": matches[0]["algoId"]}); continue
                old = [str(row["algoId"]) for row in pending if self._owned_protection(row, desired)]
                new_id = self.transport.place_stop(desired); managed[instrument] = new_id
                if old: self.transport.cancel(instrument, old); status = "REPLACED"
                else: status = "CREATED"
                results.append({"instrument": instrument, "status": status, "algoId": new_id, "stop_loss": desired["slTriggerPx"], "take_profit": desired["tpTriggerPx"]})
            self.breaker.record_success(managed); return results
        except (OSError, KeyError, TypeError, ValueError, ProtectionError) as exc:
            self.breaker.record_failure(f"dynamic: {type(exc).__name__}: {exc}")
            return [{"instrument": "dynamic", "status": "ERROR", "error": type(exc).__name__}]
