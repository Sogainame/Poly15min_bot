"""Telegram notification helper."""
from __future__ import annotations

import httpx
import config


def send_telegram(msg: str) -> None:
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5.0,
        )
    except Exception:
        pass
