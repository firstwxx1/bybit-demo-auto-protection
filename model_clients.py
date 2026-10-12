"""Independent OpenAI-compatible adapters for Grok and GPT risk analysis."""
from __future__ import annotations

import json
import hashlib
import re
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree


# Stable source identifier consumed by the deterministic gate (decision_layer).
# It is an internal key, not the runtime model name.
RISK_SOURCE_ID = "gpt-5.6-sol"


def _chat(base: str, key: str, model: str, system: str, payload: dict[str, Any]) -> str:
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    }).encode()
    request = Request(base.rstrip("/") + "/chat/completions", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "curl/7.88.1",
    })
    with urlopen(request, timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))) as response:
        result = json.load(response)
    return result["choices"][0]["message"]["content"]


def _grok_x_search(base: str, key: str, model: str, instrument: str) -> str:
    """Use xAI's native Responses API so news is actually searched on X."""
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "tools": [{"type": "x_search"}],
        "input": [{"role": "system", "content": (
            "Search X for current, public posts relevant to the instrument. "
            "Return only JSON with summary, as_of, and items. Each item must have "
            "headline, published_at as an ISO-8601 timestamp, source_url, and a "
            "severity of critical|high|medium|low. "
            "Do not invent posts or URLs."
        )}, {"role": "user", "content": (
            f"Find credible news or material market updates for {instrument} from the last 24 hours."
        )}],
    }).encode()
    request = Request(base.rstrip("/") + "/responses", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "curl/7.88.1",
    })
    with urlopen(request, timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))) as response:
        result = json.load(response)
    for item in result.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    return content["text"]
    raise KeyError("Grok Responses未返回文本")


_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "PERP", "BUSD")


def _base_symbol(instrument: str) -> str:
    """Derive the base asset from an instrument such as ETHUSDT or ETH-USDT."""
    raw = str(instrument).upper().replace("/", "-").replace(":", "-").replace("_", "-")
    base = raw.split("-")[0]
    for suffix in _QUOTE_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    return base.lower()


# Deterministic news severity classification. The gate in decision_layer
# only lets CLOSE_POSITION through when at least one news item carries a
# high/critical severity, so this replaces a model guess with auditable
# keyword evidence. It is intentionally biased towards under-reporting:
# a headline only reaches high/critical on unambiguous market events.
SEVERITY_LEVELS = ("critical", "high", "medium", "low")

_SEVERITY_RULES = (
    ("critical", (
        r"\bhack(?:ed|er|ers|ing)?\b", r"\bexploit(?:ed|s)?\b", r"\bbreach(?:ed)?\b",
        r"\bdrain(?:ed|s)?\b", r"\bdepegged?\b", r"\binsolven(?:cy|t)\b",
        r"\bbankrupt(?:cy)?\b", r"\bblockchain halt\b", r"\bnetwork halt\b",
        r"\bemergency (?:halt|shutdown)\b", r"\bcollaps(?:e|ed)\b",
        r"被盗", r"被黑", r"遭黑客", r"脱锚", r"破产", r"暂停交易", r"崩溃", r"清算潮",
    )),
    ("high", (
        r"\bsec\b.*\b(?:sue|sues|sued|lawsuit|charges|charges|probe|investigat)\w*\b",
        r"\bdoj\b", r"\bindict(?:ed|ment)?\b", r"\blawsuit\b", r"\bsues?\b",
        r"\bban(?:ned|s)?\b", r"\bdelist(?:ed|ing)?\b", r"\bblacklist(?:ed)?\b",
        r"\bcircuit breaker\b", r"\btrading halt(?:ed)?\b", r"\boutage\b",
        r"\bliquidat(?:e|ed|ion|ions)\b", r"\bflash crash\b", r"\bplunge[ds]?\b",
        r"\btumbl(?:e|ed|es)\b", r"\bslump(?:ed|s)?\b", r"\bcrash(?:ed|es)?\b",
        r"\bsurge[ds]?\b", r"\brall(?:y|ied|ies)\b", r"\ball[- ]time high\b",
        r"\brecord high\b", r"\bdefault(?:ed|s)?\b", r"\bsolven(?:cy|t)\b",
        r"\bhalt(?:ed|s)?\b", r"\bfreeze[ds]?\b",
        r"被起诉", r"遭起诉", r"下架", r"封禁", r"暴跌", r"崩盘", r"史上新高", r"创新高", r"暂停",
    )),
    ("medium", (
        r"\blist(?:ed|s|ing)?\b", r"\bpartnership\b", r"\bpartner(?:s|ed)?\b",
        r"\bintegrat(?:e|ed|ion)\b", r"\bupgrade[ds]?\b", r"\bhalving\b",
        r"\bburn(?:ed|s)?\b", r"\bstaking\b", r"\badoption\b", r"\bfunding\b",
        r"\bunlock(?:ed|s)?\b", r"\bairdrop(?:s)?\b", r"\bmainnet\b",
        r"上线", r"合作", r"升级", r"减半", r"销毁", r"质押", r"解锁", r"空投", r"主网",
    )),
)


