"""Architect-cadence main-suite health check.

Runs the product's test suite on `main` inside the agent container; when it is
RED with REAL failures (not env-broken), files ONE deduped corrective chore plus
a dashboard alert. Closes the baseline-delta blind spot: post-coder's
`pre_existing_only` path lets a feature ship over a red `main` and only *logs*
"operator/chore must fix" — a line consumed nowhere. This turns it into an owned
chore + PM-visible alert.

Design (see docs/specs/agent_environment_provisioning.md sibling discussion):
  - **Surface, don't block.** Never gates shipping — baseline-delta is unchanged;
    this only assigns the red. (Hard-blocking would resurrect the calc3 mass-Block
    deadlock.)
  - **Deterministic.** The verdict is pytest's exit code + parsed failing IDs, not
    LLM judgment.
  - **Architect cadence.** Invoked from run_cycle when the architect is queued
    (every ~3 Pushed), out-of-band in a daemon thread so the cycle never blocks.
  - **Deduped.** Reuses drift_detectors.file_corrective_chores; the chore key is
    stable per product (`broken_main_suite:{id}`), so one open chore tracks "main
    is red" regardless of which tests fail — no spam. The alert fires only on the
    transition (when a NEW chore is actually filed).
"""
from __future__ import annotations

import logging
import os
import re as _re
import subprocess as _sp
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_IDS = 25


def _run_suite_on_main(working_dir: str, timeout: int = 300) -> dict:
    """Run the suite on `main` in the agent container, WITHOUT touching the live
    workdir: clone the repo to /tmp inside the container, check out main, install,
    run pytest. Returns {status, failing_ids, output} where status is one of
    passed | env_broken | failed | skipped. Best-effort; never raises."""
    wd = Path(working_dir)
    if not (wd / "pytest.ini").exists() and not (wd / "pyproject.toml").exists():
        return {"status": "skipped", "failing_ids": [], "output": "no pytest config"}
    try:
        from orchestrator.pipelines.post_coder import (
            _agent_container_base, _all_failing_tests, _ENV_BROKEN_PATTERNS,
        )
        prefix, install = _agent_container_base(working_dir)
    except Exception as e:
        return {"status": "skipped", "failing_ids": [], "output": f"setup failed: {e}"}

    inst = f"{install} >/dev/null 2>&1; " if install else ""
    # Clone the COMMITTED state (consistent even if a coder session is mid-edit
    # in the live /workspace), check out main, install in the clone, run pytest.
    script = (
        'git config --global --add safe.directory "*" 2>/dev/null; '
        'rm -rf /tmp/_msh; git clone -q /workspace /tmp/_msh 2>/dev/null && cd /tmp/_msh && '
        '(git checkout -q origin/main 2>/dev/null || git checkout -q main 2>/dev/null) && '
        f'{inst}'
        f'timeout {timeout}s python -m pytest -q -p no:cacheprovider -o cache_dir=/tmp/_pc 2>&1'
    )
    try:
        r = _sp.run(prefix + [script], capture_output=True, text=True, timeout=timeout + 180)
    except Exception as e:
        return {"status": "skipped", "failing_ids": [], "output": f"run failed: {e}"}

    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if r.returncode == 0:
        return {"status": "passed", "failing_ids": [], "output": out[-400:]}
    env_re = _re.compile("|".join(_ENV_BROKEN_PATTERNS))
    if env_re.search(out) or "isn't writable" in out or "cannot rehash" in out:
        return {"status": "env_broken", "failing_ids": [], "output": out[-400:]}
    return {"status": "failed", "failing_ids": _all_failing_tests(out), "output": out[-1200:]}


def detect_broken_main_suite(product: dict, pm_client=None, *, dry_run: bool = False) -> dict:
    """File a deduped chore + alert when `main`'s suite is red. Returns
    {status, filed_chore}. Best-effort; never raises into the cycle."""
    if os.environ.get("MAIN_SUITE_HEALTH_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        return {"status": "disabled", "filed_chore": False}
    pid = product.get("id")
    working_dir = product.get("working_dir") or ""
    pname = product.get("name") or str(pid)
    if not pid or not working_dir:
        return {"status": "skipped", "filed_chore": False}

    res = _run_suite_on_main(working_dir)
    status = res["status"]
    if status != "failed":
        # passed / env_broken / skipped → nothing to own. (Closing a stale open
        # chore on `passed` is a future nicety; the coder marks it Pushed when fixed.)
        log.info("[main-suite-health] %s: main suite status=%s", pname, status)
        return {"status": status, "filed_chore": False}

    failing = res["failing_ids"]
    log.warning("[main-suite-health] %s: main suite RED — %d failing test(s)", pname, len(failing))
    if dry_run:
        return {"status": "failed", "filed_chore": False, "failing_ids": failing}

    try:
        from orchestrator.drift_detectors import Finding, file_corrective_chores
    except Exception:
        return {"status": "failed", "filed_chore": False, "failing_ids": failing}

    n = len(failing)
    finding = Finding(
        category="broken_main_suite",
        severity="high",
        target_type="product",
        target_id="main",
        feature_id=0,
        detail=(
            f"The test suite is RED on `main` — {n} failing test(s). Features keep "
            f"shipping over it because the post-coder baseline-delta won't blame a "
            f"single feature for pre-existing failures, so the red persists unowned. "
            f"This chore assigns the fix."
        ),
        fix_hint=(
            "Run the suite on main and make it green by fixing the listed tests (or "
            "the code they cover). Do NOT lower/disable gates or skip tests to hide "
            "failures — the reviewer and lint-guard catch that."
        ),
        occurrences=failing[:_MAX_IDS],
        product_id=int(pid),
        dedupe_key=f"broken_main_suite:{pid}",  # stable per product → one open chore
    )

    filed = 0
    try:
        if pm_client is not None:
            filed = file_corrective_chores([finding], pm_client, int(pid), pname)
            if filed:
                _post_alert(pm_client, pname, n)
        else:
            import httpx
            with httpx.Client(base_url=os.environ["PM_API_URL"], timeout=20) as c:
                filed = file_corrective_chores([finding], c, int(pid), pname)
                if filed:
                    _post_alert(c, pname, n)
    except Exception:
        log.exception("[main-suite-health] %s: filing chore/alert failed", pname)

    return {"status": "failed", "filed_chore": bool(filed), "failing_ids": failing}


def _post_alert(client, product_name: str, n: int) -> None:
    """Dashboard alert — fired only when a NEW chore was filed (transition into
    red), so the PM gets one notification, not one per architect cadence."""
    try:
        client.post("/api/alerts", json={
            "level": "warning",
            "message": f"main suite RED: {n} test(s) failing on {product_name} — corrective chore filed",
        })
    except Exception:
        log.warning("[main-suite-health] alert POST failed for %s", product_name)
