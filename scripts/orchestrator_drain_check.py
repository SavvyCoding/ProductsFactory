#!/usr/bin/env python3
"""Pre-restart drain check for the pf-orchestrator container.

Exits 0 if no agent sessions are running (safe to restart).
Exits 1 if sessions are in flight (restart would orphan their post-coder runs).
Exits 2 if the PM API is unreachable.

Why this matters: the orchestrator's post-coder pipeline (lint, test, push,
PR-open, feature PATCH to Reviewing) runs inside the orchestrator process. If
the container restarts mid-session, the agent's exit goes unobserved and the
feature gets stranded in Implementing until reset_stuck fires (~1 hour later).
This was the cause of the 2026-05-20 #626 incident.

Usage:
  python scripts/orchestrator_drain_check.py                # check, exit non-zero if busy
  python scripts/orchestrator_drain_check.py --wait 600     # poll up to N seconds
  python scripts/orchestrator_drain_check.py --force        # warn and exit 0 anyway

Typical operator workflow:
  python scripts/orchestrator_drain_check.py --wait 600 && \
    docker compose --profile orchestrator up -d --force-recreate orchestrator
"""
import argparse
import os
import sys
import time

import httpx

PM_API_URL = os.environ.get("PM_API_URL", "http://localhost:8080")
POLL_INTERVAL_SECONDS = 10


def list_active_sessions() -> list[dict]:
    try:
        r = httpx.get(f"{PM_API_URL}/api/sessions/active", timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"ERROR: cannot reach PM API at {PM_API_URL}: {e}", file=sys.stderr)
        sys.exit(2)


def print_sessions(sessions: list[dict]) -> None:
    for s in sessions:
        print(
            f"  session {s.get('id')} "
            f"uid={s.get('session_uid')} "
            f"product={s.get('product_id')} "
            f"persona={s.get('persona')} "
            f"status={s.get('status')} "
            f"started={s.get('started_at')}",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refuse orchestrator restart while agent sessions are mid-flight.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--wait", type=int, default=0, metavar="SECONDS",
        help="Poll every 10s for up to N seconds, waiting for drain.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Print warning and exit 0 even if sessions are in flight.",
    )
    args = parser.parse_args()

    sessions = list_active_sessions()
    if not sessions:
        print("orchestrator drain: 0 active sessions -- safe to restart.")
        return 0

    if args.force:
        print(
            f"orchestrator drain: {len(sessions)} active session(s); --force "
            f"specified, proceeding anyway:", file=sys.stderr,
        )
        print_sessions(sessions)
        return 0

    if args.wait > 0:
        deadline = time.monotonic() + args.wait
        while True:
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                break
            print(
                f"orchestrator drain: {len(sessions)} session(s) in flight; "
                f"polling every {POLL_INTERVAL_SECONDS}s ({remaining}s remaining)",
                file=sys.stderr,
            )
            print_sessions(sessions)
            time.sleep(POLL_INTERVAL_SECONDS)
            sessions = list_active_sessions()
            if not sessions:
                print("orchestrator drain: drained successfully.")
                return 0
        print(
            f"orchestrator drain: TIMEOUT after {args.wait}s -- "
            f"{len(sessions)} session(s) still active. Re-run with a longer "
            f"--wait or pass --force to override.",
            file=sys.stderr,
        )
        print_sessions(sessions)
        return 1

    print(
        f"orchestrator drain: REFUSING -- {len(sessions)} active session(s). "
        f"Pass --wait <seconds> to poll for drain or --force to override.",
        file=sys.stderr,
    )
    print_sessions(sessions)
    return 1


if __name__ == "__main__":
    sys.exit(main())
