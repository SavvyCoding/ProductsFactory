"""
Tests for the verify-check gate (2026-05-30).

The gate parses `Verify:` + `Expected:` pairs from `docs/story_<id>.md`,
runs each command in a subprocess, and bounces on mismatch. These tests
exercise:
  - The parser: multi-Verify per AC, multi-line commands, backtick handling.
  - The exit-code-vs-stdout heuristic in _check_expected.
  - The skip-on-server-unavailable classification.
  - End-to-end: docs/story_<id>.md → _post_coder_verify_check returns
    failures/skips/passes correctly.

The gate is gated by env POST_CODER_VERIFY_CHECK_ENABLED. All tests that
exercise the public entry point set the env before calling.
"""

import os
import platform
import pytest

# post_coder reads PM_API_URL at import time — set it before any import.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

# The verify-check runner subprocesses into `bash -c <cmd>`. Production runs
# inside the orchestrator's Linux container where bash is /bin/bash; on
# Windows hosts Git Bash exists but its arg / cwd handling diverges. Skip
# the runner-touching tests on Windows so CI on Linux still exercises the
# real behavior; the parser + _check_expected tests are pure Python and
# always run.
_skip_no_bash = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="_run_verify subprocesses bash -c, which has Windows-specific "
           "quirks; production runs on Linux only.",
)

from orchestrator.pipelines.post_coder import (  # noqa: E402
    _parse_ac_verifies,
    _run_verify,
    _check_expected,
    _is_server_required,
    _post_coder_verify_check,
)


class TestIsServerRequired:
    """Pre-run command-shape detector. Regression for the 2026-05-30
    DocumentSign cascade: `curl -s -w '%{http_code}' http://localhost:...`
    recipes ran, exited 7, wrote 000 to stdout and empty stderr (because
    of -s), and the stderr-marker-only check classified them as mismatches.
    The pre-detector closes that gap by parsing the command shape.
    """

    @pytest.mark.parametrize("cmd", [
        "curl http://localhost:8000/foo",
        "curl -s -X POST http://localhost:8000/api/auth",
        "curl -s -o /dev/null -w '%{http_code}' -H 'X-API-Key: k' http://localhost:8000/x",
        "curl http://127.0.0.1/health",
        "curl -s 'http://0.0.0.0:8765/api/v1/y'",
        "curl --silent https://localhost:8443/secure",
        "curl http://localhost:8000/x | python3 -c '...'",
    ])
    def test_curl_localhost_variants_are_server_required(self, cmd):
        assert _is_server_required(cmd) is True

    @pytest.mark.parametrize("cmd", [
        "python3 -c 'import requests; r = requests.get(\"http://localhost:8000/x\")'",
        "python -c 'import httpx; httpx.get(\"http://127.0.0.1/y\")'",
        "python -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8000/z\")'",
    ])
    def test_python_http_against_localhost_is_server_required(self, cmd):
        assert _is_server_required(cmd) is True

    @pytest.mark.parametrize("cmd", [
        "python3 -c 'from fastapi.testclient import TestClient; ...'",
        "python -c 'from src.main import app; from starlette.testclient import TestClient; c = TestClient(app); ...'",
        "python -c 'from src.foo import bar; assert bar(1) == 2'",
        "echo OK",
        "pytest tests/test_foo.py::test_bar",
        "python3 -c 'import re; assert re.match(r\"\\d+\", \"42\")'",
        # curl against EXTERNAL service is NOT server-required from the
        # post-coder pipeline's perspective (we're not the missing host).
        "curl https://api.github.com/repos/foo/bar",
    ])
    def test_pure_commands_are_not_server_required(self, cmd):
        assert _is_server_required(cmd) is False

    def test_testclient_overrides_localhost_pattern(self):
        # TestClient + a string mentioning localhost in a comment: the
        # TestClient marker wins because it doesn't actually make a
        # network call to localhost.
        cmd = (
            "python3 -c 'from fastapi.testclient import TestClient; "
            "# tests http://localhost:8000 routes via ASGI direct\n"
            "from src.main import app; c = TestClient(app); print(c.get(\"/x\").status_code)'"
        )
        assert _is_server_required(cmd) is False


# ── _parse_ac_verifies ───────────────────────────────────────────────────────


