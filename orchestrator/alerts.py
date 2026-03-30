"""
Alert delivery — sends webhook (Slack/Teams) + email fallback.
Failures are logged; never raise to caller.
"""

import os
import logging
import httpx

log = logging.getLogger("poller.alerts")

ALERT_WEBHOOK_URL    = os.environ.get("ALERT_WEBHOOK_URL", "")
WEBHOOK_FAIL_COUNTER = 0
WEBHOOK_FAIL_MAX     = 3


def send_alert(level: str, message: str, product_name: str = ""):
    """
    level: info | warning | error | critical
    Logs always. Delivers to webhook. Falls back to email after 3 webhook failures.
    """
    global WEBHOOK_FAIL_COUNTER

    prefix = {"info": "[INFO]", "warning": "[WARN]", "error": "[ERROR]", "critical": "[CRITICAL]"}.get(level, "[ALERT]")
    full_message = f"{prefix} ProductFactory [{level.upper()}]: {message}"
    if product_name:
        full_message = f"{prefix} [{product_name}] {message}"

    log.info(f"ALERT: {full_message}")

    if not ALERT_WEBHOOK_URL:
        return

    if WEBHOOK_FAIL_COUNTER >= WEBHOOK_FAIL_MAX:
        _send_email_fallback(full_message)
        return

    try:
        resp = httpx.post(
            ALERT_WEBHOOK_URL,
            json={"text": full_message},
            timeout=10,
        )
        if resp.status_code not in (200, 201, 202, 204):
            WEBHOOK_FAIL_COUNTER += 1
            log.warning(f"Webhook delivery failed ({resp.status_code}) — fail count: {WEBHOOK_FAIL_COUNTER}")
        else:
            WEBHOOK_FAIL_COUNTER = 0  # reset on success
    except Exception as e:
        WEBHOOK_FAIL_COUNTER += 1
        log.warning(f"Webhook exception: {e} — fail count: {WEBHOOK_FAIL_COUNTER}")


def _send_email_fallback(message: str):
    """Fallback email when webhook fails 3× in a row. TODO: implement SMTP."""
    log.warning(f"Email fallback (not yet implemented): {message}")
    # TODO: use smtplib to send email to PM_EMAIL env var
