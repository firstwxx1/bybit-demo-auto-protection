"""Fail-closed deterministic gate for news-driven existing-position actions."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse


class Action(str, Enum):
    HOLD = "HOLD"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    CLOSE_POSITION = "CLOSE_POSITION"
    NO_ACTION = "NO_ACTION"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class DecisionError(ValueError):
    pass


RULE_VERSION = "news-close-gate-v1"
_ACTIONS = {item.value for item in Action}
REQUIRED_RISK_SOURCES = {"gpt-5.6-sol", "independent-risk"}
DEFAULT_NEWS_MAX_AGE_SECONDS = 900


def _configured_max_age_seconds() -> int:
    try:
        value = int(float(os.getenv("NEWS_MAX_AGE_SECONDS", str(DEFAULT_NEWS_MAX_AGE_SECONDS))))
        if value <= 0:
            raise ValueError
        return value
    except (TypeError, ValueError):
        return DEFAULT_NEWS_MAX_AGE_SECONDS


def _age(value: Any, now: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise DecisionError("timestamp invalid") from exc
    return (now - parsed).total_seconds()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)


def decision_id_for(decision: dict[str, Any]) -> str:
    """Derive the ID from every immutable field used by the execution gate."""
    material = {"position": decision.get("position"), "action": decision.get("action"),
                "rule_version": decision.get("rule_version"), "created_at": decision.get("created_at"),
                "evidence_window": decision.get("evidence_window"),
                "evidence_snapshot": decision.get("evidence_snapshot"),
                "evidence_hash": decision.get("evidence_hash")}
    return hashlib.sha256(_canonical(material).encode()).hexdigest()


def _base(position: dict[str, Any], action: Action, reason: str, payload: Any, *, now: datetime,
          evidence_snapshot: dict[str, Any] | None = None, max_age_seconds: int = 900) -> dict[str, Any]:
    created_at = now.astimezone(timezone.utc).isoformat()
    snapshot = evidence_snapshot if isinstance(evidence_snapshot, dict) else {"payload": payload}
    evidence_hash = hashlib.sha256(_canonical(snapshot).encode()).hexdigest()
    result = {"rule_version": RULE_VERSION, "action": action.value, "reason": reason, "position": position,
              "created_at": created_at, "evidence_window": {"as_of": created_at, "max_age_seconds": max_age_seconds},
              "evidence_snapshot": snapshot, "evidence_hash": evidence_hash}
    result["decision_id"] = decision_id_for(result)
    decision_id = result["decision_id"]
    return {"decision_id": decision_id, "rule_version": RULE_VERSION, "action": action.value,
            "reason": reason, "position": position, "created_at": created_at,
            "evidence_window": result["evidence_window"], "evidence_snapshot": snapshot,
            "evidence_hash": evidence_hash}


def evaluate_decision(position: dict[str, Any], news: dict[str, Any], model_results: list[dict[str, Any]], *,
                      now: datetime | None = None, max_age_seconds: int | None = None,
                      min_confidence: float = 0.75, candles: list[dict[str, Any]] | None = None,
                      evidence_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an auditable action. Invalid or uncertain evidence never closes."""
    now = now or datetime.now(timezone.utc)
    if max_age_seconds is None:
        max_age_seconds = _configured_max_age_seconds()
    if not isinstance(position, dict) or not str(position.get("instrument", "")).endswith("-SWAP"):
        raise DecisionError("only an existing perpetual position is eligible")
    snapshot = evidence_snapshot if isinstance(evidence_snapshot, dict) else {
        "position": position, "candles": candles if isinstance(candles, list) else [],
        "grok": news, "models": model_results, "captured_at": now.isoformat()}
    base = lambda action, reason, payload: _base(position, action, reason, payload, now=now,
                                                  evidence_snapshot=snapshot, max_age_seconds=max_age_seconds)
    if isinstance(model_results, list):
        for row in model_results:
            if isinstance(row, dict) and row.get("recommendation") is not None and row.get("recommendation") not in _ACTIONS:
                return base(Action.MANUAL_REVIEW_REQUIRED, "MODEL_ACTION_INVALID", model_results)
    if not isinstance(candles, list) or not candles:
        return base(Action.HOLD, "MARKET_EVIDENCE_MISSING", candles)
    try:
        parsed_times = []
        for candle in candles:
            if not isinstance(candle, dict): raise ValueError
            parsed_times.append(_age(candle.get("ts") or candle.get("timestamp"), now))
            for field in ("open", "high", "low", "close", "volume"):
                if float(candle.get(field)) < 0: raise ValueError
        if min(parsed_times) > max_age_seconds or max(parsed_times) < -30:
            return base(Action.HOLD, "MARKET_EVIDENCE_STALE", candles)
    except (DecisionError, TypeError, ValueError):
        return base(Action.MANUAL_REVIEW_REQUIRED, "MARKET_EVIDENCE_INVALID", candles)
    if (not isinstance(news, dict) or news.get("source") not in {"grok", "publisher-rss"}
            or news.get("available") is not True or news.get("evidence_verified") is not True):
        return base(Action.HOLD, "NEWS_MISSING_OR_UNAVAILABLE", news)
    try:
        if _age(news.get("as_of"), now) > max_age_seconds or _age(news.get("as_of"), now) < -30:
            return base(Action.HOLD, "NEWS_STALE", news)
    except DecisionError:
        return base(Action.MANUAL_REVIEW_REQUIRED, "NEWS_TIMESTAMP_INVALID", news)
    items = news.get("items")
    if not isinstance(items, list) or not items:
        return base(Action.HOLD, "NEWS_EMPTY", news)
    try:
        for item in items:
            if (not isinstance(item, dict) or not item.get("headline") or not item.get("published_at")
                    or not item.get("source_url")):
                return base(Action.HOLD, "NEWS_ITEM_STALE_OR_INVALID", news)
            item_age = _age(item.get("published_at"), now)
            parsed_url = urlparse(str(item.get("source_url")))
            if (item_age > max_age_seconds or item_age < -30
                    or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
                return base(Action.HOLD, "NEWS_ITEM_STALE_OR_INVALID", news)
            if item.get("source") not in (None, "grok", "publisher-rss"):
                return base(Action.MANUAL_REVIEW_REQUIRED, "NEWS_SOURCE_CONFLICT", news)
    except DecisionError:
        return base(Action.MANUAL_REVIEW_REQUIRED, "NEWS_ITEM_TIMESTAMP_INVALID", news)
    if not isinstance(model_results, list) or not model_results:
        return base(Action.HOLD, "MODEL_MISSING", model_results)
    if len(model_results) != len(REQUIRED_RISK_SOURCES):
        return base(Action.MANUAL_REVIEW_REQUIRED, "REQUIRED_RISK_SOURCE_MISSING", model_results)
    sources = [row.get("source") if isinstance(row, dict) else None for row in model_results]
    if set(sources) != REQUIRED_RISK_SOURCES or len(set(sources)) != len(sources):
        return base(Action.MANUAL_REVIEW_REQUIRED, "REQUIRED_RISK_SOURCE_MISSING_OR_DUPLICATE", model_results)
    unavailable_sources = [
        row.get("source", "unknown")
        for row in model_results
        if not isinstance(row, dict) or row.get("available") is not True
    ]
    if unavailable_sources:
        reason = (
            "GPT_MODEL_UNAVAILABLE"
            if unavailable_sources == ["gpt-5.6-sol"]
            else "INDEPENDENT_RISK_SOURCE_UNAVAILABLE"
            if unavailable_sources == ["independent-risk"]
            else "REQUIRED_RISK_MODEL_UNAVAILABLE"
        )
        return base(Action.MANUAL_REVIEW_REQUIRED, reason, model_results)
    sol = next(row for row in model_results if row["source"] == "gpt-5.6-sol")
    if sol.get("model") != "gpt-5.6-sol":
        return base(Action.MANUAL_REVIEW_REQUIRED, "SOL_MODEL_IDENTITY_INVALID", model_results)
    recommendations = []
    for row in model_results:
        recommendation = row.get("recommendation")
        if recommendation is None:
            return base(Action.MANUAL_REVIEW_REQUIRED, "MODEL_OUTPUT_INVALID", model_results)
        if recommendation not in _ACTIONS:
            return base(Action.MANUAL_REVIEW_REQUIRED, "MODEL_ACTION_INVALID", model_results)
        try:
            confidence = float(row.get("confidence"))
        except (TypeError, ValueError):
            return base(Action.MANUAL_REVIEW_REQUIRED, "MODEL_CONFIDENCE_INVALID", model_results)
        if confidence < min_confidence:
            return base(Action.MANUAL_REVIEW_REQUIRED, "CONFIDENCE_TOO_LOW", model_results)
        try:
            if _age(row.get("as_of"), now) > max_age_seconds or _age(row.get("as_of"), now) < -30:
                return base(Action.MANUAL_REVIEW_REQUIRED, "MODEL_STALE", model_results)
        except DecisionError:
            return base(Action.MANUAL_REVIEW_REQUIRED, "MODEL_TIMESTAMP_INVALID", model_results)
        recommendations.append(recommendation)
    if len(set(recommendations)) != 1:
        return base(Action.MANUAL_REVIEW_REQUIRED, "MODEL_SOURCE_CONFLICT", model_results)
    action = Action(recommendations[0])
    if action == Action.CLOSE_POSITION and not any(str(i.get("severity", "")).lower() in {"high", "critical"} for i in items):
        return base(Action.HOLD, "NEWS_NOT_MATERIAL", news)
    return base(action, "CONSENSUS_VALIDATED", {"news": news, "models": model_results})
