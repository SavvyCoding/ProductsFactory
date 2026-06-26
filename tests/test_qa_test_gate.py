"""QA/Tester gate — deterministic test execution in a throwaway agent-image
container (no LLM). Replaces in-orchestrator-process test execution, which
broke for every non-Python stack because the orchestrator lacks node/npm/go
(canonical: MyCalc1 #1370 'npm not found' → env_broken cascade).

These tests assert `_container_test_run` builds the correct `docker run` argv
(stack-appropriate clean install prepended, workspace mounted, hardening flags)
without actually launching docker.
"""
import os
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import orchestrator.pipelines.post_coder as pc


def _capture(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(pc._sp, "run", fake_run)
    return calls


class TestAllFailingTests:
    """Runner-agnostic parsing of the FULL failing-test set (widened rework
    feedback) — so the coder fixes every failure, not just the first."""

    def test_vitest_lists_all_failing_files(self):
        out = (
            " ✓ tests/engine.test.ts (6 tests) 2ms\n"
            " ❯ tests/Calculator.test.tsx (0 test)\n"
            " ❯ tests/scaffolding.test.ts (4 tests | 2 failed) 7346ms\n"
        )
        got = pc._all_failing_tests(out)
        assert "tests/Calculator.test.tsx" in got
        assert "tests/scaffolding.test.ts" in got
        assert "tests/engine.test.ts" not in got        # passing file excluded

    def test_pytest_lists_all_failed_ids(self):
        out = "FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n"
        got = pc._all_failing_tests(out)
        assert got == ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]

    def test_ignores_application_error_logs(self):
        # The gate runs pytest with `-s`, so the app's own ERROR-level logs reach
        # stdout. They must NOT be parsed as failing tests — doing so fed garbage
        # ids like `[src.lib.cache]` into both the rework feedback and the
        # baseline-diff (which could never match them on origin/main), bouncing
        # every feature (2026-06-22 ship-freeze). Real pytest FAILED lines with a
        # .py/:: node id are still picked up.
        out = (
            "ERROR [src.lib.cache] Unexpected error in get_match_cache: 'REDIS_URL'\n"
            "ERROR [src.lib.push] Error sending push notification: FCM_CREDENTIALS_PATH must be set\n"
            "ERROR [src.lib.celery_app] send_push_task failed for user 9: boom\n"
            "INFO  [src.lib.celery_app] send_push_task result: sent=2 total=3\n"
            "FAILED tests/test_admin.py::test_is_admin_column_exists\n"
        )
        got = pc._all_failing_tests(out)
        assert got == ["tests/test_admin.py::test_is_admin_column_exists"]
        assert not any("src.lib" in g for g in got)

    def test_real_pytest_collection_error_kept(self):
        # A genuine pytest collection error names a .py file → still captured.
        assert pc._all_failing_tests("ERROR tests/test_imports.py\n") == ["tests/test_imports.py"]

    def test_go_failures(self):
        assert "TestFoo" in pc._all_failing_tests("--- FAIL: TestFoo (0.01s)\n")

    def test_strips_ansi_and_dedupes(self):
        out = "\x1b[31mFAILED tests/x.py::t\x1b[0m\nFAILED tests/x.py::t\n"
        assert pc._all_failing_tests(out) == ["tests/x.py::t"]

    def test_empty_on_no_failures(self):
        assert pc._all_failing_tests("all good\n") == []
        assert pc._all_failing_tests("") == []


