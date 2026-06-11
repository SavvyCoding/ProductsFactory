"""Architect-cadence fresh-environment install check (A4.2, 2026-06-10).

Answers the one question the container test gate structurally cannot: does a
FRESH clone of this product install and collect from requirements.txt alone?
The agent image pre-bakes common packages (bcrypt, httpx, pytest plugins), so
the post-coder test gate runs against a warm environment — a missing
declaration is invisible until someone installs from scratch. Canonical
incidents: MyJira 2026-06-09 (passlib without bcrypt; fastapi.testclient
without httpx — fresh `pip install -r requirements.txt && pytest` fails on
collect), the original 2026-05-22 MyDocusign Guard-18 incident one level
deeper than Guard 18's import walk can see.

Design mirrors main_suite_health (the sibling architect-cadence check):
  - **Surface, don't block.** Never gates shipping; files ONE deduped chore
    (`fresh_install:{product_id}`) + a dashboard alert on the transition.
  - **Deterministic.** Verdict = pip's / pytest-collect's exit code inside a
    `python -m venv` (no system site-packages) in the agent container.
  - **Architect cadence**, out-of-band in a daemon thread from run_cycle.
  - Python-first: skips products without requirements.txt.

The static sibling `drift_detectors.detect_undeclared_backend_deps` catches
the KNOWN backend pairs every cycle for free; this check is the exhaustive
slow-path that catches the pairs nobody enumerated.
"""
from __future__ import annotations

import logging
import os
import subprocess as _sp
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_LINES = 25


def _run_fresh_install(working_dir: str, timeout: int = 600) -> dict:
    """Clone the committed state to /tmp inside the agent container, create a
    CLEAN venv (isolated from the image's pre-baked site-packages), install
    requirements.txt, then `pytest --collect-only`. Returns {status, output}
    where status is one of passed | install_failed | collect_failed | skipped.
    Best-effort; never raises."""
    wd = Path(working_dir)
    if not (wd / "requirements.txt").exists():
        return {"status": "skipped", "output": "no requirements.txt"}
    try:
        from orchestrator.pipelines.post_coder import _agent_container_base
        prefix, _install = _agent_container_base(working_dir)
    except Exception as e:
        return {"status": "skipped", "output": f"setup failed: {e}"}

    # Sentinel between the install and collect stages so a single combined
    # run still yields an unambiguous two-stage verdict (same trick the
    # post-coder timeout classification wants; here it's load-bearing).
    script = (
        'git config --global --add safe.directory "*" 2>/dev/null; '
        'rm -rf /tmp/_fec; git clone -q /workspace /tmp/_fec 2>/dev/null && cd /tmp/_fec && '
        '(git checkout -q origin/main 2>/dev/null || git checkout -q main 2>/dev/null); '
        'python -m venv /tmp/_fecv && '
        f'timeout {timeout - 120}s /tmp/_fecv/bin/pip install -q -r requirements.txt 2>&1 && '
        'echo __PF_INSTALL_OK__ && '
        'timeout 120s /tmp/_fecv/bin/python -m pytest --collect-only -q '
        '-p no:cacheprovider -o cache_dir=/tmp/_fpc 2>&1'
    )
    try:
        r = _sp.run(prefix + [script], capture_output=True, text=True, timeout=timeout + 120)
    except Exception as e:
        return {"status": "skipped", "output": f"run failed: {e}"}

    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if r.returncode == 0:
        return {"status": "passed", "output": out[-400:]}
    if "__PF_INSTALL_OK__" not in out:
        return {"status": "install_failed", "output": out[-1500:]}
    return {"status": "collect_failed", "output": out[-1500:]}


def detect_broken_fresh_install(product: dict, pm_client=None, *, dry_run: bool = False) -> dict:
    """File a deduped chore + alert when a fresh install/collect of `main`
    fails. Returns {status, filed_chore}. Best-effort; never raises into the
    cycle."""
    if os.environ.get("FRESH_ENV_CHECK_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        return {"status": "disabled", "filed_chore": False}
    pid = product.get("id")
    working_dir = product.get("working_dir") or ""
    pname = product.get("name") or str(pid)
    if not pid or not working_dir:
        return {"status": "skipped", "filed_chore": False}

    res = _run_fresh_install(working_dir)
    status = res["status"]
    if status in ("passed", "skipped"):
        log.info("[fresh-env-check] %s: fresh install status=%s", pname, status)
        return {"status": status, "filed_chore": False}

    stage = "pip install -r requirements.txt" if status == "install_failed" \
        else "pytest --collect-only (after a clean install)"
    log.warning("[fresh-env-check] %s: fresh environment broken at `%s`", pname, stage)
    if dry_run:
        return {"status": status, "filed_chore": False}

    try:
        from orchestrator.drift_detectors import Finding, file_corrective_chores
    except Exception:
        return {"status": status, "filed_chore": False}

    tail = [ln for ln in res["output"].splitlines() if ln.strip()][-_MAX_LINES:]
    finding = Finding(
        category="broken_fresh_install",
        severity="high",
        target_type="product",
        target_id="requirements.txt",
        feature_id=0,
        detail=(
            f"A FRESH clone of this product fails at `{stage}` — the declared "
            "dependencies cannot reproduce a working environment. The agent "
            "image pre-bakes common packages, so the in-container test gate "
            "passes while every fresh install (CI, contributor, deploy) fails."
        ),
        fix_hint=(
            "Reproduce with: python -m venv v && v/bin/pip install -r "
            "requirements.txt && v/bin/python -m pytest --collect-only. Add the "
            "missing/implicit packages to requirements.txt (watch for backend "
            "deps: passlib needs bcrypt, fastapi.testclient needs httpx). Do "
            "NOT fix by importing less in tests."
        ),
        occurrences=tail,
        product_id=int(pid),
        dedupe_key=f"fresh_install:{pid}",  # stable per product → one open chore
    )

    filed = 0
    try:
        if pm_client is not None:
            filed = file_corrective_chores([finding], pm_client, int(pid), pname)
            if filed:
                _post_alert(pm_client, pname, stage)
        else:
            import httpx
            with httpx.Client(base_url=os.environ["PM_API_URL"], timeout=20) as c:
                filed = file_corrective_chores([finding], c, int(pid), pname)
                if filed:
                    _post_alert(c, pname, stage)
    except Exception:
        log.exception("[fresh-env-check] %s: filing chore/alert failed", pname)

    return {"status": status, "filed_chore": bool(filed)}


def _post_alert(client, product_name: str, stage: str) -> None:
    """Dashboard alert — only on the transition (when a NEW chore was filed)."""
    try:
        client.post("/api/alerts", json={
            "level": "warning",
            "message": (
                f"fresh environment broken on {product_name}: fails at `{stage}` "
                "— corrective chore filed"
            ),
        })
    except Exception:
        log.warning("[fresh-env-check] alert POST failed for %s", product_name)
