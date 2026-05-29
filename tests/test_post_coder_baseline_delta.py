"""
Tests for the post-coder test-check baseline-delta (2026-05-29).

A single broken test in the suite used to deadlock every feature: post-coder
runs the FULL suite and bounced on ANY failure, including pre-existing ones the
feature didn't touch (calc3 mass-block). The test-check now baselines failures
against origin/main and bounces only on failures the feature INTRODUCED.

The worktree baseline runner (_baseline_pytest_failures) is monkeypatched for
deterministic classification tests; a fail-safe smoke test covers its error path.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines import post_coder as pc  # noqa: E402


def _fake_run(run_out="", run_rc=1, collect_out=""):
    def _run(cmd, **kw):
        if cmd[:1] == ["pip"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "--collect-only" in cmd:
            return SimpleNamespace(returncode=0, stdout=collect_out, stderr="")
        return SimpleNamespace(returncode=run_rc, stdout=run_out, stderr="")
    return _run


class TestIntroducedFailures:
    def test_delta(self):
        assert pc._introduced_failures(["a", "b", "c"], {"b"}) == ["a", "c"]

    def test_all_preexisting(self):
        assert pc._introduced_failures(["a", "b"], {"a", "b"}) == []

    def test_none_preexisting(self):
        assert pc._introduced_failures(["a"], set()) == ["a"]

    def test_order_preserved(self):
        assert pc._introduced_failures(["c", "a", "b"], {"a"}) == ["c", "b"]


class TestTestCheckBaselineDelta:
    def _pytest_repo(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        return str(tmp_path)

    def test_introduced_failure_bounces(self, tmp_path, monkeypatch):
        wd = self._pytest_repo(tmp_path)
        monkeypatch.setattr(pc, "_baseline_pytest_failures",
                            lambda *a, **k: {"tests/x.py::test_a"})
        run = _fake_run("FAILED tests/x.py::test_a\nFAILED tests/x.py::test_b\n")
        res = pc._post_coder_test_check(wd, run)
        assert res["passed"] is False
        assert res["introduced_failures"] == ["tests/x.py::test_b"]
        assert res["pre_existing_failures"] == ["tests/x.py::test_a"]
        assert "test_b" in res["first_failure"]

    def test_all_preexisting_does_not_bounce(self, tmp_path, monkeypatch):
        wd = self._pytest_repo(tmp_path)
        monkeypatch.setattr(pc, "_baseline_pytest_failures",
                            lambda *a, **k: {"tests/x.py::test_a", "tests/x.py::test_b"})
        run = _fake_run("FAILED tests/x.py::test_a\nFAILED tests/x.py::test_b\n")
        res = pc._post_coder_test_check(wd, run)
        assert res["passed"] is True          # not bounced
        assert res["pre_existing_only"] is True
        assert res["introduced_failures"] == []

    def test_baseline_empty_treats_all_introduced(self, tmp_path, monkeypatch):
        # Baselining unavailable (worktree add failed → empty) → fail safe to
        # the old bounce-on-any behaviour.
        wd = self._pytest_repo(tmp_path)
        monkeypatch.setattr(pc, "_baseline_pytest_failures", lambda *a, **k: set())
        run = _fake_run("FAILED tests/x.py::test_a\n")
        res = pc._post_coder_test_check(wd, run)
        assert res["passed"] is False
        assert res["introduced_failures"] == ["tests/x.py::test_a"]

    def test_pass_is_unchanged(self, tmp_path, monkeypatch):
        wd = self._pytest_repo(tmp_path)
        # Should never even call the baseline helper on a green run.
        monkeypatch.setattr(pc, "_baseline_pytest_failures",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called on pass")))
        res = pc._post_coder_test_check(wd, _fake_run("", run_rc=0))
        assert res["passed"] is True
        assert res["pre_existing_only"] is False


class TestBaselineFailuresFailSafe:
    def test_no_git_repo_returns_empty(self, tmp_path):
        # Not a git repo → `git worktree add` fails → empty set, no raise.
        assert pc._baseline_pytest_failures(str(tmp_path), ["tests/x.py::test_a"], 30) == set()

    def test_empty_ids_returns_empty(self, tmp_path):
        assert pc._baseline_pytest_failures(str(tmp_path), [], 30) == set()
