"""Continuum notification dispatcher.

Sends task-complete and checkpoint alerts to Telegram, Discord, or any webhook.
Designed to fire from the daemon when a task finishes — so you get a message on
your phone while Claude is working in the background.

Supported channels:
  telegram  — bot token + chat_id  (env: CONTINUUM_TG_TOKEN, CONTINUUM_TG_CHAT)
  discord   — incoming webhook URL (env: CONTINUUM_DISCORD_WEBHOOK)
  webhook   — any URL, POST JSON  (env: CONTINUUM_WEBHOOK_URL)
  slack     — incoming webhook URL (env: CONTINUUM_SLACK_WEBHOOK)

All channels are opt-in via environment variables.  Zero config = no notifications,
no errors, no noise.

Notification config can also be stored per-project in the DB (NotifyConfig model)
and overridden at call time.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Config — assembled from env vars, overridable at call time
# ---------------------------------------------------------------------------

@dataclass
class NotifyConfig:
    """Notification channel configuration.

    All fields are optional.  Only channels with the required fields populated
    will fire.  Store per-project in the DB via notify_config column in
    project_configs, or pass directly to notify().
    """
    telegram_token:    Optional[str] = None
    telegram_chat_id:  Optional[str] = None
    discord_webhook:   Optional[str] = None
    webhook_url:       Optional[str] = None
    slack_webhook:     Optional[str] = None
    # If True, only fire on failure/error
    errors_only: bool = False

    @classmethod
    def from_env(cls) -> "NotifyConfig":
        return cls(
            telegram_token   = os.environ.get("CONTINUUM_TG_TOKEN"),
            telegram_chat_id = os.environ.get("CONTINUUM_TG_CHAT"),
            discord_webhook  = os.environ.get("CONTINUUM_DISCORD_WEBHOOK"),
            webhook_url      = os.environ.get("CONTINUUM_WEBHOOK_URL"),
            slack_webhook    = os.environ.get("CONTINUUM_SLACK_WEBHOOK"),
        )

    def has_any_channel(self) -> bool:
        return any([
            self.telegram_token and self.telegram_chat_id,
            self.discord_webhook,
            self.webhook_url,
            self.slack_webhook,
        ])


# ---------------------------------------------------------------------------
# Internal helpers — one function per channel
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict, timeout: int = 8) -> bool:
    """POST JSON to a URL.  Returns True on 2xx, False otherwise."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram message limit is 4096 chars
    return _post_json(url, {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "Markdown",
    })


def _send_discord(webhook_url: str, message: str, title: str = "") -> bool:
    # Discord embed for a cleaner look
    payload: dict = {
        "embeds": [{
            "title": title or "Continuum",
            "description": message[:4096],
            "color": 0x6366f1,  # indigo
        }]
    }
    return _post_json(webhook_url, payload)


def _send_slack(webhook_url: str, message: str) -> bool:
    return _post_json(webhook_url, {"text": message[:3000]})


def _send_generic_webhook(url: str, payload: dict) -> bool:
    return _post_json(url, payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class NotifyResult:
    """Outcome of a notify() call — which channels fired, which failed."""
    fired: list[str]   = field(default_factory=list)
    failed: list[str]  = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def any_sent(self) -> bool:
        return bool(self.fired)

    def summary(self) -> str:
        parts = []
        if self.fired:   parts.append(f"sent via {', '.join(self.fired)}")
        if self.failed:  parts.append(f"failed: {', '.join(self.failed)}")
        if self.skipped: parts.append(f"skipped (not configured): {', '.join(self.skipped)}")
        return "; ".join(parts) if parts else "no channels configured"


def notify(
    title: str,
    message: str,
    config: Optional[NotifyConfig] = None,
    extra: Optional[dict] = None,
) -> NotifyResult:
    """Fire notifications to all configured channels.

    Args:
        title:   Short headline — used as Telegram bold prefix and Discord embed title
        message: Body text.  Markdown is supported on Telegram and Discord.
        config:  NotifyConfig override.  If None, reads from environment.
        extra:   Extra key/value pairs included in the generic webhook payload.

    Returns:
        NotifyResult with .fired, .failed, .skipped lists.
    """
    cfg = config or NotifyConfig.from_env()
    result = NotifyResult()

    if not cfg.has_any_channel():
        return result  # silent no-op

    full_text = f"*{title}*\n{message}" if title else message
    generic_payload = {
        "title": title,
        "message": message,
        **(extra or {}),
    }

    # Telegram
    if cfg.telegram_token and cfg.telegram_chat_id:
        ok = _send_telegram(cfg.telegram_token, cfg.telegram_chat_id, full_text)
        (result.fired if ok else result.failed).append("telegram")
    else:
        result.skipped.append("telegram")

    # Discord
    if cfg.discord_webhook:
        ok = _send_discord(cfg.discord_webhook, message, title=title)
        (result.fired if ok else result.failed).append("discord")
    else:
        result.skipped.append("discord")

    # Slack
    if cfg.slack_webhook:
        ok = _send_slack(cfg.slack_webhook, full_text)
        (result.fired if ok else result.failed).append("slack")
    else:
        result.skipped.append("slack")

    # Generic webhook
    if cfg.webhook_url:
        ok = _send_generic_webhook(cfg.webhook_url, generic_payload)
        (result.fired if ok else result.failed).append("webhook")
    else:
        result.skipped.append("webhook")

    return result


# ---------------------------------------------------------------------------
# Convenience formatters — build good messages from Continuum objects
# ---------------------------------------------------------------------------

def format_task_complete(task_id: str, project: str, summary: str, success: bool) -> tuple[str, str]:
    """Return (title, message) for a task-complete notification."""
    icon  = "✅" if success else "❌"
    title = f"{icon} [{project}] Task complete"
    msg   = f"Task `{task_id[:8]}` finished {'successfully' if success else 'with errors'}.\n\n{summary}"
    return title, msg


def format_checkpoint(project: str, task: str, status: str, checkpoint_id: str) -> tuple[str, str]:
    """Return (title, message) for a checkpoint notification."""
    title = f"📍 [{project}] Checkpoint saved"
    msg   = f"**{task}**\nStatus: {status}\nID: `{checkpoint_id[:8]}`"
    return title, msg


def format_token_warning(project: str, percent_used: int) -> tuple[str, str]:
    """Return (title, message) for a token-budget warning."""
    title = f"⚠️ [{project}] Context at {percent_used}%"
    msg   = (
        f"Context window is {percent_used}% full.\n"
        "A checkpoint has been auto-saved. Consider calling `handoff()` soon."
    )
    return title, msg