def _classify_severity(headline: str) -> str:
    """Return critical/high/medium/low from auditable headline keywords."""
    text = str(headline or "").lower()
    for level, patterns in _SEVERITY_RULES:
        for pattern in patterns:
            if re.search(pattern, text):
                return level
    return "low"


def _normalize_severity(value: Any, headline: str) -> str:
    """Keep an explicitly supplied valid level, otherwise classify the headline."""
    text = str(value or "").strip().lower()
    if text in SEVERITY_LEVELS:
        return text
    return _classify_severity(headline)


def _rss_news(instrument: str) -> dict[str, Any]:
    """Collect publisher RSS evidence when the Grok proxy ignores x_search."""
    urls = [url.strip() for url in os.getenv("NEWS_RSS_URLS", "").split(",") if url.strip()]
    if not urls:
        return {"available": False, "summary": "RSS未配置", "items": [], "evidence_verified": False}
    symbol = _base_symbol(instrument)
    now = datetime.now(timezone.utc)
    max_age = float(os.getenv("NEWS_MAX_AGE_SECONDS", "86400"))
    fresh_hours = max(1, int(max_age // 3600))
    items: list[dict[str, str]] = []
    for feed_url in urls:
        try:
            request = Request(feed_url, headers={"User-Agent": "curl/7.88.1"})
            with urlopen(request, timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))) as response:
                root = ElementTree.fromstring(response.read())
            entries = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for entry in entries:
                def text(*names: str) -> str:
                    for name in names:
                        node = entry.find(name)
                        if node is not None and node.text:
                            return node.text.strip()
                    return ""
                title = text("title", "{http://www.w3.org/2005/Atom}title")
                link = text("link", "{http://www.w3.org/2005/Atom}link")
                if not link:
                    node = entry.find("{http://www.w3.org/2005/Atom}link")
                    link = (node.get("href", "") if node is not None else "").strip()
                published_raw = text("pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated")
                if not title or not link or symbol not in title.lower():
                    continue
                try:
                    published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
                except ValueError:
                    published = parsedate_to_datetime(published_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                age = (now - published.astimezone(timezone.utc)).total_seconds()
                if -30 <= age <= max_age:
                    items.append({"headline": title, "published_at": published.astimezone(timezone.utc).isoformat(), "source_url": urljoin(feed_url, link), "severity": _classify_severity(title)})
        except (OSError, ValueError, ElementTree.ParseError):
            continue
    unique = {item["source_url"]: item for item in items}
    selected = list(unique.values())[:5]
    return {"available": bool(selected), "source": "publisher-rss", "model": "deterministic", "as_of": now.isoformat(), "summary": f"已通过公开RSS取得最近{fresh_hours}小时{symbol.upper()}新闻" if selected else f"RSS未取得最近{fresh_hours}小时{symbol.upper()}新闻", "items": selected, "evidence_verified": bool(selected)}


def grok_news(position: dict[str, Any]) -> dict[str, Any]:
    key = os.getenv("GROK_API_KEY", "")
    if not key or key == "[REDACTED]":
        return {"available": False, "summary": "Grok未配置"}
    try:
        content = None
        selected_model = os.getenv("GROK_MODEL", "grok-4")
        models = [selected_model]
        fallback_model = os.getenv("GROK_FALLBACK_MODEL", "").strip()
        if fallback_model and fallback_model not in models:
            models.append(fallback_model)
        for index, model_name in enumerate(models):
            try:
                try:
                    content = _grok_x_search(os.getenv("GROK_API_BASE", "https://api.x.ai/v1"), key,
                                             model_name, position["instrument"])
                except (OSError, KeyError, ValueError):
                    # Compatibility proxies may expose chat but not native Responses.
                    content = _chat(os.getenv("GROK_API_BASE", "https://api.x.ai/v1"), key,
                                    model_name,
                                    "仅分析与持仓币种相关的新闻和舆情。不得建议下单、仓位、止损或执行交易。"
                                    "必须返回JSON，items中的每条新闻必须包含headline、published_at、source_url、severity（critical/high/medium/low）。",
                                    {"instrument": position["instrument"]})
                selected_model = model_name
                break
            except HTTPError as exc:
                if exc.code not in {408, 429, 500, 502, 503, 504} or index == len(models) - 1:
                    raise
        if content is None:
            raise RuntimeError("Grok未返回内容")
        now = datetime.now(timezone.utc).isoformat()
        parsed = None
        try:
            parsed = json.loads(content.strip().removeprefix("```json").removesuffix("```").strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            items = parsed["items"]
            for item in items:
                if isinstance(item, dict):
                    item["severity"] = _normalize_severity(item.get("severity"), str(item.get("headline", "")))
            verified = bool(items) and all(isinstance(item, dict) and item.get("headline")
                                           and item.get("published_at") and item.get("source_url") for item in items)
            if verified:
                now_dt = datetime.now(timezone.utc)
                for item in items:
                    try:
                        published = datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00"))
                        if published.tzinfo is None:
                            published = published.replace(tzinfo=timezone.utc)
                        age_seconds = (now_dt - published).total_seconds()
                        max_age_seconds = float(os.getenv("NEWS_MAX_AGE_SECONDS", "86400"))
                        if age_seconds < -30 or age_seconds > max_age_seconds:
                            verified = False
                            break
                    except (TypeError, ValueError):
                        verified = False
                        break
            summary = parsed.get("summary", content.strip())
            if not verified:
                summary = "新闻证据未通过校验：时间校验失败，存在缺失、过期或未来时间戳"
            result = {"available": True, "source": "grok", "model": selected_model, "as_of": parsed.get("as_of", now),
                      "summary": summary, "items": items,
                      "evidence_verified": verified}
            if not verified and os.getenv("NEWS_RSS_ENABLED", "false").lower() == "true":
                rss = _rss_news(position["instrument"])
                if rss.get("evidence_verified"):
                    return rss
            return result
        # Grok answered with prose instead of JSON items (e.g. the proxy ignored
        # x_search): still try the whitelisted publisher RSS evidence source.
        if os.getenv("NEWS_RSS_ENABLED", "false").lower() == "true":
            rss = _rss_news(position["instrument"])
            if rss.get("evidence_verified"):
                return rss
        return {"available": True, "source": "grok", "as_of": now,
                "summary": content.strip(), "items": [], "evidence_verified": False}
    except HTTPError as exc:
        if os.getenv("NEWS_RSS_ENABLED", "false").lower() == "true":
            rss = _rss_news(position["instrument"])
            if rss.get("evidence_verified"):
                return rss
        return {"available": False, "summary": f"Grok不可用：HTTP {exc.code}"}
    except (OSError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        if os.getenv("NEWS_RSS_ENABLED", "false").lower() == "true":
            rss = _rss_news(position["instrument"])
            if rss.get("evidence_verified"):
                return rss
        return {"available": False, "summary": f"Grok不可用：{type(exc).__name__}"}


def risk_analysis(position: dict[str, Any], fib: dict[str, Any], news: dict[str, Any]) -> dict[str, Any]:
    key = os.getenv("RISK_MODEL_API_KEY", "")
    base = os.getenv("RISK_MODEL_API_BASE", "")
    if not key or key == "[REDACTED]" or not base or base == "[REDACTED]":
        return {"available": False, "error": "GPT未配置"}
    model_id = os.getenv("RISK_MODEL", RISK_SOURCE_ID)
    system = (
        "你是只读持仓风险分析器。只返回JSON对象，字段为risk_level、confidence、recommendation、take_profit、stop_loss。"
        "recommendation只能是HOLD、TIGHTEN_STOP、CLOSE_POSITION、NO_ACTION、MANUAL_REVIEW_REQUIRED之一。"
        "不得决定仓位或交易动作。"
        "take_profit是基于Fib回撤和当前结构的止盈建议，无可靠依据时必须为null。"
        "stop_loss是基于持仓方向、强平价和当前波动的止损建议价格，无可靠依据时必须为null。"
        "对于空单：stop_loss必须高于当前标记价且低于强平价，take_profit必须低于当前标记价。"
        "对于多单：stop_loss必须低于当前标记价且高于强平价，take_profit必须高于当前标记价。"
    )
    try:
        content = _chat(base, key, model_id, system,
                        {"position": position, "fib": fib, "news": news})
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("模型输出不是对象")
        return {"available": True, "source": RISK_SOURCE_ID, "model": model_id,
                "as_of": datetime.now(timezone.utc).isoformat(), **result}
    except HTTPError as exc:
        return {"available": False, "source": RISK_SOURCE_ID, "model": model_id,
                "error": f"GPT不可用：HTTP {exc.code}"}
    except (OSError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        return {"available": False, "source": RISK_SOURCE_ID, "model": model_id,
                "error": f"GPT不可用：{type(exc).__name__}"}


INDEPENDENT_RISK_SOURCE_ID = "independent-risk"


def _independent_risk_system_prompt() -> str:
    return (
        "你是独立的只读持仓风险复核器，与主风险模型不共享凭据或端点。"
        "只返回JSON对象，字段为risk_level、confidence、recommendation、take_profit、stop_loss。"
        "recommendation只能是HOLD、TIGHTEN_STOP、CLOSE_POSITION、NO_ACTION、MANUAL_REVIEW_REQUIRED之一。"
        "不得决定仓位或交易动作。"
        "take_profit是基于Fib回撤和当前结构的止盈建议，无可靠依据时必须为null。"
        "stop_loss是基于持仓方向、强平价和当前波动的止损建议价格，无可靠依据时必须为null。"
        "对于空单：stop_loss必须高于当前标记价且低于强平价，take_profit必须低于当前标记价。"
        "对于多单：stop_loss必须低于当前标记价且高于强平价，take_profit必须高于当前标记价。"
    )


def independent_risk_analysis(position: dict[str, Any], fib: dict[str, Any], news: dict[str, Any]) -> dict[str, Any]:
    """Second, independently provisioned risk opinion required before an active close.

    Endpoint, key and model come only from the dedicated INDEPENDENT_RISK_* keys so
    this source can never reuse the main GPT transport. It additionally refuses to
    run when it would share the main model's endpoint or credential. When it is not
    configured the source stays unavailable and the deterministic gate holds the
    position (fail-closed).
    """
    key = os.getenv("INDEPENDENT_RISK_API_KEY", "")
    base = os.getenv("INDEPENDENT_RISK_API_BASE", "")
    model_id = os.getenv("INDEPENDENT_RISK_MODEL", INDEPENDENT_RISK_SOURCE_ID)
    if not key or key == "[REDACTED]" or not base or base == "[REDACTED]":
        return {"available": False, "source": INDEPENDENT_RISK_SOURCE_ID, "model": model_id,
                "error": "独立风险来源未配置"}
    main_key = os.getenv("RISK_MODEL_API_KEY", "")
    main_base = os.getenv("RISK_MODEL_API_BASE", "")
    if main_key and key == main_key:
        return {"available": False, "source": INDEPENDENT_RISK_SOURCE_ID, "model": model_id,
                "error": "独立风险来源与主风险模型共用凭据"}
    if main_base and base.rstrip("/") == main_base.rstrip("/"):
        return {"available": False, "source": INDEPENDENT_RISK_SOURCE_ID, "model": model_id,
                "error": "独立风险来源与主风险模型端点相同"}
    try:
        content = _chat(base, key, model_id, _independent_risk_system_prompt(),
                        {"position": position, "fib": fib, "news": news})
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("模型输出不是对象")
        return {"available": True, "source": INDEPENDENT_RISK_SOURCE_ID, "model": model_id,
                "as_of": datetime.now(timezone.utc).isoformat(), **result}
    except HTTPError as exc:
        return {"available": False, "source": INDEPENDENT_RISK_SOURCE_ID, "model": model_id,
                "error": f"独立风险来源不可用：HTTP {exc.code}"}
    except (OSError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        return {"available": False, "source": INDEPENDENT_RISK_SOURCE_ID, "model": model_id,
                "error": f"独立风险来源不可用：{type(exc).__name__}"}
