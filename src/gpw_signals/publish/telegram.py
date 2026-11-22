"""Publikacja na kanał Telegram przez Bot API (bot musi być adminem kanału)."""
from __future__ import annotations

import logging
import time

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


class Publisher:
    def send(self, text: str, reply_to: int | None = None) -> int | None: ...


class DryRunPublisher(Publisher):
    def send(self, text: str, reply_to: int | None = None) -> int | None:
        print("─" * 40 + "\n" + text + "\n" + "─" * 40)
        return None


class TelegramPublisher(Publisher):
    API = "https://api.telegram.org/bot{token}/{method}"
    MIN_INTERVAL_S = 3.1  # Telegram: ~20 wiadomości/min na grupę/kanał

    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise ValueError("Brak TELEGRAM_BOT_TOKEN lub ID kanału w .env")
        self.token, self.chat_id = token, chat_id
        self._last = 0.0

    @retry(retry=retry_if_exception_type(httpx.TransportError), stop=stop_after_attempt(4),
           wait=wait_exponential(min=2, max=30), reraise=True)
    def _call(self, method: str, payload: dict) -> dict:
        r = httpx.post(self.API.format(token=self.token, method=method), json=payload, timeout=20)
        data = r.json()
        if r.status_code == 429:  # flood control – Telegram mówi, ile czekać
            time.sleep(int(data.get("parameters", {}).get("retry_after", 5)))
            raise httpx.TransportError("429 retry")
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} error: {data.get('description')}")
        return data["result"]

    def send(self, text: str, reply_to: int | None = None) -> int | None:
        wait = self.MIN_INTERVAL_S - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = self._call("sendMessage", payload)
        self._last = time.time()
        log.info("Wysłano na %s (msg %s)", self.chat_id, result.get("message_id"))
        return result.get("message_id")
