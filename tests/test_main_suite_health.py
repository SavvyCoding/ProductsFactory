"""detect_broken_main_suite: files a deduped chore + alert only when main is RED
with real failures; skips on passed/env_broken/dedupe; honors the kill-switch."""
import os
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import orchestrator.main_suite_health as msh

PRODUCT = {"id": 30, "working_dir": "/p/testingcalc", "name": "testingcalc"}


class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code
        self.is_success = 200 <= code < 300

    def json(self):
        return self._d


class _Client:
    def __init__(self, open_features=None):
        self.posts = []
        self._feats = open_features or []

    def get(self, url):          # /api/products/{id}/features (dedupe lookup)
        return _Resp(self._feats)

    def post(self, url, json=None):
        self.posts.append((url, json))
        return _Resp({}, 201)


def _patch_run(monkeypatch, status, ids=None):
    monkeypatch.setattr(msh, "_run_suite_on_main",
                        lambda wd, timeout=300: {"status": status,
                                                 "failing_ids": ids or [], "output": ""})


class TestMainSuiteHealth:
    def test_red_files_chore_and_alert(self, monkeypatch):
        _patch_run(monkeypatch, "failed", ["tests/test_x.py::t1", "tests/test_y.py::t2"])
        c = _Client()
        res = msh.detect_broken_main_suite(PRODUCT, pm_client=c)
        assert res["filed_chore"] is True
        urls = [u for u, _ in c.posts]
        assert "/api/features" in urls and "/api/alerts" in urls
        chore = next(j for u, j in c.posts if u == "/api/features")
        assert chore["feature_type"] == "chore"
        assert "broken_main_suite:30" in chore["description"]   # dedupe key embedded

    def test_passed_no_action(self, monkeypatch):
        _patch_run(monkeypatch, "passed")
        c = _Client()
        assert msh.detect_broken_main_suite(PRODUCT, pm_client=c)["filed_chore"] is False
        assert c.posts == []

    def test_env_broken_no_action(self, monkeypatch):
        _patch_run(monkeypatch, "env_broken")
        c = _Client()
        assert msh.detect_broken_main_suite(PRODUCT, pm_client=c)["filed_chore"] is False
        assert c.posts == []

    def test_dedupe_skips_when_chore_open(self, monkeypatch):
        _patch_run(monkeypatch, "failed", ["tests/test_x.py::t1"])
        open_feat = [{"feature_type": "chore", "status": "Approved",
                      "description": "x\n<!-- reconciler-key: broken_main_suite:30 -->"}]
        c = _Client(open_features=open_feat)
        res = msh.detect_broken_main_suite(PRODUCT, pm_client=c)
        assert res["filed_chore"] is False                       # deduped
        assert all(u != "/api/alerts" for u, _ in c.posts)       # no new alert

    def test_dry_run_files_nothing(self, monkeypatch):
        _patch_run(monkeypatch, "failed", ["t"])
        c = _Client()
        res = msh.detect_broken_main_suite(PRODUCT, pm_client=c, dry_run=True)
        assert res["filed_chore"] is False and c.posts == []

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("MAIN_SUITE_HEALTH_ENABLED", "0")
        _patch_run(monkeypatch, "failed", ["t"])
        c = _Client()
        assert msh.detect_broken_main_suite(PRODUCT, pm_client=c)["status"] == "disabled"
        assert c.posts == []
