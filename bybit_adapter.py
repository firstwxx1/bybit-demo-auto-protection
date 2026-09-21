"""Bybit mainnet Demo Trading adapter (api-demo.bybit.com only)."""
from __future__ import annotations
import hashlib, hmac, json, os, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api-demo.bybit.com"

class BybitError(RuntimeError): pass

def _sign(api_key: str, secret: str, timestamp: str, recv: str, payload: str) -> str:
    return hmac.new(secret.encode(), (timestamp + api_key + recv + payload).encode(), hashlib.sha256).hexdigest()

class BybitDemoClient:
    """Fail-closed client. It refuses production and testnet hosts."""
    def __init__(self, api_key: str, secret: str, base_url: str = BASE, timeout: float = 20):
        if base_url.rstrip('/') != BASE:
            raise BybitError("Bybit Demo 客户端只允许 api-demo.bybit.com")
        if not api_key or not secret: raise BybitError("Bybit Demo API 凭据不完整")
        self.api_key, self.secret, self.base_url, self.timeout = api_key, secret, BASE, timeout
        self.recv_window = "5000"

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}; body = body or {}
        query = urlencode([(k, str(v)) for k, v in sorted(params.items()) if v is not None])
        payload = query if method == "GET" else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        ts = str(int(time.time() * 1000))
        sig = _sign(self.api_key, self.secret, ts, self.recv_window, payload)
        url = self.base_url + path + (("?" + query) if method == "GET" and query else "")
        req = Request(url, method=method, data=(payload.encode() if method != "GET" else None), headers={
            "X-BAPI-API-KEY": self.api_key, "X-BAPI-SIGN": sig, "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json", "User-Agent": "bybit-demo-auto-protection/1.0"})
        with urlopen(req, timeout=self.timeout) as response: result = json.load(response)
        if not isinstance(result, dict) or result.get("retCode") != 0:
            raise BybitError(str(result.get("retMsg", "Bybit request failed")))
        return result

    def positions(self, settle_coin: str = "USDT") -> list[dict[str, Any]]:
        rows = self.request("GET", "/v5/position/list", {"category": "linear", "settleCoin": settle_coin}).get("result", {}).get("list", [])
        return [normalize_position(x) for x in rows if float(x.get("size") or 0) > 0 and x.get("side") in {"Buy", "Sell"}]

    def candles(self, symbol: str, limit: int = 100, interval: str = "240") -> list[dict[str, Any]]:
        rows = self.request("GET", "/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}).get("result", {}).get("list", [])
        return [{"ts": datetime.fromtimestamp(int(r[0])/1000, timezone.utc).isoformat(), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows if len(r) >= 6]

    def set_trading_stop(self, position: dict[str, Any], stop_loss: float | None, take_profit: float | None) -> str:
        mark = float(position.get("mark_price") or 0)
        liq = float(position["liquidation_price"]) if position.get("liquidation_price") else None
        if mark <= 0 or stop_loss is None or float(stop_loss) <= 0:
            raise BybitError("止损价缺失或无效")
        stop_loss = float(stop_loss)
        if position["side"] == "long":
            if stop_loss >= mark or (liq and stop_loss <= liq):
                raise BybitError("多单止损方向或强平边界无效")
            if take_profit is not None and float(take_profit) <= mark:
                raise BybitError("多单止盈方向无效")
        else:
            if stop_loss <= mark or (liq and stop_loss >= liq):
                raise BybitError("空单止损方向或强平边界无效")
            if take_profit is not None and float(take_profit) >= mark:
                raise BybitError("空单止盈方向无效")
        body: dict[str, Any] = {"category": "linear", "symbol": position["instrument"], "tpslMode": "Full", "positionIdx": int(position.get("position_idx", 0)), "stopLoss": str(stop_loss), "slTriggerBy": "MarkPrice"}
        if take_profit is not None:
            body.update({"takeProfit": str(float(take_profit)), "tpTriggerBy": "MarkPrice"})
        self.request("POST", "/v5/position/trading-stop", body=body)
        return "trading-stop"

def normalize_position(raw: dict[str, Any]) -> dict[str, Any]:
    raw_side = raw.get("side")
    if raw_side not in {"Buy", "Sell"}:
        raise BybitError("Bybit 持仓方向无效")
    side = "long" if raw_side == "Buy" else "short"
    trade_mode = str(raw.get("tradeMode") or "")
    margin_mode = {"0": "cross", "1": "isolated"}.get(trade_mode, trade_mode or None)
    position_idx = int(raw.get("positionIdx") or 0)
    position_side = {0: "net", 1: "long", 2: "short"}.get(position_idx)
    return {"instrument": raw["symbol"], "side": side, "size": abs(float(raw.get("size") or 0)), "size_unit": "合约", "leverage": raw.get("leverage") or "0", "entry_price": raw.get("avgPrice") or raw.get("avgEntryPrice") or "0", "mark_price": raw.get("markPrice") or "0", "unrealized_pnl": raw.get("unrealisedPnl", 0), "liquidation_price": raw.get("liqPrice") or None, "margin_mode": margin_mode, "position_side": position_side, "position_idx": position_idx, "take_profit": raw.get("takeProfit") or "", "stop_loss": raw.get("stopLoss") or ""}

class BybitProtectionEngine:
    def __init__(self, client: BybitDemoClient, state_path: Path, enabled: bool = False):
        self.client, self.state_path, self.enabled = client, state_path, enabled
    def reconcile_dynamic(self, positions: list[dict[str, Any]], candidates: dict[str, dict[str, Any]], *, cached_at: str | None = None) -> list[dict[str, Any]]:
        if not self.enabled: return [{"instrument": p.get("instrument"), "status": "DISABLED"} for p in positions]
        if cached_at: return [{"instrument": p.get("instrument"), "status": "STALE_POSITION_BLOCKED"} for p in positions]
        out=[]
        for p in positions:
            c=candidates.get(p.get("instrument"))
            if not isinstance(c, dict): continue
            try:
                sl=c.get("stop_loss"); tp=c.get("take_profit")
                self.client.set_trading_stop(p, sl, tp)
                out.append({"instrument":p["instrument"],"status":"UPDATED","stop_loss":sl,"take_profit":tp})
            except Exception as exc: out.append({"instrument":p.get("instrument"),"status":"ERROR","error":type(exc).__name__})
        return out
