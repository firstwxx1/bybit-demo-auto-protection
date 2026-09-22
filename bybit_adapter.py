"""Fail-closed Bybit V5 client and Demo Trading protection reconciler.

The client intentionally supports only Bybit's mainnet Demo Trading host
(`https://api-demo.bybit.com`).  It never silently switches to Testnet or the
real production account.  Trading-stop protection is attached to an existing
position with ``tpslMode=Full``; no position is opened by this module.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

DEMO_BASE_URL = "https://api-demo.bybit.com"
# Kept as a compatibility alias for callers that used the first migration.
BASE = DEMO_BASE_URL

_ALLOWED_REQUESTS = {
    ("GET", "/v5/position/list"),
    ("GET", "/v5/market/kline"),
    ("GET", "/v5/market/instruments-info"),
    ("POST", "/v5/position/trading-stop"),
    ("POST", "/v5/order/create"),
    ("GET", "/v5/order/realtime"),
    ("GET", "/v5/order/history"),
}


class BybitError(RuntimeError):
    """A Bybit API or local validation failure."""

    def __init__(self, message: str, *, ret_code: int | None = None) -> None:
        super().__init__(message)
        self.ret_code = ret_code


def _sign(api_key: str, secret: str, timestamp: str, recv: str, payload: str) -> str:
    material = timestamp + api_key + recv + payload
    return hmac.new(secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()


def _text_number(value: Any, *, positive: bool = False, name: str = "number") -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BybitError(f"{name}必须为数值") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise BybitError(f"{name}必须为有限正数")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if "." not in text:
        # Keep an explicit decimal component for the JSON contract used by
        # the adapter and by callers that compare request bodies.
        text = text + ".0"
    return text or "0.0"


def _positive_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BybitError(f"{name}必须为数值") from exc
    if not math.isfinite(number) or number <= 0:
        raise BybitError(f"{name}必须为有限正数")
    return number


def _canonical_params(params: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    pairs = [(str(key), str(value)) for key, value in params.items() if value is not None]
    pairs.sort(key=lambda item: item[0])
    return urlencode(pairs), pairs


def _normalise_interval(interval: str | int) -> str:
    aliases = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
        "4H": "240", "1H": "60", "1D": "D", "1W": "W", "1M": "M",
    }
    value = str(interval).strip()
    value = aliases.get(value, value)
    if value not in {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}:
        raise BybitError(f"不支持的Bybit K线周期：{interval}")
    return value


class BybitDemoClient:
    """Signed V5 client locked to mainnet Demo Trading."""

    def __init__(
        self,
        api_key: str,
        secret: str,
        base_url: str = DEMO_BASE_URL,
        timeout: float = 20,
        recv_window: str = "5000",
    ) -> None:
        parsed = urlparse(str(base_url).rstrip("/"))
        if str(base_url).rstrip("/") != DEMO_BASE_URL or parsed.scheme != "https" or parsed.hostname != "api-demo.bybit.com":
            raise BybitError("Bybit Demo 客户端只允许 https://api-demo.bybit.com")
        if not api_key or not secret:
            raise BybitError("Bybit Demo API 凭据不完整")
        self.api_key = api_key
        self.secret = secret
        self.base_url = DEMO_BASE_URL
        self.timeout = float(timeout)
        self.recv_window = str(recv_window)

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = str(method).upper()
        if (method, path) not in _ALLOWED_REQUESTS:
            raise BybitError(f"请求不在Bybit安全白名单：{method} {path}")
        params = dict(params or {})
        body = dict(body or {})
        query, _ = _canonical_params(params)
        payload = query if method == "GET" else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        signature = _sign(self.api_key, self.secret, timestamp, self.recv_window, payload)
        url = self.base_url + path + (("?" + query) if method == "GET" and query else "")
        request = Request(
            url,
            method=method,
            data=(payload.encode("utf-8") if method != "GET" else None),
            headers={
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "Content-Type": "application/json",
                "User-Agent": "bybit-demo-auto-protection/2.0",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                payload_error = json.loads(raw)
                message = payload_error.get("retMsg", raw)
                code = payload_error.get("retCode")
            except (OSError, ValueError):
                message, code = str(exc), None
            raise BybitError(f"Bybit HTTP请求失败：{message}", ret_code=code) from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise BybitError(f"Bybit网络请求失败：{type(exc).__name__}") from exc
        if not isinstance(result, dict) or result.get("retCode") != 0:
            message = result.get("retMsg", "Bybit request failed") if isinstance(result, dict) else "invalid response"
            code = result.get("retCode") if isinstance(result, dict) else None
            raise BybitError(str(message), ret_code=code)
        return result

    @staticmethod
    def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        rows = result.get("result", {}).get("list", [])
        return [row for row in rows if isinstance(row, dict)]

    def positions(self, settle_coin: str = "USDT") -> list[dict[str, Any]]:
        """Return all non-zero linear positions, following the cursor safely."""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"category": "linear", "settleCoin": settle_coin}
            if cursor:
                params["cursor"] = cursor
            result = self.request("GET", "/v5/position/list", params=params)
            rows.extend(self._rows(result))
            cursor = result.get("result", {}).get("nextPageCursor") or None
            if not cursor:
                break
        normalised: list[dict[str, Any]] = []
        for row in rows:
            try:
                if float(row.get("size") or 0) <= 0 or row.get("side") not in {"Buy", "Sell"}:
                    continue
                normalised.append(normalize_position(row))
            except (BybitError, KeyError, TypeError, ValueError):
                # An unusable row must not become an executable position.
                continue
        return normalised

    def get_position(self, instrument: str, position_idx: int | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {"category": "linear", "symbol": instrument}
        result = self.request("GET", "/v5/position/list", params=params)
        rows = self._rows(result)
        for raw in rows:
            if position_idx is not None and int(raw.get("positionIdx") or 0) != int(position_idx):
                continue
            if raw.get("symbol") != instrument or float(raw.get("size") or 0) <= 0:
                continue
            if raw.get("side") not in {"Buy", "Sell"}:
                continue
            return normalize_position(raw)
        return None

    def candles(self, symbol: str, limit: int = 100, interval: str | int = "240") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        result = self.request("GET", "/v5/market/kline", params={
            "category": "linear", "symbol": symbol, "interval": _normalise_interval(interval), "limit": limit,
        })
        rows = result.get("result", {}).get("list", [])
        candles: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                stamp = datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                continue
            candles.append({"ts": stamp, "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5]})
        return candles

    def set_trading_stop(
        self,
        position: dict[str, Any],
        stop_loss: float,
        take_profit: float | None,
    ) -> str:
        """Set full-position MarkPrice protection; ``None`` explicitly clears TP."""
        mark = _positive_float(position.get("mark_price"), "mark_price")
        stop = _positive_float(stop_loss, "stop_loss")
        liq_value = position.get("liquidation_price")
        liq = _positive_float(liq_value, "liquidation_price") if liq_value not in (None, "", "0", 0) else None
        side = str(position.get("side", "")).lower()
        if side not in {"long", "short"}:
            raise BybitError("持仓方向无效")
        if side == "long":
            if stop >= mark or (liq is not None and stop <= liq):
                raise BybitError("多单止损方向或强平边界无效")
            if take_profit is not None and _positive_float(take_profit, "take_profit") <= mark:
                raise BybitError("多单止盈方向无效")
        else:
            if stop <= mark or (liq is not None and stop >= liq):
                raise BybitError("空单止损方向或强平边界无效")
            if take_profit is not None and _positive_float(take_profit, "take_profit") >= mark:
                raise BybitError("空单止盈方向无效")
        try:
            position_idx = int(position.get("position_idx", 0))
        except (TypeError, ValueError) as exc:
            raise BybitError("positionIdx无效") from exc
        if position_idx not in {0, 1, 2}:
            raise BybitError("positionIdx无效")
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": str(position.get("instrument", "")),
            "tpslMode": "Full",
            "positionIdx": position_idx,
            "stopLoss": _text_number(stop, positive=True, name="stop_loss"),
            "slTriggerBy": "MarkPrice",
            # Bybit documents 0 as the explicit cancel value.  This is what
            # makes the SL-only fallback remove a stale TP instead of leaving
            # the previous TP active.
            "takeProfit": "0",
        }
        if not body["symbol"]:
            raise BybitError("symbol缺失")
        if take_profit is not None:
            body["takeProfit"] = _text_number(take_profit, positive=True, name="take_profit")
            body["tpTriggerBy"] = "MarkPrice"
        self.request("POST", "/v5/position/trading-stop", body=body)
        return "trading-stop"

    def create_close_order(
        self,
        position: dict[str, Any],
        quantity: float | str | None = None,
        *,
        order_link_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit one reduce-only market order in the opposite direction."""
        symbol = str(position.get("instrument", ""))
        side = str(position.get("side", "")).lower()
        if not symbol or side not in {"long", "short"}:
            raise BybitError("主动平仓持仓字段无效")
        qty = quantity if quantity is not None else position.get("size")
        qty_text = _text_number(qty, positive=True, name="close_qty")
        try:
            position_idx = int(position.get("position_idx", 0))
        except (TypeError, ValueError) as exc:
            raise BybitError("positionIdx无效") from exc
        if position_idx not in {0, 1, 2}:
            raise BybitError("positionIdx无效")
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell" if side == "long" else "Buy",
            "orderType": "Market",
            "qty": qty_text,
            "positionIdx": position_idx,
            "reduceOnly": True,
            "closeOnTrigger": True,
        }
        if order_link_id:
            body["orderLinkId"] = str(order_link_id)[:36]
        result = self.request("POST", "/v5/order/create", body=body)
        return {**body, **(result.get("result", {}) if isinstance(result.get("result"), dict) else {})}

    def get_order(self, instrument: str, order_id: str, *, history_fallback: bool = True) -> dict[str, Any]:
        params = {"category": "linear", "symbol": instrument, "orderId": order_id}
        result = self.request("GET", "/v5/order/realtime", params=params)
        rows = self._rows(result)
        if not rows and history_fallback:
            result = self.request("GET", "/v5/order/history", params=params)
            rows = self._rows(result)
        if not rows:
            raise BybitError("订单状态缺失")
        return rows[0]