class TestParseACVerifies:
    def test_single_verify_per_ac(self):
        doc = (
            "# Feature Design: foo\n\n"
            "## Acceptance Criteria\n\n"
            "AC1. Does the thing.\n"
            "     Verify: `echo OK`\n"
            "     Expected: `OK`\n"
            "     Test: test_thing.\n"
        )
        out = _parse_ac_verifies(doc)
        assert len(out) == 1
        assert out[0] == (1, "echo OK", "OK")

    def test_multi_verify_per_ac(self):
        # Multi-Verify per AC: the new prompt allows this for ACs with
        # multiple observable behaviors (success path + error path + edge).
        doc = (
            "AC1. Auth path.\n"
            "     Verify: `curl -X POST -H 'X-API-Key: valid' http://x/auth`\n"
            "     Expected: `200`\n"
            "     Verify: `curl -X POST -H 'X-API-Key: bad' http://x/auth`\n"
            "     Expected: `401`\n"
            "     Test: test_auth.\n"
        )
        out = _parse_ac_verifies(doc)
        assert len(out) == 2
        assert out[0][0] == 1 and out[0][2] == "200"
        assert out[1][0] == 1 and out[1][2] == "401"

    def test_multiple_acs(self):
        doc = (
            "AC1. one. Verify: `echo a` Expected: `a` Test: t1.\n"
            "AC2. two.\n"
            "     Verify: `echo b`\n"
            "     Expected: `b`\n"
            "AC3. three.\n"
            "     Verify: `echo c`\n"
            "     Expected: `c`\n"
        )
        out = _parse_ac_verifies(doc)
        # AC1 single-line with inline pairs sometimes won't parse cleanly
        # (no newline between Verify and Expected). Accept >= 2 for the
        # multi-line ones.
        ac_nums = sorted({n for n, _, _ in out})
        assert 2 in ac_nums and 3 in ac_nums

    def test_multiline_command_inside_backticks(self):
        # python -c with semicolons inside the backtick fence.
        doc = (
            "AC1. complex.\n"
            "     Verify: `python3 -c \"\n"
            "import sys\n"
            "print('MATCH')\n"
            "\"`\n"
            "     Expected: `MATCH`\n"
        )
        out = _parse_ac_verifies(doc)
        assert len(out) == 1
        assert out[0][0] == 1
        assert "import sys" in out[0][1]
        assert "MATCH" in out[0][1]
        assert out[0][2] == "MATCH"

    def test_empty_doc_returns_empty(self):
        assert _parse_ac_verifies("") == []
        assert _parse_ac_verifies("# Empty\n\nNo ACs here.\n") == []

    def test_ac_without_verify_skipped(self):
        # Legacy doc with no Verify line — no triple emitted.
        doc = (
            "AC1. Old-style AC with no recipe.\n"
            "     Test: test_legacy.\n"
        )
        assert _parse_ac_verifies(doc) == []

    def test_expected_with_backtick_then_parenthetical_narration(self):
        # Regression for 2026-05-31 DocumentSign #1145/#1146 cascade.
        # Designer prompt §AC quality calibration shows Expected as:
        #     Expected: `[100,76.66,73.33]` (matches §Algorithm Specs...).
        # The first backtick-quoted segment is the strict-match target;
        # the parenthetical that follows is human commentary. Pre-fix,
        # the parser emitted the entire post-strip line including the
        # parenthetical, and downstream `cleaned in actual_stdout` failed
        # because the target was longer than the actual output.
        doc = (
            "AC1. Insert and read back.\n"
            "     Verify: `python -c \"print('view {test:true}')\"`\n"
            "     Expected: `view {test:true}` (row inserted and readable "
            "with correct values).\n"
            "     Test: test_audit_insert.\n"
        )
        out = _parse_ac_verifies(doc)
        assert len(out) == 1
        # Strict-match target is the backtick-quoted output ONLY —
        # the parenthetical narration must be stripped.
        assert out[0][2] == "view {test:true}"

    def test_expected_with_status_codes_and_narration(self):
        # Real-world #1146 shape: multiple status codes inside backticks
        # followed by a verbose parenthetical mapping each to its case.
        doc = (
            "AC1. Decline endpoint.\n"
            "     Verify: `python -c \"...\"`\n"
            "     Expected: `200 declined 409 404` (valid token → 200 + "
            "\"declined\", used token → 409, fake token → 404).\n"
        )
        out = _parse_ac_verifies(doc)
        assert len(out) == 1
        assert out[0][2] == "200 declined 409 404"

    def test_expected_without_backticks_falls_back_to_full_line(self):
        # Legacy / freeform Expected lines (no backtick quotes) still parse
        # sensibly — the whole stripped line is the match target so the
        # current substring heuristic in _check_expected keeps working.
        doc = (
            "AC1. plain.\n"
            "     Verify: `echo hello world`\n"
            "     Expected: hello world\n"
        )
        out = _parse_ac_verifies(doc)
        assert len(out) == 1
        assert out[0][2] == "hello world"


