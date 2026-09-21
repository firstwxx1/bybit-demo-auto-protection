"""Bybit Demo live collector using the normalized schema expected by the risk core."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from bybit_adapter import BybitDemoClient, BybitError


def write_cache(path: Path, positions: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cached_at": datetime.now(timezone.utc).isoformat(), "positions": positions}, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def read_cache(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cached = datetime.fromisoformat(payload["cached_at"].replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - cached).total_seconds() > int(os.getenv("CACHE_MAX_AGE_SECONDS", "3600")):
        raise BybitError("Bybit 持仓查询失败且缓存已过期")
    return payload


def get_client() -> BybitDemoClient:
    return BybitDemoClient(os.getenv("BYBIT_DEMO_API_KEY", ""), os.getenv("BYBIT_DEMO_API_SECRET", ""), os.getenv("BYBIT_API_BASE", "https://api-demo.bybit.com"), float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")))


def fetch_positions(cache_path: Path, *, include_receipt: bool = False):
    try:
        positions = get_client().positions(os.getenv("BYBIT_SETTLE_COIN", "USDT"))
        write_cache(cache_path, positions)
        receipt = object()
        return (positions, None, receipt) if include_receipt else (positions, None)
    except (OSError, ValueError, KeyError, BybitError):
        payload = read_cache(cache_path)
        return (payload["positions"], payload["cached_at"], None) if include_receipt else (payload["positions"], payload["cached_at"])


def fetch_candles(instrument: str, limit: int = 100, *, bar: str = "240"):
    return get_client().candles(instrument, limit, bar)
