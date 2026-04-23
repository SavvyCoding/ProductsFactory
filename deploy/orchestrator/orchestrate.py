#!/usr/bin/env python3
"""
Direct orchestration loop for ProductFactory.

Calls run_cycle() every 60s to check for work and launch agent containers.
Deterministic — no LLM reasoning. Claude is reserved for agent personas
(coder, designer, reviewer) where reasoning adds value.
"""

import json
import logging
import os
import sys
import time

# The orchestrator package lives at /app/orchestrator inside the container.
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

import orchestrator_runtime as tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s orchestrate: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("orchestrate")

CYCLE_INTERVAL = int(os.environ.get("ORCHESTRATION_CYCLE_SECONDS", "60"))


def run_orchestration_loop():
    log.info("ProductFactory orchestrator starting (cycle: %ds)", CYCLE_INTERVAL)

    while True:
        try:
            log.info("=== cycle start ===")
            result_json = tools.run_cycle({})
            result = json.loads(result_json)

            if result.get("ok"):
                data = result.get("data", {})
                action = data.get("action")
                reason = data.get("reason", "")
                log.info("action=%s reason=%s", action, reason)

                if action == "409_stop":
                    log.warning("Another orchestrator holds the lock. Exiting.")
                    sys.exit(0)
                elif action == "launched":
                    product_id = data.get("product_id")
                    persona = data.get("persona")
                    log.info("launched session: product=%s persona=%s", product_id, persona)
            else:
                log.error("run_cycle failed: %s", result.get("error", "unknown"))

        except Exception:
            log.exception("unhandled error in orchestration cycle")

        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run_orchestration_loop()
