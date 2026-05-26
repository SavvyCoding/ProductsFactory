"""
Alert delivery — sends to Slack/Teams webhook.
Webhook URL priority: DB system_config > ALERT_WEBHOOK_URL env var.
Failures are logged; never raise to caller.
"""

import os
import time
import logging
import threading
import httpx

log = logging.getLogger("poller.alerts")

ALERT_WEBHOOK_URL    = os.environ.get("ALERT_WEBHOOK_URL", "")
PM_API_URL           = os.environ.get("PM_API_URL", "")
WEBHOOK_FAIL_MAX     = 3
# After this many seconds the suppression resets and delivery is retried once
WEBHOOK_FAIL_RESET_INTERVAL = 900  # 15 minutes

# Thread-safe counter state
_webhook_lock: threading.Lock = threading.Lock()
_webhook_fail_counter: int = 0
_webhook_suppressed_since: float = 0.0

_cached_webhook_url: str | None = None


def _get_webhook_url() -> str:
    """Fetch webhook URL from DB config, falling back to env var."""
    global _cached_webhook_url
    if _cached_webhook_url is not None:
        return _cached_webhook_url
    if PM_API_URL:
        try:
            resp = httpx.get(f"{PM_API_URL}/api/system-config", timeout=5)
            if "application/json" in resp.headers.get("content-type", ""):
                url = resp.json().get("slack_webhook_url") or ""
                if url:
                    _cached_webhook_url = url
                    return url
        except Exception:
            pass
    _cached_webhook_url = ALERT_WEBHOOK_URL
    return ALERT_WEBHOOK_URL


def send_alert(level: str, message: str, product_name: str = ""):
    """
    level: info | warning | error | critical
    Logs always. Delivers to Slack webhook if configured.
    Thread-safe: counter/suppression state is protected by _webhook_lock.
    """
    global _webhook_fail_counter, _webhook_suppressed_since

    prefix = {"info": "ℹ️", "warning": "⚠️", "error": "🔴", "critical": "🚨"}.get(level, "📢")
    if product_name:
        full_message = f"{prefix} *[{product_name}]* {message}"
    else:
        full_message = f"{prefix} *ProductFactory* — {message}"

    log.info(f"ALERT ({level}): {message}")

    webhook_url = _get_webhook_url()
    if not webhook_url:
        return

    with _webhook_lock:
        if _webhook_fail_counter >= WEBHOOK_FAIL_MAX:
            # Auto-reset after cooldown so delivery is retried once the webhook recovers
            if time.time() - _webhook_suppressed_since >= WEBHOOK_FAIL_RESET_INTERVAL:
                log.info("Webhook suppression cooldown expired — retrying delivery")
                _webhook_fail_counter = 0
            else:
                log.warning("Webhook failing repeatedly — suppressing delivery until cooldown expires")
                return

    try:
        resp = httpx.post(
            webhook_url,
            json={"text": full_message},
            timeout=10,
        )
        with _webhook_lock:
            if resp.status_code not in (200, 201, 202, 204):
                _webhook_fail_counter += 1
                if _webhook_fail_counter == 1:
                    _webhook_suppressed_since = time.time()
                log.warning(f"Webhook delivery failed ({resp.status_code}) — fail count: {_webhook_fail_counter}")
            else:
                _webhook_fail_counter = 0
    except Exception as e:
        with _webhook_lock:
            _webhook_fail_counter += 1
            if _webhook_fail_counter == 1:
                _webhook_suppressed_since = time.time()
        log.warning(f"Webhook exception: {e} — fail count: {_webhook_fail_counter}")