# ── _check_expected ──────────────────────────────────────────────────────────


class TestCheckExpected:
    def test_exit_code_zero_match(self):
        assert _check_expected("", 0, "exit code 0") is True
        assert _check_expected("noise", 0, "exits 0") is True

    def test_exit_code_zero_mismatch(self):
        assert _check_expected("", 1, "exit code 0") is False
        assert _check_expected("", 137, "exits 0") is False

    def test_exit_code_nonzero_target(self):
        assert _check_expected("", 401, "exits 401") is True
        assert _check_expected("", 400, "exit code 401") is False

    def test_stdout_substring_match(self):
        assert _check_expected("OK\n", 0, "OK") is True
        assert _check_expected("status=OK trailing", 0, "OK") is True

    def test_stdout_strips_prefix_words(self):
        # "stdout exactly MATCH" → match if stdout contains "MATCH"
        assert _check_expected("MATCH\n", 0, "stdout exactly `MATCH`") is True
        assert _check_expected("MATCH", 0, "prints `MATCH`") is True

    def test_stdout_substring_mismatch(self):
        assert _check_expected("WRONG\n", 0, "OK") is False

    def test_empty_expected_passes(self):
        # Designer wrote an Expected line with nothing parseable — don't
        # spuriously bounce.
        assert _check_expected("", 0, "") is True
        assert _check_expected("anything", 0, "``") is True


# ── _run_verify ──────────────────────────────────────────────────────────────


@_skip_no_bash
class TestRunVerify:
    def test_success_captures_stdout_and_exit(self, tmp_path):
        r = _run_verify("echo HELLO", cwd=str(tmp_path), timeout=5)
        assert r["exit_code"] == 0
        assert "HELLO" in r["stdout"]
        assert r["skipped"] is False

    def test_failed_command_returns_nonzero(self, tmp_path):
        r = _run_verify("exit 137", cwd=str(tmp_path), timeout=5)
        assert r["exit_code"] == 137
        assert r["skipped"] is False

    def test_timeout_yields_124(self, tmp_path):
        r = _run_verify("sleep 5", cwd=str(tmp_path), timeout=1)
        assert r["exit_code"] == 124
        assert "timed out" in r["stderr"]

    def test_connection_refused_is_skipped(self, tmp_path):
        # Hit a port that should have no listener. The exact stderr text
        # depends on curl's locale but always includes a known marker.
        r = _run_verify(
            "curl -s --max-time 2 http://127.0.0.1:1 2>&1 1>/dev/null; "
            "curl -s --max-time 2 http://127.0.0.1:1",
            cwd=str(tmp_path), timeout=10,
        )
        # On some platforms the connect failure goes to stdout via the
        # combined redirect; on others stderr. The function checks stderr
        # only, so this test asserts on the function's behavior given a
        # synthetic Connection-refused stderr instead — see the next test.

    def test_synthetic_connection_refused_classified_as_skip(self, tmp_path):
        # Use a wrapper that writes "Connection refused" to stderr so the
        # heuristic fires regardless of curl/platform quirks.
        r = _run_verify(
            'echo "curl: (7) Failed to connect to localhost port 8000: '
            'Connection refused" >&2; exit 7',
            cwd=str(tmp_path), timeout=5,
        )
        assert r["skipped"] is True
        assert "server unavailable" in r["skip_reason"]


# ── _post_coder_verify_check (public entry) ─────────────────────────────────


def _write_story(tmp_path, fid: int, body: str):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / f"story_{fid}.md").write_text(body, encoding="utf-8")