class TestRecipeDefectDetection:
    """A Verify recipe whose OWN code raises is a designer spec defect (route to
    designer); an exception from the app under test is the coder's bug (bounce
    coder); an AssertionError is a legit AC failure (bounce coder)."""

    def _tb(self, deepest_frame, exc_line):
        return (f"Traceback (most recent call last):\n"
                f'  File "<string>", line 2, in <module>\n'
                f'  File "{deepest_frame}", line 9, in check\n'
                f"{exc_line}")

    def test_recipe_attributeerror_is_defect(self):
        # #1872: AC1 recipe used mw.options (starlette has .kwargs, never .options)
        tb = ('Traceback (most recent call last):\n'
              '  File "<string>", line 3, in <module>\n'
              'AttributeError: \'Middleware\' object has no attribute \'options\'')
        assert pc._recipe_defect_reason("", tb, 1)

    def test_recipe_typeerror_is_defect(self):
        # #1882: PRAGMA index_info row indexed with a string
        tb = ('Traceback (most recent call last):\n'
              '  File "<string>", line 5, in <module>\n'
              'TypeError: list indices must be integers or slices, not str')
        assert pc._recipe_defect_reason(tb, "", 1)

    def test_unawaited_coroutine_is_defect(self):
        # #1565: recipe calls async init_db() synchronously
        out = "RuntimeWarning: coroutine 'init_db' was never awaited"
        assert pc._recipe_defect_reason(out, "", 1)

    def test_app_exception_is_not_defect(self):
        # #1882 later: RecursionError from src/lib/cache.py is the CODER's bug.
        tb = self._tb("/workspace/src/lib/cache.py",
                      "RecursionError: maximum recursion depth exceeded")
        assert pc._recipe_defect_reason(tb, "", 1) is None

    def test_assertion_is_not_defect(self):
        # Recipe asserted, app didn't satisfy it → legit AC failure, bounce coder.
        tb_bare = ('Traceback (most recent call last):\n'
                   '  File "<string>", line 1, in <module>\nAssertionError')
        tb_msg = ('Traceback (most recent call last):\n'
                  '  File "<string>", line 1, in <module>\n'
                  'AssertionError: expected 10 got 0')
        assert pc._recipe_defect_reason(tb_bare, "", 1) is None
        assert pc._recipe_defect_reason(tb_msg, "", 1) is None

    def test_clean_exit_and_plain_mismatch_not_defect(self):
        assert pc._recipe_defect_reason("all good", "", 0) is None      # passed
        assert pc._recipe_defect_reason("got 4 want 5", "", 1) is None  # mismatch, no traceback

    def test_setup_failure_int_none_is_defect_even_in_app_frame(self):
        # #1852: recipe used a 7-char password → registration 422 → missing id →
        # str(None) → int('None') ValueError. The crash surfaces in APP code
        # (src/auth/deps.py), so the frame-based discriminator alone misses it,
        # but the int('None') signature means the recipe's SETUP failed → designer.
        tb = self._tb("/workspace/src/auth/deps.py",
                      "ValueError: invalid literal for int() with base 10: 'None'")
        assert pc._recipe_defect_reason(tb, "", 1)

    def test_setup_failure_int_empty_is_defect(self):
        tb = self._tb("/workspace/src/auth/deps.py",
                      "ValueError: invalid literal for int() with base 10: ''")
        assert pc._recipe_defect_reason(tb, "", 1)

    def test_real_int_parse_error_not_flagged(self):
        # int('abc') is NOT the forged-from-missing-setup signature — could be a
        # real coder bug parsing genuine input → leave it for the coder.
        tb = self._tb("/workspace/src/api/widgets.py",
                      "ValueError: invalid literal for int() with base 10: 'abc'")
        assert pc._recipe_defect_reason(tb, "", 1) is None

    def test_recipe_syntaxerror_full_display_is_defect(self):
        # #1864: multi-statement async snippet crammed into one `python -c` line.
        out = ('  File "<string>", line 1\n'
               "    async def f(): for x in y: pass\n"
               "                   ^^^\n"
               "SyntaxError: invalid syntax")
        assert pc._recipe_defect_reason("", out, 1)

    def test_recipe_syntaxerror_TRUNCATED_is_defect(self):
        # Real shape: verify-check caps stderr at 500 chars, so the long echoed
        # source truncates the literal "SyntaxError:" line OFF. Detect by shape:
        # `File "<string>"` + no traceback header. (HCS #1864/#1849/#1832.)
        out = ('File "<string>", line 1\n'
               "    import os, asyncio; from sqlalchemy import update; "
               "os.environ['JWT_SECRET']='x'; os.environ.setdefault('REDIS_U")
        assert pc._recipe_defect_reason("", out, 1)

    def test_stdin_parse_error_is_defect(self):
        out = 'File "<stdin>", line 2\n    bad syntax here'
        assert pc._recipe_defect_reason(out, "", 1)

    def test_imported_module_syntaxerror_is_NOT_recipe_defect(self):
        # A SyntaxError in an IMPORTED src/ module raises a real Traceback whose
        # deepest frame is src/ → the CODER's bug, not the recipe's. Must NOT flag.
        tb = ('Traceback (most recent call last):\n'
              '  File "<string>", line 1, in <module>\n'
              '  File "/workspace/src/api/broken.py", line 9\n'
              '    def f(:\n'
              '          ^\n'
              'SyntaxError: invalid syntax')
        assert pc._recipe_defect_reason("", tb, 1) is None


