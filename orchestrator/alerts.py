"""
Alert delivery — sends to Slack/Teams webhook.
Webhook URL priority: DB system_config > ALERT_WEBHOOK_URL env var.
Failures are logged; never raise to caller.
"""

import os
import logging
import httpx

log = logging.getLogger("poller.alerts")

ALERT_WEBHOOK_URL    = os.environ.get("ALERT_WEBHOOK_URL", "")
PM_API_URL           = os.environ.get("PM_API_URL", "")
WEBHOOK_FAIL_COUNTER = 0
WEBHOOK_FAIL_MAX     = 3

_cached_webhook_url: str | None = None


def _get_webhook_url() -> str:
    """Fetch webhook URL from DB config, falling back to env var."""
    global _cached_webhook_url
    if _cached_webhook_url is not None:
        return _cached_webhook_url
    if PM_API_URL:
        try:
            resp = httpx.get(f"{PM_API_URL}/api/system-config", timeout=5)
            url = resp.json().get("slack_webhook_url") or ""
            if url:
                _cached_webhook_url = url
                return url
        except Exception:
            pass
    _cached_webhook_url = ALERT_WEBHOOK_URL
    return ALERT_WEBHOOK_URL


def invalidate_webhook_cache():
    """Call after admin saves new webhook URL."""
    global _cached_webhook_url
    _cached_webhook_url = None


def send_alert(level: str, message: str, product_name: str = ""):
    """
    level: info | warning | error | critical
    Logs always. Delivers to Slack webhook if configured.
    """
    global WEBHOOK_FAIL_COUNTER

    prefix = {"info": "ℹ️", "warning": "⚠️", "error": "🔴", "critical": "🚨"}.get(level, "📢")
    if product_name:
        full_message = f"{prefix} *[{product_name}]* {message}"
    else:
        full_message = f"{prefix} *ProductFactory* — {message}"

    log.info(f"ALERT ({level}): {message}")

    webhook_url = _get_webhook_url()
    if not webhook_url:
        return

    if WEBHOOK_FAIL_COUNTER >= WEBHOOK_FAIL_MAX:
        log.warning("Webhook failing repeatedly — suppressing delivery")
        return

    try:
        resp = httpx.post(
            webhook_url,
            json={"text": full_message},
            timeout=10,
        )
        if resp.status_code not in (200, 201, 202, 204):
            WEBHOOK_FAIL_COUNTER += 1
            log.warning(f"Webhook delivery failed ({resp.status_code}) — fail count: {WEBHOOK_FAIL_COUNTER}")
        else:
            WEBHOOK_FAIL_COUNTER = 0
    except Exception as e:
        WEBHOOK_FAIL_COUNTER += 1
        log.warning(f"Webhook exception: {e} — fail count: {WEBHOOK_FAIL_COUNTER}")