def normalize_position(raw: dict[str, Any]) -> dict[str, Any]:
    raw_side = raw.get("side")
    if raw_side not in {"Buy", "Sell"}:
        raise BybitError("Bybit持仓方向无效")
    try:
        position_idx = int(raw.get("positionIdx") or 0)
    except (TypeError, ValueError) as exc:
        raise BybitError("Bybit positionIdx无效") from exc
    if position_idx not in {0, 1, 2}:
        raise BybitError("Bybit positionIdx无效")
    symbol = str(raw.get("symbol") or "").strip()
    if not symbol:
        raise BybitError("Bybit symbol缺失")
    size = _positive_float(raw.get("size"), "size")
    # tradeMode is deprecated in current V5 responses.  Do not call 0
    # "cross"; unknown is safer than asserting a margin mode that was not
    # returned by the exchange.
    margin_mode = raw.get("marginMode") if raw.get("marginMode") in {"cross", "isolated"} else None
    if margin_mode is None and str(raw.get("tradeMode") or "") == "1":
        margin_mode = "isolated"
    position_side = {0: "net", 1: "long", 2: "short"}[position_idx]
    take_profit = raw.get("takeProfit")
    stop_loss = raw.get("stopLoss")
    if take_profit in (None, "", "0", 0):
        take_profit = None
    if stop_loss in (None, "", "0", 0):
        stop_loss = None
    return {
        "instrument": symbol,
        "side": "long" if raw_side == "Buy" else "short",
        "size": size,
        "size_unit": "合约",
        "leverage": raw.get("leverage") or "0",
        "entry_price": raw.get("avgPrice") or raw.get("avgEntryPrice") or "0",
        "mark_price": raw.get("markPrice") or "0",
        "unrealized_pnl": raw.get("unrealisedPnl", raw.get("unrealizedPnl", 0)),
        "liquidation_price": raw.get("liqPrice") or None,
        "margin_mode": margin_mode,
        "position_side": position_side,
        "position_idx": position_idx,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
    }