class TestVerifyCheckInContainer:
    """The verify-check (per-AC Verify recipes) must run in the agent container
    too — the recipes invoke stack tools (npx/tsc/node) the orchestrator lacks
    (the MyCalc1 #1370 `npx: command not found` false-bounce)."""

    def test_recipe_runs_in_agent_container(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
        calls = _capture(monkeypatch)
        res = pc._run_verify("npx tsc --noEmit", cwd=str(tmp_path), timeout=60)
        cmd = calls["cmd"]
        assert cmd[:3] == ["docker", "run", "--rm"]
        assert "productfactory-agent" in cmd
        assert any(a.endswith(":/workspace") for a in cmd)
        script = cmd[-1]
        assert "npx tsc --noEmit" in script        # the recipe (via bash -c)
        assert "timeout 60s" in script             # hard timeout backstop
        assert ">/dev/null 2>&1" in script         # install silenced
        assert res["skipped"] is False

    def test_server_required_recipe_still_skipped_without_container(self, tmp_path, monkeypatch):
        calls = _capture(monkeypatch)
        res = pc._run_verify("curl -s http://localhost:8000/health", cwd=str(tmp_path), timeout=30)
        assert res["skipped"] is True              # pre-detected, no docker run
        assert "cmd" not in calls                  # never launched a container


class TestEnvBrokenClassification:
    """A pip install failure from a CODER-declared bad dependency (unresolvable
    package) must be a real bounce (fix_attempts++), not env_broken — only
    genuine infra/transient failures stay env_broken (testingcalc #1423)."""

    def _check(self, tmp_path, install_stderr):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "requirements.txt").write_text("somepkg\n")

        def fake_run(cmd, **kw):
            class _R:
                returncode = 1
                stdout = ""
                stderr = install_stderr
            return _R()

        return pc._post_coder_test_check(str(tmp_path), fake_run, "test")

    def test_unresolvable_package_is_real_bounce(self, tmp_path):
        res = self._check(tmp_path,
            "ERROR: Could not find a version that satisfies the requirement bogus==9.9")
        assert res["env_broken"] is False            # coder's fault → fix_attempts++
        assert res["passed"] is False
        assert "requirements.txt" in res["first_failure"]

    def test_no_matching_distribution_is_real_bounce(self, tmp_path):
        res = self._check(tmp_path,
            "ERROR: No matching distribution found for nonexistent-pkg-xyz")
        assert res["env_broken"] is False

    def test_transient_network_stays_env_broken(self, tmp_path):
        res = self._check(tmp_path,
            "WARNING: Retrying (Retry(total=4)) ... Connection broken: Read timed out")
        assert res["env_broken"] is True             # infra/transient → no bump
        assert res["passed"] is False

    def test_benign_pyenv_rehash_is_ignored(self, tmp_path):
        """pip exits non-zero ONLY because the pyenv rehash hook can't write the
        root-owned shims dir (deps installed fine) → must NOT bounce; proceed to
        tests (testingcalc #1423 infinite loop)."""
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "requirements.txt").write_text("fastapi\n")

        def fake_run(cmd, **kw):
            is_install = "install" in cmd
            class _R:
                returncode = 1 if is_install else 0
                stdout = "" if is_install else "collected 1 item\n"
                stderr = ("pyenv: cannot rehash: /opt/pyenv/shims isn't writable"
                          if is_install else "")
            return _R()

        res = pc._post_coder_test_check(str(tmp_path), fake_run, "test")
        assert res["env_broken"] is False            # benign rehash ignored
        assert res["passed"] is True                 # install OK → tests ran & passed


