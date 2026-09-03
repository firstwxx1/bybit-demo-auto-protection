"""Independent OpenAI-compatible adapters for Grok and GPT risk analysis."""
from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree


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
            "headline, published_at as an ISO-8601 timestamp, and source_url. "
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


def _rss_news(instrument: str) -> dict[str, Any]:
    """Collect publisher RSS evidence when the Grok proxy ignores x_search."""
    urls = [url.strip() for url in os.getenv("NEWS_RSS_URLS", "").split(",") if url.strip()]
    if not urls:
        return {"available": False, "summary": "RSS未配置", "items": [], "evidence_verified": False}
    symbol = instrument.split("-")[0].lower()
    now = datetime.now(timezone.utc)
    max_age = float(os.getenv("NEWS_MAX_AGE_SECONDS", "86400"))
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
                    items.append({"headline": title, "published_at": published.astimezone(timezone.utc).isoformat(), "source_url": urljoin(feed_url, link)})
        except (OSError, ValueError, ElementTree.ParseError):
            continue
    unique = {item["source_url"]: item for item in items}
    selected = list(unique.values())[:5]
    return {"available": bool(selected), "source": "publisher-rss", "model": "deterministic", "as_of": now.isoformat(), "summary": "已通过公开RSS取得最近24小时ETH新闻" if selected else "RSS未取得最近24小时ETH新闻", "items": selected, "evidence_verified": bool(selected)}


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
                                    "必须返回JSON，items中的每条新闻必须包含headline、published_at、source_url。",
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
        content = _chat(base, key, os.getenv("RISK_MODEL", "gpt-5.6-sol"), system,
                        {"position": position, "fib": fib, "news": news})
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("模型输出不是对象")
        return {"available": True, "source": "gpt-5.6-sol", "model": "gpt-5.6-sol",
                "as_of": datetime.now(timezone.utc).isoformat(), **result}
    except HTTPError as exc:
        return {"available": False, "source": "gpt-5.6-sol", "model": "gpt-5.6-sol",
                "error": f"GPT不可用：HTTP {exc.code}"}
    except (OSError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        return {"available": False, "source": "gpt-5.6-sol", "model": "gpt-5.6-sol",
                "error": f"GPT不可用：{type(exc).__name__}"}


def independent_risk_analysis(position: dict[str, Any], fib: dict[str, Any], news: dict[str, Any]) -> dict[str, Any]:
    """A separately injected assessment is mandatory before an active close.

    No default network client is supplied for this source: absent explicit local
    integration it is unavailable and the deterministic gate holds the position.
    """
    del position, fib, news
    return {"available": False, "source": "independent-risk", "error": "独立风险来源未注入"}
