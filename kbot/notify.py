"""Operator notifications: always logged and stored; optionally POSTed to a webhook
(ntfy.sh, Slack/Discord incoming webhook, etc.). Never includes secrets."""
from __future__ import annotations

import logging

import httpx

from .db import DB

log = logging.getLogger("kbot.notify")


class Notifier:
    def __init__(self, db: DB, webhook: str = ""):
        self.db, self.webhook = db, webhook

    def __call__(self, text: str) -> None:
        log.warning("NOTIFY: %s", text)
        self.db.event("notify", text=text)
        if self.webhook:
            try:
                httpx.post(self.webhook, json={"text": f"[kbot] {text}", "content": f"[kbot] {text}"}, timeout=5)
            except Exception as e:  # a failed notification must never stop the bot
                log.error("webhook failed: %s", e)
