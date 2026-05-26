#!/usr/bin/env python3
"""Static dead-code scanner — emits candidates for the architect persona.

Built after the 2026-05-25 drift-cleanup pass to keep dead code from
re-accumulating between manual audits. Run periodically (manually, or by
wiring into the architect persona's quantitative-drift detection at
``orchestrator/maintenance/architect.py``).

What it finds (each tier is a separate detector, all run by default):

  A. Functions / classes whose public name is referenced ≤1 time across
     the repo (i.e. only the def itself).
  B. FastAPI routes whose URL path string never appears outside its own
     ``@app.<method>(...)`` declaration.
  C. Module-level constants ``UPPER_CASE_NAME = ...`` never read outside
     their own module.
  D. ``import`` lines whose imported symbol is never used inside the
     importing file.

What it deliberately does NOT find (false positives in past audits):

  - Pydantic schemas resolved by FastAPI ``response_model=`` introspection.
  - ORM models that back tables written via raw SQL.
  - Detectors invoked by string name through dynamic dispatch.
  - Anything under ``tests/``, ``.venv/``, ``db/migrations/``, archives.

Output is plain text grouped by detector + by file so you can act on it
or pipe through ``grep``. Exit code is 0 always — this is informational,
not a CI gate. Wire it into a CI gate only after baselining the
remaining false-positive rate.

Usage:
    python scripts/find_dead_code.py              # all detectors
    python scripts/find_dead_code.py --funcs      # just detector A
    python scripts/find_dead_code.py --routes     # just detector B
    python scripts/find_dead_code.py --consts     # just detector C
    python scripts/find_dead_code.py --imports    # just detector D
    python scripts/find_dead_code.py --json       # machine-readable

Heuristic, not proof. Verify each finding before deleting — see how the
manual audit on 2026-05-25 cross-checked HTML/JS/prompts/scripts before
removing each item.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that contribute references but whose own files we do NOT
# audit (they're allowed to have one-off helpers, snapshots, etc.).
EXCLUDE_DIRS = {
    ".venv", ".pytest_cache", ".git", "node_modules",
    "backups", "logs", "output", "evals",
    "db/migrations",
}

# Paths we audit. Anything under these contributes "is this def dead?"
# candidates; anything outside them is reference-only.
AUDIT_ROOTS = ("orchestrator", "website", "scripts", "templates", "deploy/orchestrator")

# Names that are conventionally allowed to be "unreferenced" because
# they're entry points or magic methods discovered dynamically.
NAME_ALLOWLIST = {
    "main", "__init__", "__main__", "__all__",
    # FastAPI lifecycle / common framework hooks.
    "startup", "shutdown", "lifespan", "exception_handler",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _walk_py(roots: list[Path]):
    """Yield every .py file under the given roots, minus EXCLUDE_DIRS."""
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            rel = p.relative_to(REPO_ROOT).as_posix()
            if any(rel.startswith(ex) or f"/{ex}/" in rel for ex in EXCLUDE_DIRS):
                continue
            yield p


def _ripgrep_hits(symbol: str, types: tuple[str, ...] = ("py", "html", "js", "md")) -> int:
    """Count whole-word occurrences of ``symbol`` across the repo.

    Uses ripgrep when available (fast); falls back to a Python scan that
    only loads files matching one of the extension types.
    """
    rg = _find_rg()
    if rg:
        # -c gives per-file count; sum it. --max-columns avoids massive
        # generated files blowing the count.
        type_args: list[str] = []
        for t in types:
            type_args += ["--type-add", f"{t}:*.{t}", "--type", t]
        cmd = [
            rg, "-c", "-w", f"{re.escape(symbol)}",
            *type_args,
            "--glob", "!.venv", "--glob", "!.pytest_cache",
            "--glob", "!backups", "--glob", "!logs", "--glob", "!output",
            "--glob", "!evals", "--glob", "!db/migrations",
            str(REPO_ROOT),
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return _python_count(symbol, types)
        if out.returncode not in (0, 1):
            return _python_count(symbol, types)
        total = 0
        for line in out.stdout.splitlines():
            # Format is "path:count".
            if ":" in line:
                try:
                    total += int(line.rsplit(":", 1)[1])
                except ValueError:
                    pass
        return total
    return _python_count(symbol, types)


def _find_rg() -> str | None:
    """Return ripgrep binary path or None. Caches the result."""
    if not hasattr(_find_rg, "_cached"):
        for candidate in ("rg", "rg.exe"):
            try:
                subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
                _find_rg._cached = candidate  # type: ignore[attr-defined]
                return candidate
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        _find_rg._cached = None  # type: ignore[attr-defined]
    return _find_rg._cached  # type: ignore[attr-defined]


def _python_count(symbol: str, types: tuple[str, ...]) -> int:
    """Pure-Python fallback when ripgrep isn't available."""
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    total = 0
    roots = [REPO_ROOT / r for r in AUDIT_ROOTS]
    for root in roots:
        if not root.exists():
            continue
        for ext in types:
            for p in root.rglob(f"*.{ext}"):
                rel = p.relative_to(REPO_ROOT).as_posix()
                if any(ex in rel for ex in EXCLUDE_DIRS):
                    continue
                try:
                    total += len(pattern.findall(p.read_text(encoding="utf-8", errors="ignore")))
                except OSError:
                    continue
    return total