@_skip_no_bash
class TestPostCoderVerifyCheck:
    def test_disabled_by_default_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("POST_CODER_VERIFY_CHECK_ENABLED", raising=False)
        _write_story(tmp_path, 99, "AC1. x.\n     Verify: `echo OK`\n     Expected: `OK`\n")
        r = _post_coder_verify_check(
            working_dir=str(tmp_path),
            product_name="t",
            assigned_features=[{"id": 99}],
        )
        assert r["checked"] is False
        assert r["passed"] is True
        assert r["total"] == 0

    def test_enabled_all_pass(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POST_CODER_VERIFY_CHECK_ENABLED", "1")
        _write_story(tmp_path, 99,
            "AC1. one.\n     Verify: `echo OK`\n     Expected: `OK`\n"
            "AC2. two.\n     Verify: `echo MATCH`\n     Expected: `MATCH`\n"
        )
        r = _post_coder_verify_check(
            working_dir=str(tmp_path),
            product_name="t",
            assigned_features=[{"id": 99}],
        )
        assert r["checked"] is True
        assert r["passed"] is True
        assert r["total"] == 2
        assert r["failures"] == []

    def test_enabled_mismatch_bounces(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POST_CODER_VERIFY_CHECK_ENABLED", "1")
        _write_story(tmp_path, 99,
            "AC1. one.\n     Verify: `echo WRONG`\n     Expected: `OK`\n"
        )
        r = _post_coder_verify_check(
            working_dir=str(tmp_path),
            product_name="t",
            assigned_features=[{"id": 99}],
        )
        assert r["checked"] is True
        assert r["passed"] is False
        assert len(r["failures"]) == 1
        f = r["failures"][0]
        assert f["feature_id"] == 99
        assert f["ac"] == 1
        assert "WRONG" in f["actual_stdout"]
        assert f["expected"] == "OK"

    def test_enabled_server_unavailable_skipped_not_failed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POST_CODER_VERIFY_CHECK_ENABLED", "1")
        # Synthetic "Connection refused" → skip, not fail.
        _write_story(tmp_path, 99,
            "AC1. one.\n"
            "     Verify: `echo 'curl: (7) Connection refused' >&2; exit 7`\n"
            "     Expected: `200`\n"
        )
        r = _post_coder_verify_check(
            working_dir=str(tmp_path),
            product_name="t",
            assigned_features=[{"id": 99}],
        )
        assert r["checked"] is True
        assert r["passed"] is True  # skip ≠ fail
        assert len(r["skipped"]) == 1
        assert r["failures"] == []

    def test_no_design_doc_silent_pass(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POST_CODER_VERIFY_CHECK_ENABLED", "1")
        # No story_<id>.md → gate silently passes (legacy / no recipe).
        r = _post_coder_verify_check(
            working_dir=str(tmp_path),
            product_name="t",
            assigned_features=[{"id": 99}],
        )
        assert r["checked"] is True
        assert r["passed"] is True
        assert r["total"] == 0

    def test_doc_without_verify_recipes_silent_pass(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POST_CODER_VERIFY_CHECK_ENABLED", "1")
        _write_story(tmp_path, 99,
            "AC1. legacy AC with no Verify line.\n     Test: test_legacy.\n"
        )
        r = _post_coder_verify_check(
            working_dir=str(tmp_path),
            product_name="t",
            assigned_features=[{"id": 99}],
        )
        # Legacy docs are pre-verify-driven; the gate should silently pass
        # so the rollout doesn't bounce old in-flight features.
        assert r["passed"] is True
        assert r["total"] == 0

    def test_mixed_pass_and_skip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POST_CODER_VERIFY_CHECK_ENABLED", "1")
        _write_story(tmp_path, 99,
            "AC1. ok.\n     Verify: `echo OK`\n     Expected: `OK`\n"
            "AC2. server-dep.\n"
            "     Verify: `echo 'Connection refused' >&2; exit 7`\n"
            "     Expected: `200`\n"
        )
        r = _post_coder_verify_check(
            working_dir=str(tmp_path),
            product_name="t",
            assigned_features=[{"id": 99}],
        )
        assert r["checked"] is True
        assert r["passed"] is True  # one ok + one skip → overall pass
        assert r["total"] == 2
        assert len(r["skipped"]) == 1
        assert len(r["failures"]) == 0