def _same_price(actual: Any, desired: Any) -> bool:
    if desired is None:
        return actual in (None, "", "0", 0)
    try:
        return math.isclose(float(actual), float(desired), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


class BybitProtectionEngine:
    """Persistent, fail-closed reconciliation of live position TP/SL.

    Bybit's V5 ``Set Trading Stop`` endpoint owns the position protection
    instead of exposing exchange-specific pending algo orders.  Reconciliation is
    therefore idempotent: an unchanged TP/SL is reported as ``PROTECTED``;
    otherwise the full-position protection is updated in one API call.
    """

    def __init__(
        self,
        client: BybitDemoClient,
        state_path: Path,
        enabled: bool = False,
        failure_limit: int | None = None,
    ) -> None:
        self.client = client
        self.state_path = Path(state_path)
        self.enabled = bool(enabled)
        self.failure_limit = max(1, int(failure_limit or os.getenv("PROTECTION_FAILURE_LIMIT", "3")))

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.state_path)

    def _read_state(self) -> tuple[dict[str, Any], bool]:
        if not self.state_path.exists():
            return {"failures": 0, "circuit_open": False, "managed": {}}, False
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state must be an object")
            payload.setdefault("failures", 0)
            payload.setdefault("circuit_open", False)
            payload.setdefault("managed", {})
            return payload, False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"failures": self.failure_limit, "circuit_open": True, "managed": {}, "error": "STATE_UNREADABLE"}, True

    def _persist_failure(self, state: dict[str, Any], error: str) -> None:
        state["failures"] = int(state.get("failures", 0)) + 1
        state["circuit_open"] = state["failures"] >= self.failure_limit
        state["last_error"] = error[:500]
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

    def _persist_success(self, state: dict[str, Any]) -> None:
        state["failures"] = 0
        state["circuit_open"] = False
        state["last_error"] = None
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_candidate(position: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, float | None]:
        if not isinstance(candidate, dict):
            raise BybitError("dynamic candidate missing")
        try:
            stop = _positive_float(candidate["stop_loss"], "stop_loss")
        except KeyError as exc:
            raise BybitError("stop_loss missing") from exc
        take_profit = None
        if candidate.get("take_profit") not in (None, "", "0", 0):
            take_profit = _positive_float(candidate["take_profit"], "take_profit")
        side = str(position.get("side", "")).lower()
        mark = _positive_float(position.get("mark_price"), "mark_price")
        liq = position.get("liquidation_price")
        liq_value = _positive_float(liq, "liquidation_price") if liq not in (None, "", "0", 0) else None
        if side == "short":
            if stop <= mark or (liq_value and stop >= liq_value) or (take_profit is not None and take_profit >= mark):
                raise BybitError("空单TP/SL方向或强平边界无效")
        elif side == "long":
            if stop >= mark or (liq_value and stop <= liq_value) or (take_profit is not None and take_profit <= mark):
                raise BybitError("多单TP/SL方向或强平边界无效")
        else:
            raise BybitError("持仓方向无效")
        return stop, take_profit

    def _reconcile_candidates(
        self,
        positions: list[dict[str, Any]],
        candidates: dict[str, dict[str, Any]],
        *,
        cached_at: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return [{"instrument": row.get("instrument"), "status": "DISABLED"} for row in positions]
        if cached_at is not None:
            return [{"instrument": row.get("instrument"), "status": "STALE_POSITION_BLOCKED"} for row in positions]
        state, corrupt = self._read_state()
        if corrupt or state.get("circuit_open"):
            return [{"instrument": row.get("instrument"), "status": "CIRCUIT_OPEN"} for row in positions]
        if not isinstance(candidates, dict):
            candidates = {}
        results: list[dict[str, Any]] = []
        managed: dict[str, Any] = dict(state.get("managed") or {})
        had_error = False
        for position in positions:
            instrument = str(position.get("instrument", ""))
            candidate = candidates.get(instrument)
            if candidate is None:
                results.append({"instrument": instrument, "status": "NO_VALID_CANDIDATE"})
                continue
            try:
                stop, take_profit = self._validate_candidate(position, candidate)
                if _same_price(position.get("stop_loss"), stop) and _same_price(position.get("take_profit"), take_profit):
                    managed[instrument] = {"stop_loss": stop, "take_profit": take_profit}
                    results.append({
                        "instrument": instrument,
                        "status": "PROTECTED",
                        "stop_loss": stop,
                        "take_profit": take_profit,
                        "protection_mode": "TP_SL" if take_profit is not None else "SL_ONLY",
                    })
                    continue
                self.client.set_trading_stop(position, stop, take_profit)
                managed[instrument] = {"stop_loss": stop, "take_profit": take_profit}
                results.append({
                    "instrument": instrument,
                    "status": "UPDATED",
                    "stop_loss": stop,
                    "take_profit": take_profit,
                    "protection_mode": "TP_SL" if take_profit is not None else "SL_ONLY",
                })
            except (BybitError, OSError, KeyError, TypeError, ValueError) as exc:
                had_error = True
                error = f"{instrument}: {type(exc).__name__}: {exc}"
                self._persist_failure(state, error)
                results.append({"instrument": instrument, "status": "ERROR", "error": type(exc).__name__})
                break
        if had_error and state.get("circuit_open"):
            seen = {row.get("instrument") for row in results}
            results.extend({"instrument": row.get("instrument"), "status": "CIRCUIT_OPEN"} for row in positions if row.get("instrument") not in seen)
        elif not had_error:
            self._persist_success(state)
        state["managed"] = managed
        try:
            self._write_state(state)
        except OSError:
            # API side effects may have happened but their state is unknown;
            # leave a conservative in-memory error result for this cycle.
            results.append({"instrument": "state", "status": "POST_EXECUTION_STATE_UNKNOWN"})
        return results

    def reconcile_dynamic(
        self,
        positions: list[dict[str, Any]],
        candidates: dict[str, dict[str, Any]],
        *,
        cached_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Apply fresh, report-supplied TP/SL prices; never derive defaults."""
        return self._reconcile_candidates(positions, candidates, cached_at=cached_at)

    @staticmethod
    def _fixed_candidate(
        position: dict[str, Any],
        stop_pct: float,
        take_profit_pct: float | None,
    ) -> dict[str, float | None]:
        if not math.isfinite(stop_pct) or not 0 < stop_pct <= 0.25:
            raise BybitError("fixed stop ratio is outside safety bounds")
        side = str(position.get("side", "")).lower()
        entry = _positive_float(position.get("entry_price"), "entry_price")
        mark = _positive_float(position.get("mark_price"), "mark_price")
        if side == "short":
            stop = entry * (1 + stop_pct)
        elif side == "long":
            stop = entry * (1 - stop_pct)
        else:
            raise BybitError("持仓方向无效")
        take_profit: float | None = None
        if take_profit_pct is not None:
            if not math.isfinite(take_profit_pct) or not 0 < take_profit_pct <= 0.5:
                raise BybitError("take-profit ratio is outside safety bounds")
            take_profit = mark * (1 - take_profit_pct if side == "short" else 1 + take_profit_pct)
        return {"stop_loss": round(stop, 8), "take_profit": (round(take_profit, 8) if take_profit is not None else None)}

    def reconcile(
        self,
        positions: list[dict[str, Any]],
        *,
        cached_at: str | None = None,
        stop_pct: float = 0.04855847842644323,
        take_profit_pct: float | None = None,
    ) -> list[dict[str, Any]]:
        """Preserve the original fixed-protection entrypoint on Bybit.

        The old exchange-specific pending-order reconciliation is replaced by
        the idempotent V5 Trading Stop update, while keeping the fixed stop
        fallback and SL-only mode for callers that still use ``protection_runner``.
        """
        if not self.enabled:
            return [{"instrument": row.get("instrument"), "status": "DISABLED"} for row in positions]
        if cached_at is not None:
            return [{"instrument": row.get("instrument"), "status": "STALE_POSITION_BLOCKED"} for row in positions]
        candidates: dict[str, dict[str, Any]] = {}
        invalid: list[dict[str, Any]] = []
        valid_positions: list[dict[str, Any]] = []
        for position in positions:
            instrument = str(position.get("instrument", ""))
            try:
                candidate = self._fixed_candidate(position, float(stop_pct), take_profit_pct)
                self._validate_candidate(position, candidate)
            except (BybitError, TypeError, ValueError) as exc:
                invalid.append({
                    "instrument": instrument,
                    "status": "MANUAL_INTERVENTION_REQUIRED",
                    "reason": str(exc),
                })
                continue
            candidates[instrument] = candidate
            valid_positions.append(position)
        return invalid + self._reconcile_candidates(valid_positions, candidates)


ProtectionError = BybitError

__all__ = [
    "BASE", "DEMO_BASE_URL", "BybitError", "ProtectionError", "BybitDemoClient", "BybitProtectionEngine",
    "normalize_position", "_sign",
]
