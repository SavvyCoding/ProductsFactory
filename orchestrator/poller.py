"""
ProductFactory Poller — main loop (Windows localhost).

Runs as a Windows Service via NSSM.
Poll interval: 60s when idle, 5s between products.

Flow per cycle:
  1. Auth health check (real API call, not --version)
  2. Discover any newly-registered folders
  3. Heartbeat — kill stale containers (progress.md not pushed in >45m)
  4. Reset stuck features (Implementing > 2h)
  5. Pick next product (ORDER BY last_run_at ASC)
  6. PR count gate (≥3 open PRs → pause product)
  7. GitHub PR reconciliation (sync merged PRs → DB)
  8. Run Claude in Docker (blocks until session ends)
  9. Update last_run_at ONLY on clean exit (exit code 0)
"""

import os
import time
import logging
import subprocess
from datetime import datetime, timezone

import httpx

from orchestrator.setup_product import discover_and_populate
from orchestrator.docker_runner import run_claude_in_docker
from orchestrator.github_client import count_open_prs, reconcile_merged_prs
from orchestrator.heartbeat import check_stale_sessions
from orchestrator.alerts import send_alert

PM_API_URL = os.environ["PM_API_URL"]          # e.g. https://ubuntu-vm:8080
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
AUTH_CHECK_TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("orchestrator/poller.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("poller")


def claude_auth_healthy() -> bool:
    """
    Real auth check — makes an actual API call.
    claude --version always returns 0 even when logged out. Don't use it.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "ping", "--max-budget-usd", "0.001"],
            timeout=AUTH_CHECK_TIMEOUT,
            capture_output=True,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.warning("Auth check timed out")
        return False
    except FileNotFoundError:
        log.error("'claude' CLI not found in PATH")
        return False


def get_next_product() -> dict | None:
    """Fetch the product with oldest last_run_at that has approved features."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/products/next")
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        log.error(f"get_next_product failed: {e}")
        return None


def reset_stuck_features():
    """Reset features stuck in Implementing for >2h back to Approved."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/features/reset_stuck")
        resp.raise_for_status()
        data = resp.json()
        count = data.get("reset_count", 0)
        if count:
            log.info(f"Reset {count} stuck feature(s) from Implementing → Approved")
    except httpx.HTTPError as e:
        log.error(f"reset_stuck_features failed: {e}")


def main():
    log.info("ProductFactory Poller starting...")

    while True:
        try:
            # ① Auth check
            if not claude_auth_healthy():
                send_alert("critical", "Claude OAuth session expired — re-login needed")
                log.warning("Auth unhealthy — skipping cycle")
                time.sleep(POLL_INTERVAL)
                continue

            # ② Discover new products
            with httpx.Client(base_url=PM_API_URL) as client:
                products = client.get("/api/products").json()

            for product in products:
                if product["status"] == "registered":
                    log.info(f"Discovering product: {product['working_dir']}")
                    discover_and_populate(product)

            # ③ Heartbeat — kill stale containers
            check_stale_sessions(products)

            # ④ Reset stuck features
            reset_stuck_features()

            # ⑤ Pick next product
            product = get_next_product()
            if not product:
                log.debug("No products ready — sleeping")
                time.sleep(POLL_INTERVAL)
                continue

            log.info(f"Selected product: {product['name']} (id={product['id']})")

            # ⑥ PR count gate
            open_pr_count = count_open_prs(product)
            if open_pr_count >= 3:
                log.info(f"PR gate: {open_pr_count} open PRs — skipping, waiting for PM to merge")
                send_alert("warning", f"{product['name']}: ≥3 open PRs unmerged — pausing until merged")
                time.sleep(300)
                continue

            # ⑦ GitHub PR reconciliation
            reconcile_merged_prs(product)

            # ⑧ Run Claude session
            log.info(f"Launching Claude session for: {product['name']}")
            exit_code = run_claude_in_docker(product)
            log.info(f"Session ended — exit_code={exit_code}")

            # ⑨ Update last_run_at ONLY on clean exit
            if exit_code == 0:
                with httpx.Client(base_url=PM_API_URL) as client:
                    client.patch(
                        f"/api/products/{product['id']}",
                        json={"last_run_at": datetime.now(timezone.utc).isoformat()},
                    )

        except KeyboardInterrupt:
            log.info("Poller stopped by user")
            break
        except Exception as e:
            log.exception(f"Unexpected error in poll loop: {e}")
            send_alert("error", f"Poller loop error: {e}")

        time.sleep(5)


if __name__ == "__main__":
    main()
