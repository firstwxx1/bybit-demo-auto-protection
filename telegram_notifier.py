"""Telegram Bot API notifier for Bybit Demo risk reports.

Sends text messages via the Telegram Bot API. Long messages are automatically
split to respect the 4096-character limit.
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def send_telegram(
    message: str,
    *,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> dict:
    """Send a message via Telegram Bot API. Returns the API response dict.

    If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured, returns
    a dict with ok=False without making a network request.
    """
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured"}

    # Telegram message limit is 4096 chars; split with margin.
    chunks = [message[i : i + 4000] for i in range(0, len(message), 4000)]
    results: list[dict] = []
    for chunk in chunks:
        body = json.dumps(
            {
                "chat_id": chat,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode()
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "bybit-demo-risk-reporter/1.0",
            },
        )
        try:
            with urlopen(
                request, timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
            ) as response:
                results.append(json.load(response))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            results.append({"ok": False, "error": str(exc)})

    if len(results) == 1:
        return results[0]
    return {"ok": all(r.get("ok") for r in results), "results": results}
