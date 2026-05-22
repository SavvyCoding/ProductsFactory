#!/usr/bin/env python3
"""
Pre-commit self-review for accidental code deletion.

Mirrors the orchestrator's post-coder Guard 17 (AST-diff deletion safety)
so the coder can verify locally BEFORE committing — and avoid bouncing
back to Implementing with `changes_requested`. The orchestrator runs the
same check after push; this script is the agent's chance to catch it first.

Usage (run from product working directory):
    python check_deletion_safety.py

Exit:
    0  — no dangling deletions detected
    1  — at least one removed top-level symbol still has a surviving caller

Scope: Python files only (.py). Inspects MODIFIED + DELETED files vs HEAD,
extracts removed public top-level def/async def/class/module assignments,
then word-greps surviving callers in tracked .py files. Files you've also
co-modified are excluded — the assumption is you're already aware of those.

This is a deterministic check (no LLM, no network). Whole-word grep means
common names (`name`, `run`, `get`) can false-positive on local-variable
matches; the report names the symbol + caller paths so you can verify in
seconds. If the report is wrong, fix the real issue (or restore the
deletion) and re-run.
"""

import ast
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _top_level_names(src: str) -> set[str]:
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                    names.add(tgt.id)
    return names


def _changed_py_files(filter_spec: str) -> set[str]:
    """Files changed vs HEAD matching the diff-filter (e.g. 'MD', 'AMD')."""
    try:
        r = _run(["git", "diff", "--name-only", f"--diff-filter={filter_spec}", "HEAD"])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    if r.returncode != 0:
        return set()
    return {
        f.strip() for f in (r.stdout or "").splitlines()
        if f.strip().endswith(".py")
    }


def _file_old(path: str) -> str | None:
    """Content of `path` at HEAD, or None if file did not exist."""
    try:
        r = _run(["git", "show", f"HEAD:{path}"])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return r.stdout if r.returncode == 0 else None


def _file_new(path: str) -> str:
    """Content of `path` in the working tree, or '' if deleted/missing."""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _surviving_callers(name: str, all_changed: set[str]) -> list[str]:
    try:
        r = _run(["git", "grep", "-l", "-w", name, "--", "*.py"], timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if r.returncode != 0:  # no matches OR error
        return []
    hits = [h.strip() for h in (r.stdout or "").splitlines() if h.strip()]
    return [h for h in hits if h not in all_changed]


def main() -> int:
    md_files = _changed_py_files("MD")  # modified + deleted .py files
    if not md_files:
        print("[deletion-safety] no modified/deleted .py files vs HEAD — OK")
        return 0

    # Any file the coder also touched in this commit is treated as "you're
    # aware of it" — mirror the orchestrator's Guard 17 semantics.
    all_changed = _changed_py_files("AMD") or md_files

    removed: list[tuple[str, str]] = []  # (path, symbol)
    for path in sorted(md_files):
        old_src = _file_old(path)
        if old_src is None:
            continue  # path didn't exist at HEAD — nothing was deleted
        new_src = _file_new(path)
        for name in sorted(_top_level_names(old_src) - _top_level_names(new_src)):
            removed.append((path, name))

    if not removed:
        print("[deletion-safety] no public top-level symbols removed — OK")
        return 0

    dangling: list[tuple[str, str, list[str]]] = []
    for path, name in removed:
        callers = _surviving_callers(name, all_changed)
        if callers:
            dangling.append((path, name, callers))

    if not dangling:
        print(
            f"[deletion-safety] {len(removed)} symbol(s) removed but no "
            f"surviving callers — OK"
        )
        return 0

    print(f"[deletion-safety] FAIL — {len(dangling)} dangling deletion(s):\n")
    for path, name, callers in dangling:
        sample = "\n      ".join(callers[:5])
        more = f"\n      ... ({len(callers) - 5} more)" if len(callers) > 5 else ""
        print(f"  - `{name}` (removed from {path}) still referenced in:")
        print(f"      {sample}{more}")
        print(
            f"    Decide: restore `{name}` in {path}, "
            f"OR update the caller(s) above.\n"
        )
    print(
        "Fix one of the two and re-run before committing. The orchestrator's "
        "post-coder Guard 17 will fire on the same condition after push."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
