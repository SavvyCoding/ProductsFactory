"""
Regression test for the 2026-05-27 product-23 (CalcV2) incident:
_post_coder_test_check was running pytest without first installing
requirements.txt, so any product import of flask/django/etc. failed
with ModuleNotFoundError → "0 items collected" → bounce on agent
code that actually passed locally.

This file pins the new behavior: the test-check installs the product's
requirements before pytest runs, and a failed install short-circuits
as env_broken (don't bump fix_attempts).
"""

import os
from types import SimpleNamespace

import pytest

# post_coder reads PM_API_URL at import time
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_coder import _post_coder_test_check  # noqa: E402


def _make_recorder(replies):
    """Build a fake _run that records commands and replays canned replies.

    replies is a list of dicts: [{"match": str, "returncode": int,
    "stdout": str, "stderr": str}, ...]. The first reply whose `match`
    substring appears in argv wins for each call.
    """
    calls = []

    def _run(cmd, timeout=None, **kw):
        calls.append({"cmd": list(cmd), "timeout": timeout})
        argv = " ".join(cmd)
        for r in replies:
            if r["match"] in argv:
                return SimpleNamespace(
                    returncode=r.get("returncode", 0),
                    stdout=r.get("stdout", ""),
                    stderr=r.get("stderr", ""),
                )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run, calls


def test_pytest_install_step_runs_before_collect(tmp_path):
    """When requirements.txt is present, pip install runs first."""
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp_path / "requirements.txt").write_text("flask>=3.0,<4\n")

    _run, calls = _make_recorder([
        {"match": "pip install", "returncode": 0},
        {"match": "--collect-only", "returncode": 0,
         "stdout": "collected 5 items"},
        {"match": "pytest -q", "returncode": 0,
         "stdout": "5 passed"},
    ])
    result = _post_coder_test_check(str(tmp_path), _run, product_name="t")

    assert result["passed"] is True
    assert result["env_broken"] is False
    assert result["framework"] == "pytest"
    # pip install fired before pytest collect
    pip_idx = next(i for i, c in enumerate(calls) if "pip" in c["cmd"][0])
    collect_idx = next(
        i for i, c in enumerate(calls)
        if "pytest" in c["cmd"][0] and "--collect-only" in c["cmd"]
    )
    assert pip_idx < collect_idx, "pip install must run before pytest --collect-only"


def test_pip_install_failure_is_env_broken(tmp_path):
    """Failed pip install does not bounce the agent — it's an env issue."""
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp_path / "requirements.txt").write_text("nonexistent-pkg-xyz\n")

    _run, calls = _make_recorder([
        {"match": "pip install", "returncode": 1,
         "stderr": "ERROR: No matching distribution found for nonexistent-pkg-xyz"},
    ])
    result = _post_coder_test_check(str(tmp_path), _run, product_name="t")

    assert result["env_broken"] is True
    assert result["passed"] is False
    assert "pip install" in result["first_failure"]
    # pytest never ran (no pytest call recorded)
    assert not any("pytest" in c["cmd"][0] for c in calls)


def test_no_requirements_file_skips_install(tmp_path):
    """A product without requirements.txt just goes straight to pytest."""
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    # NO requirements.txt

    _run, calls = _make_recorder([
        {"match": "--collect-only", "returncode": 0,
         "stdout": "collected 1 item"},
        {"match": "pytest -q", "returncode": 0, "stdout": "1 passed"},
    ])
    result = _post_coder_test_check(str(tmp_path), _run, product_name="t")

    assert result["passed"] is True
    # No pip install call was made
    assert not any("pip" in c["cmd"][0] for c in calls)


def test_requirements_dev_also_installed(tmp_path):
    """Both requirements.txt and requirements-dev.txt are installed."""
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest-mock\n")

    _run, calls = _make_recorder([
        {"match": "pip install", "returncode": 0},
        {"match": "--collect-only", "returncode": 0,
         "stdout": "collected 1 item"},
        {"match": "pytest -q", "returncode": 0, "stdout": "1 passed"},
    ])
    _post_coder_test_check(str(tmp_path), _run, product_name="t")

    pip_calls = [c for c in calls if "pip" in c["cmd"][0]]
    assert len(pip_calls) == 2
    req_files = [c["cmd"][-1] for c in pip_calls]
    assert "requirements.txt" in req_files
    assert "requirements-dev.txt" in req_files


def test_non_python_stacks_skip_install(tmp_path):
    """npm / go products never trigger the pip install path."""
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest --coverage"}}'
    )

    _run, calls = _make_recorder([
        {"match": "npm test", "returncode": 0, "stdout": "All passed"},
    ])
    result = _post_coder_test_check(str(tmp_path), _run, product_name="t")

    assert result["framework"] == "npm"
    assert not any("pip" in c["cmd"][0] for c in calls)