class TestVerifyActionableHint:
    """Verify-check feedback must be ACTIONABLE — surface the recipe's asserts
    and name the re-raise-vs-return-status fix (testingcalc #1423)."""

    def test_surfaces_recipe_asserts(self):
        cmd = "python -c \"\nresp = client.get('/x')\nassert resp.status_code == 200\nprint('OK')\""
        hint = pc._verify_actionable_hint(cmd, "OK\n", 0)
        assert "AC requires" in hint
        assert "status_code == 200" in hint

    def test_reraise_diagnosis_on_propagated_exception(self):
        cmd = "python -c \"\nresp = client.get('/crash')\nassert resp.status_code == 500\nprint('OK')\""
        actual = '{"level":"ERROR"}\nTraceback (most recent call last):\nRuntimeError: simulated boom'
        hint = pc._verify_actionable_hint(cmd, actual, 1)
        assert "RAISED instead of returning 500" in hint
        assert "RETURN a response" in hint

    def test_no_status_assert_no_reraise_hint(self):
        cmd = "python -c \"\nassert resp.status_code == 500\nprint('OK')\""
        # asserts surfaced, but no exception in output → no re-raise diagnosis
        hint = pc._verify_actionable_hint(cmd, "OK\n", 0)
        assert "AC requires" in hint
        assert "RAISED instead" not in hint

    def test_empty_when_nothing_actionable(self):
        assert pc._verify_actionable_hint("echo hi", "hi", 0) == ""


class TestContainerTestRun:
    def test_npm_install_prefixed(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["npm", "test", "--silent"], timeout=300)
        cmd = calls["cmd"]
        assert cmd[:3] == ["docker", "run", "--rm"]
        assert "productfactory-agent" in cmd          # default agent image
        assert any(a.endswith(":/workspace") for a in cmd)
        script = cmd[-1]
        assert script.startswith("npm ci") and "npm test --silent" in script

    def test_pip_install_prefixed(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"], timeout=300)
        script = calls["cmd"][-1]
        assert "pip install" in script and "requirements.txt" in script and "pytest -q" in script

    def test_go_install_prefixed(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module x\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["go", "test", "./..."], timeout=300)
        script = calls["cmd"][-1]
        assert script.startswith("go mod download") and "go test ./..." in script

    def test_no_markers_no_install_prefix(self, tmp_path, monkeypatch):
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["echo", "hi"])
        # No install prefix, but the hard-timeout backstop still wraps the cmd.
        assert calls["cmd"][-1] == "timeout 300s echo hi"

    def test_ci_env_and_timeout_backstop(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["npm", "test"], timeout=300)
        cmd = calls["cmd"]
        # CI=true disables watch mode (the MyCalc1 vitest hang); timeout is the
        # backstop that kills a stuck runner instead of hanging the pipeline.
        assert "CI=true" in cmd
        assert "timeout 300s npm test" in cmd[-1]

    def test_npm_takes_precedence_over_requirements(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["npm", "test"])
        assert calls["cmd"][-1].startswith("npm ci")

    def test_security_and_resource_flags_present(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"])
        cmd = calls["cmd"]
        for flag in ("--cap-drop", "--security-opt", "--network", "--pids-limit", "--memory"):
            assert flag in cmd, f"missing hardening flag {flag}"

    def test_per_product_download_cache_mounted(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"])
        cmd = calls["cmd"]
        assert any(a.endswith(":/cache") for a in cmd)          # cache bind-mount
        assert "PIP_CACHE_DIR=/cache/pip" in cmd
        assert "npm_config_cache=/cache/npm" in cmd
        # Cache lives outside the repo (sibling .pf-cache), never under the workspace.
        assert (tmp_path.parent / ".pf-cache" / tmp_path.name).exists()

    def test_outer_timeout_exceeds_inner(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"], timeout=300)
        assert calls["kw"]["timeout"] == 300 + 120     # install headroom over inner cap