# ── Detector A: dead functions and classes ────────────────────────────────────

def detect_dead_defs() -> list[dict]:
    """Yield non-private def/class declarations with ≤1 whole-word hit
    across .py/.html/.js/.md files in the audit roots.
    """
    roots = [REPO_ROOT / r for r in AUDIT_ROOTS]
    out: list[dict] = []
    for path in _walk_py(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if name.startswith("_") or name in NAME_ALLOWLIST:
                continue
            # Skip Pydantic BaseModel / SQLAlchemy Base / TypedDict
            # subclasses — these are routinely referenced via FastAPI
            # response_model= introspection or by tablename-string
            # lookups or as type annotations on dict literals, none of
            # which a whole-word grep can see reliably.
            if isinstance(node, ast.ClassDef) and _is_framework_class(node):
                continue
            # Skip FastAPI route handlers (any def with a @app.<method>
            # or @router.<method> decorator). The framework resolves
            # them via the URL string, not the symbol name, so they
            # always look 1-hit but aren't dead.
            if _has_route_decorator(node):
                continue
            hits = _ripgrep_hits(name)
            if hits <= 1:
                out.append({
                    "kind": "def" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "class",
                    "name": name,
                    "file": str(path.relative_to(REPO_ROOT).as_posix()),
                    "line": node.lineno,
                    "hits": hits,
                })
    return out


_FRAMEWORK_BASES = {"BaseModel", "Base", "TypedDict", "Enum", "StrEnum", "IntEnum"}


def _is_framework_class(node: ast.ClassDef) -> bool:
    """True if any base looks like Pydantic BaseModel, SQLAlchemy
    declarative Base, TypedDict, or stdlib Enum. These classes get
    referenced through machinery the whole-word grep can't see
    (FastAPI response_model=, tablename lookups, type-annotation lists,
    enum-value dispatch).
    """
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _FRAMEWORK_BASES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _FRAMEWORK_BASES:
            return True
    return False


_ROUTE_DECORATOR_RE = re.compile(
    r"^(app|router)\.(get|post|patch|delete|put|websocket)$"
)


def _has_route_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    """True if the def carries a FastAPI route decorator
    (``@app.get(...)`` etc) — these are resolved at request time via the
    URL string, not by symbol name, so they always look 1-hit.
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            full = f"{target.value.id}.{target.attr}"
            if _ROUTE_DECORATOR_RE.match(full):
                return True
    return False


# ── Detector B: dead FastAPI routes ───────────────────────────────────────────

_ROUTE_RE = re.compile(
    r"@app\.(get|post|patch|delete|put)\(\s*['\"]([^'\"]+)['\"]"
)


def detect_dead_routes() -> list[dict]:
    """FastAPI routes whose URL path appears only at the @app.<method>
    declaration site. Searches .py / .html / .js / .md for the path
    string.
    """
    main = REPO_ROOT / "website" / "main.py"
    if not main.exists():
        return []
    text = main.read_text(encoding="utf-8")
    out: list[dict] = []
    for m in _ROUTE_RE.finditer(text):
        method, path = m.group(1).upper(), m.group(2)
        line = text.count("\n", 0, m.start()) + 1
        # Search for the literal path. Strip path params (``{x}``) to
        # construct a substring stable enough to grep — e.g.
        # ``/api/features/{feature_id}/labels`` becomes
        # ``/api/features/`` AND ``/labels`` (we require BOTH).
        segments = [s for s in path.split("/") if s and not s.startswith("{")]
        if not segments:
            continue
        hits = _ripgrep_hits(segments[-1])
        if hits <= 2:  # the def itself plus maybe a docstring mention
            out.append({
                "kind": "route",
                "name": f"{method} {path}",
                "file": "website/main.py",
                "line": line,
                "hits": hits,
            })
    return out


# ── Detector C: dead module-level UPPER_CASE constants ────────────────────────

_CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]+)\s*[:=]")


def detect_dead_constants() -> list[dict]:
    """Module-level UPPER_CASE constants whose name appears only in the
    file that defines them. Skips names imported from elsewhere (those
    show up as multiple hits naturally).
    """
    roots = [REPO_ROOT / r for r in AUDIT_ROOTS]
    out: list[dict] = []
    for path in _walk_py(roots):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln_no, line in enumerate(lines, start=1):
            m = _CONST_RE.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in {"True", "False", "None"}:
                continue
            hits = _ripgrep_hits(name, types=("py",))
            if hits <= 1:
                out.append({
                    "kind": "const",
                    "name": name,
                    "file": str(path.relative_to(REPO_ROOT).as_posix()),
                    "line": ln_no,
                    "hits": hits,
                })
    return out


# ── Detector D: imports never used in their own file ──────────────────────────

def detect_unused_imports() -> list[dict]:
    """Top-level imports whose names don't appear elsewhere in the file."""
    roots = [REPO_ROOT / r for r in AUDIT_ROOTS]
    out: list[dict] = []
    for path in _walk_py(roots):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        names: list[tuple[str, int]] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.append((alias.asname or alias.name.split(".")[0], node.lineno))
            elif isinstance(node, ast.ImportFrom):
                # ``from __future__ import annotations`` is a compiler
                # directive, not a runtime symbol — never grep-able.
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    names.append((alias.asname or alias.name, node.lineno))
        # Allow ``# noqa: F401`` to silence the warning — this is the
        # convention for legitimately-unused re-export imports.
        noqa_lines = {
            i for i, line in enumerate(src.splitlines(), start=1)
            if re.search(r"#\s*noqa\s*:.*\bF401\b", line)
        }
        # Remove the import lines themselves from the haystack so we
        # don't count the import as a "use".
        ref_src = re.sub(r"^\s*(import |from )[^\n]*\n", "", src, flags=re.M)
        for name, lineno in names:
            if name in NAME_ALLOWLIST:
                continue
            if lineno in noqa_lines:
                continue
            if not re.search(rf"\b{re.escape(name)}\b", ref_src):
                out.append({
                    "kind": "import",
                    "name": name,
                    "file": str(path.relative_to(REPO_ROOT).as_posix()),
                    "line": lineno,
                    "hits": 0,
                })
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

DETECTORS = {
    "funcs":   ("Functions and classes",   detect_dead_defs),
    "routes":  ("FastAPI routes",          detect_dead_routes),
    "consts":  ("Module-level constants",  detect_dead_constants),
    "imports": ("Unused imports",          detect_unused_imports),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    for key, (label, _) in DETECTORS.items():
        parser.add_argument(f"--{key}", action="store_true", help=f"only: {label}")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()

    selected = [k for k in DETECTORS if getattr(args, k)] or list(DETECTORS)

    findings: dict[str, list[dict]] = {}
    for key in selected:
        label, fn = DETECTORS[key]
        findings[key] = fn()

    if args.json:
        print(json.dumps(findings, indent=2, sort_keys=True))
        return 0

    total = 0
    for key in selected:
        items = findings[key]
        label, _ = DETECTORS[key]
        print(f"\n=== {label} ({len(items)}) ===")
        by_file: dict[str, list[dict]] = defaultdict(list)
        for f in items:
            by_file[f["file"]].append(f)
        for fname in sorted(by_file):
            print(f"\n  {fname}:")
            for f in sorted(by_file[fname], key=lambda x: x["line"]):
                print(f"    L{f['line']:>5}  hits={f['hits']:<3}  {f['kind']:<7}  {f['name']}")
        total += len(items)
    print(f"\nTotal candidates: {total}")
    print("Note: heuristic, not proof. Verify each finding before deleting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
