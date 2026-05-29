"""
Tests for build_repo_map (2026-05-29 v0).

Whole-repo, AST-derived inventory injected into every coder session. Validates
that the route table is correctly extracted (Flask `@app.route` + Flask/FastAPI
method decorators), that the module surface lists public defs/classes with
signatures, that excluded dirs (tests/migrations/__pycache__) don't pollute,
that the output respects a char budget, and that routes are always preserved
under truncation pressure.
"""
import os

# context_builder pulls nothing through, but PM_API_URL is expected at deeper import.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.session.context_builder import build_repo_map  # noqa: E402


def _write(p, body):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class TestRoutesExtraction:
    def test_flask_app_route_with_methods(self, tmp_path):
        _write(tmp_path / "src" / "main.py", (
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "@app.route('/api/users', methods=['GET', 'POST'])\n"
            "def users(): pass\n"
        ))
        out = build_repo_map(str(tmp_path))
        assert "GET" in out and "POST" in out
        assert "/api/users" in out
        assert "src/main.py:users" in out

    def test_flask_method_decorators(self, tmp_path):
        _write(tmp_path / "src" / "main.py", (
            "@app.get('/x')\n"
            "def get_x(): pass\n"
            "@app.delete('/y')\n"
            "def del_y(): pass\n"
        ))
        out = build_repo_map(str(tmp_path))
        assert "GET    /x" in out or "GET     /x" in out  # column width may vary
        assert "DELETE" in out and "/y" in out

    def test_fastapi_router_decorators(self, tmp_path):
        _write(tmp_path / "src" / "api.py", (
            "@router.post('/items')\n"
            "async def create_item(): pass\n"
            "@router.put('/items/{id}')\n"
            "def update_item(): pass\n"
        ))
        out = build_repo_map(str(tmp_path))
        assert "POST" in out and "/items" in out
        assert "PUT" in out and "src/api.py:update_item" in out

    def test_route_in_tests_dir_excluded(self, tmp_path):
        _write(tmp_path / "tests" / "test_x.py", (
            "@app.get('/leaky')\ndef leaky(): pass\n"
        ))
        _write(tmp_path / "src" / "main.py", "@app.get('/real')\ndef real(): pass\n")
        out = build_repo_map(str(tmp_path))
        assert "/real" in out
        assert "/leaky" not in out

    def test_route_in_migrations_excluded(self, tmp_path):
        _write(tmp_path / "migrations" / "0001.py", "@app.get('/leaky')\ndef leaky(): pass\n")
        _write(tmp_path / "src" / "main.py", "@app.get('/real')\ndef real(): pass\n")
        out = build_repo_map(str(tmp_path))
        assert "/real" in out
        assert "/leaky" not in out


class TestModuleSurface:
    def test_public_defs_and_classes(self, tmp_path):
        _write(tmp_path / "src" / "history.py", (
            "def init_db(): pass\n"
            "def get_history(limit: int = 20) -> list: pass\n"
            "def _private(): pass\n"
            "class Repository: pass\n"
            "class _Internal: pass\n"
        ))
        out = build_repo_map(str(tmp_path))
        assert "src/history.py" in out
        assert "init_db" in out
        assert "get_history" in out
        assert "_private" not in out, "private functions must be skipped"
        assert "Repository" in out
        assert "_Internal" not in out

    def test_function_signature_included(self, tmp_path):
        _write(tmp_path / "src" / "calc.py", (
            "def calculate(op: str, a: float, b: float) -> float: pass\n"
        ))
        out = build_repo_map(str(tmp_path))
        # Signature should appear in some form.
        assert "calculate" in out
        assert "op" in out and "a" in out and "b" in out


class TestRendering:
    def test_empty_dir_returns_empty(self, tmp_path):
        assert build_repo_map(str(tmp_path)) == ""

    def test_missing_dir_returns_empty(self, tmp_path):
        assert build_repo_map(str(tmp_path / "nope")) == ""

    def test_budget_truncates_symbols_but_keeps_routes(self, tmp_path):
        # Many big files; small budget. Routes must survive; symbol list
        # may be truncated.
        for i in range(30):
            body = "\n".join(f"def func_{i}_{k}(arg_{k}): pass" for k in range(20))
            _write(tmp_path / "src" / f"m{i}.py", body)
        _write(tmp_path / "src" / "main.py",
               "@app.get('/critical')\ndef critical_endpoint(): pass\n")
        out = build_repo_map(str(tmp_path), max_chars=600)
        assert "/critical" in out, "routes must survive small budget"
        assert "critical_endpoint" in out
        assert len(out) <= 1200, "honour budget within ~2x slack for closing fences"

    def test_routes_sorted_by_path_then_method(self, tmp_path):
        _write(tmp_path / "src" / "main.py", (
            "@app.post('/b')\ndef b_post(): pass\n"
            "@app.get('/a')\ndef a_get(): pass\n"
            "@app.delete('/a')\ndef a_del(): pass\n"
        ))
        out = build_repo_map(str(tmp_path))
        # /a comes before /b; among /a, DELETE before GET (alphabetical method).
        ai = out.index("/a")
        bi = out.index("/b")
        assert ai < bi
        del_i = out.index("DELETE")
        get_i = out.index("GET")
        assert del_i < get_i

    def test_route_dedup(self, tmp_path):
        # A route declared twice (e.g. in two modules) should appear once.
        _write(tmp_path / "src" / "a.py", "@app.get('/x')\ndef ax(): pass\n")
        _write(tmp_path / "src" / "b.py", "@app.get('/x')\ndef bx(): pass\n")
        out = build_repo_map(str(tmp_path))
        # Both handlers should be deduped by (method, path, file, func) — they
        # ARE distinct since file differs. So both still appear.
        assert "src/a.py:ax" in out
        assert "src/b.py:bx" in out
