"""
Pre-coder context augmentation (Phase 7 of quality-specs, 2026-05-19).

Builds the `{related_existing_code}` block that gets injected into the coder
prompt before each session. The block lists canonical modules + discovered
exports for the area this feature touches. Goal: stop the coder from
creating parallel implementations of concepts that already have a canonical
module (StockAnalysis's two user-stores, Calculator's five main.py variants).

Inputs:
  - working_dir: the product's repo root
  - feature: dict with at least "name" and "description"
  - architecture_md: the product's ARCHITECTURE.md content (Phase 1 sections)
  - max_symbols: cap to keep the prompt budget bounded

Output: markdown block (empty string on any failure or when no relevant
modules are found). The coder prompt should render this above the feature
description so the coder sees it before writing any code.

Best-effort throughout — any exception bubbles up to empty string. The coder
session must NEVER fail to launch because this builder couldn't find a
canonical module.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("orchestrator.session.context_builder")

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "in", "of", "on", "off",
    "with", "is", "be", "by", "as", "at", "this", "that", "from", "into",
    "via", "use", "using", "add", "new", "support", "implement", "create",
    "build", "make", "feature", "story", "chore", "bug", "fix", "update",
    "should", "will", "must", "can", "may", "all", "any", "each", "per",
    "user", "users",  # too generic — drop
}

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")
_PY_SYMBOL_RE = re.compile(r"^(def|class|async def)\s+(\w+)", re.M)
_JS_EXPORT_RE = re.compile(
    r"export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|const|let|var|class|interface|type)\s+(\w+)"
)


def build_related_code_context(
    working_dir: str,
    feature: dict,
    architecture_md: str = "",
    max_symbols: int = 30,
) -> str:
    """Build the markdown block. Returns empty string on failure or no-match."""
    try:
        keywords = _extract_keywords(feature)
        canonical = _parse_modules_section(architecture_md, keywords)
        deprecated = _parse_deprecated_section(architecture_md)
        entry_points = _parse_entry_points_section(architecture_md)
        # If MODULES gave us something, scan its directories for additional
        # exports. Otherwise guess areas from keyword + top-level dir match.
        if canonical:
            seen = set()
            area_dirs: list[Path] = []
            for c in canonical:
                p = Path(c["module"]).parent
                key = str(p)
                if key not in seen:
                    seen.add(key)
                    area_dirs.append(p)
        else:
            area_dirs = _guess_area_dirs(working_dir, keywords)
        discovered = _ast_scan_dirs(working_dir, area_dirs, max_symbols)
        log.info(
            "[phase-7] feature=%s keywords=%d canonical=%d discovered=%d "
            "deprecated=%d entry_points=%d",
            feature.get("id") or feature.get("name") or "?",
            len(keywords), len(canonical), len(discovered),
            len(deprecated), len(entry_points),
        )
        focused = _render_block(canonical, discovered, deprecated, entry_points)
        # Repo-map v0 (2026-05-29): always-included, whole-repo route+symbol
        # inventory. Independent of MODULES staleness / keyword matching so
        # the coder structurally sees every registered route before editing —
        # canonical calc3 #1025/#1031 case where coders editing main.py for
        # an unrelated feature clobbered /api/auth/login and the read-before-
        # edit prompt didn't reliably prevent it.
        repo_map = build_repo_map(working_dir)
        parts = [b for b in (repo_map, focused) if b]
        return "\n\n".join(parts)
    except Exception:
        log.debug("build_related_code_context failed (returning empty)", exc_info=True)
        return ""


def _extract_keywords(feature: dict) -> list[str]:
    """Significant tokens from feature.name + description, deduped, lowercased."""
    text = (feature.get("name") or "") + " " + (feature.get("description") or "")
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    # Cap at 20 — too many keywords matches too liberally
    return out[:20]


def _section_blob(md: str, section_name: str) -> str:
    """Return the body of `## <section_name>` up to the next `## ` heading or EOF."""
    if not md:
        return ""
    pat = re.compile(rf"##\s+{re.escape(section_name)}\b.*?(?=\n##\s|\Z)", re.S)
    m = pat.search(md)
    return m.group(0) if m else ""


def _parse_modules_section(md: str, keywords: list[str]) -> list[dict]:
    """Parse rows of the MODULES table that match any keyword."""
    section = _section_blob(md, "MODULES")
    if not section or not keywords:
        return []
    rows = re.findall(
        r"^\|\s*([^|\n]+?)\s*\|\s*([^|\n]+?)\s*\|\s*([^|\n]*?)\s*\|\s*([^|\n]*?)\s*\|$",
        section, re.M,
    )
    result = []
    for concern, module, owns, notes in rows:
        # Skip header / separator / placeholder rows
        if not concern.strip() or "---" in concern:
            continue
        lc = concern.lower()
        if lc.startswith("concern") or "populated" in lc:
            continue
        if "_(" in concern or concern.strip() in ("", "—", "-"):
            continue
        haystack = (concern + " " + module + " " + owns + " " + notes).lower()
        if any(kw in haystack for kw in keywords):
            result.append({
                "concern": concern.strip(),
                "module": module.strip().strip("`"),
                "owns": owns.strip(),
                "notes": notes.strip(),
            })
    return result[:10]


def _parse_deprecated_section(md: str) -> list[str]:
    section = _section_blob(md, "DEPRECATED")
    if not section:
        return []
    items = re.findall(r"^\s*-\s+([^\n]+)", section, re.M)
    out = []
    for it in items:
        if "populated" in it.lower() or "_(" in it:
            continue
        out.append(it.strip())
    return out[:10]


def _parse_entry_points_section(md: str) -> list[dict]:
    section = _section_blob(md, "ENTRY POINTS")
    if not section:
        return []
    rows = re.findall(
        r"^\|\s*([^|\n]+?)\s*\|\s*([^|\n]+?)\s*\|\s*([^|\n]*?)\s*\|$",
        section, re.M,
    )
    out = []
    for concern, fil, notes in rows:
        lc = concern.lower()
        if lc.startswith("concern") or "---" in concern or "_(" in fil:
            continue
        out.append({"concern": concern.strip(), "file": fil.strip().strip("`"),
                    "notes": notes.strip()})
    return out


def _guess_area_dirs(working_dir: str, keywords: list[str]) -> list[Path]:
    """Fallback when MODULES is empty: top-level source dirs whose names
    contain any keyword."""
    wd = Path(working_dir)
    if not wd.exists():
        return []
    candidates = []
    for base_name in ("SRC", "src", "lib", "app", "internal", "pages"):
        base = wd / base_name
        if not base.exists() or not base.is_dir():
            continue
        try:
            for sub in base.iterdir():
                if sub.is_dir() and any(kw in sub.name.lower() for kw in keywords):
                    candidates.append(sub.relative_to(wd))
        except Exception:
            continue
    return candidates[:3]


def _ast_scan_dirs(working_dir: str, dirs: list[Path], max_symbols: int) -> list[dict]:
    """For each dir, list source files and their top-level public symbols."""
    wd = Path(working_dir)
    out = []
    for d in dirs:
        d_path = (wd / d) if not Path(d).is_absolute() else Path(d)
        if not d_path.exists() or not d_path.is_dir():
            continue
        try:
            for f in d_path.iterdir():
                if not f.is_file():
                    continue
                symbols: list[str] = []
                if f.suffix == ".py":
                    symbols = _extract_python_symbols(f)
                elif f.suffix in (".js", ".jsx", ".ts", ".tsx"):
                    symbols = _extract_js_symbols(f)
                if symbols:
                    rel = str(f.relative_to(wd)).replace("\\", "/")
                    out.append({"path": rel, "symbols": symbols[:6]})
                    if len(out) >= max_symbols:
                        return out
        except Exception:
            continue
    return out


def _extract_python_symbols(file_path: Path) -> list[str]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                out.append(node.name)
    return out


def _extract_js_symbols(file_path: Path) -> list[str]:
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    out: list[str] = []
    for m in _JS_EXPORT_RE.finditer(content):
        out.append(m.group(1))
    return out


def _render_block(
    canonical: list[dict],
    discovered: list[dict],
    deprecated: list[str],
    entry_points: list[dict],
) -> str:
    if not (canonical or discovered or deprecated or entry_points):
        return ""
    lines = [
        "## Existing modules in your problem area",
        "",
        "Before writing new code, USE the modules listed below — they're the "
        "canonical implementations for this area. Do not create parallel "
        "`*Repository` / `*Store` / `*Service` / `*_v2` files alongside them. "
        "(The StockAnalysis two-user-stores and Calculator five-main.py "
        "patterns trace back to coders not seeing what already existed.)",
        "",
    ]
    if entry_points:
        lines.append("### Entry points — extend, don't replace")
        for ep in entry_points:
            n = f" ({ep['notes']})" if ep.get("notes") else ""
            lines.append(f"- **{ep['concern']}**: `{ep['file']}`{n}")
        lines.append("")
    if canonical:
        lines.append("### Canonical modules from ARCHITECTURE.md MODULES")
        lines.append("")
        for m in canonical:
            owns = ""
            if m["owns"] and "populated" not in m["owns"].lower():
                owns = f" — owns: {m['owns']}"
            notes = ""
            if m["notes"] and m["notes"] not in ("-", "—"):
                notes = f"  _{m['notes']}_"
            lines.append(f"- **{m['concern']}**: `{m['module']}`{owns}{notes}")
        lines.append("")
    if discovered:
        lines.append("### Files discovered in the same area")
        lines.append("")
        for d in discovered[:12]:
            syms = ", ".join(d["symbols"]) if d["symbols"] else "(no public symbols)"
            lines.append(f"- `{d['path']}` — {syms}")
        lines.append("")
    if deprecated:
        lines.append("### ⚠ DEPRECATED — do not use, do not re-create with a new name")
        lines.append("")
        for d in deprecated:
            lines.append(f"- {d}")
        lines.append("")
    return "\n".join(lines)


# ── Repo-map v0 (always-included, structural) ────────────────────────────────
#
# Whole-repo, AST-derived inventory of (a) every registered HTTP route and
# (b) every file's top-level public symbols. Reads CODE (not ARCHITECTURE.md),
# so it can't go stale. Always included regardless of feature keywords, so the
# coder structurally sees every existing route before editing a multi-route
# file — preventing the canonical clobber pattern where a feature editing
# main.py for one route drops unrelated routes (calc3 #1025/#1031).

_FLASK_FASTAPI_ROUTE_DECOS = frozenset({"route", "get", "post", "put", "patch", "delete"})

_RM_EXCLUDE_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    "Temp", "Results", "dist", "build", ".pytest_cache", ".mypy_cache",
    "tests", "test", "migrations",
})

_RM_EXCLUDE_PATH_SUBSTR = ("alembic/versions", "db/migrations")

# Per-file symbol cap and overall budget. Routes get unconditional inclusion
# (small + highest-signal); symbols fill remaining budget then truncate.
_RM_SYMBOLS_PER_FILE = 8
_RM_DEFAULT_MAX_CHARS = 2500


def _rm_iter_py_files(working_dir: Path):
    """Yield .py files under `working_dir`, pruning excluded dirs."""
    if not working_dir.is_dir():
        return
    for root, dirs, files in os.walk(working_dir):
        dirs[:] = [d for d in dirs if d not in _RM_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, working_dir).replace("\\", "/")
        if any(sub in rel_root for sub in _RM_EXCLUDE_PATH_SUBSTR):
            continue
        for fn in files:
            if fn.endswith(".py"):
                yield Path(root) / fn


def _rm_extract_routes_from_tree(tree: ast.AST, rel_path: str) -> list[tuple]:
    """Return [(method, path, file, func_name), ...] for every Flask/FastAPI
    route decorator in `tree`. Handles `@app.route("/p", methods=[...])` and
    `@app.get/post/...("/p")` / `@router.get(...)`."""
    out: list[tuple] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if not isinstance(d, ast.Call):
                continue
            if not isinstance(d.func, ast.Attribute):
                continue
            attr = d.func.attr
            if attr not in _FLASK_FASTAPI_ROUTE_DECOS:
                continue
            # First positional arg = path literal.
            if not d.args or not isinstance(d.args[0], ast.Constant) \
                    or not isinstance(d.args[0].value, str):
                continue
            path = d.args[0].value
            # `methods=[...]` kwarg only on Flask `route()`; otherwise method
            # is the decorator name (`get` → GET, etc.).
            methods: list[str] = []
            for kw in d.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            methods.append(elt.value)
                    break
            if not methods:
                methods = ["GET"] if attr == "route" else [attr.upper()]
            for m in methods:
                out.append((m.upper(), path, rel_path, node.name))
    return out


def _rm_extract_module_surface(tree: ast.AST) -> list[tuple]:
    """Return [(name, kind, signature), ...] for top-level public defs/classes
    (skips `_`-prefixed). signature is `ast.unparse(node.args)` for funcs."""
    out: list[tuple] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            try:
                sig = ast.unparse(node.args)  # py 3.9+
            except Exception:
                sig = ""
            out.append((node.name, "def", sig))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            out.append((node.name, "class", ""))
    return out


def _rm_collect(working_dir: str) -> tuple[list[tuple], list[tuple]]:
    """Walk the repo once; return (routes, files_with_symbols)."""
    wd = Path(working_dir)
    routes: list[tuple] = []
    files: list[tuple] = []
    for fpath in _rm_iter_py_files(wd):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except Exception:
            continue
        rel = str(fpath.relative_to(wd)).replace("\\", "/")
        routes.extend(_rm_extract_routes_from_tree(tree, rel))
        symbols = _rm_extract_module_surface(tree)
        if symbols:
            files.append((rel, symbols))
    return routes, files


def _render_repo_map(routes: list[tuple], files: list[tuple], max_chars: int) -> str:
    if not routes and not files:
        return ""
    header = (
        "## Repository map — every registered route + module surface\n\n"
        "**This is the COMPLETE route table and module surface of this product, "
        "auto-extracted from the code on every session. Before editing a file, "
        "scan this map: if your target file (e.g. `src/main.py`) appears below, "
        "every route and function listed for it currently exists — your edit must "
        "preserve them. Never regenerate a file from scratch; in-place edits only.**\n\n"
    )
    if routes:
        rows = sorted(set(routes), key=lambda r: (r[1], r[0]))   # by path, then method
        # Compact fixed-width rendering.
        path_w = max((len(r[1]) for r in rows), default=8)
        path_w = min(path_w, 40)
        route_lines = ["### Registered HTTP routes", "```"]
        for method, path, file, func in rows:
            route_lines.append(f"{method:<7} {path:<{path_w}}  {file}:{func}")
        route_lines.append("```\n")
    else:
        route_lines = []
    routes_block = "\n".join(route_lines)
    current = header + routes_block
    if not files or len(current) >= max_chars:
        return current.rstrip() + "\n"
    # Module surface — append until budget hit. Per-file cap, then overall truncation.
    sym_lines = ["### Module surface (top-level public symbols)", "```"]
    for rel, symbols in sorted(files, key=lambda x: x[0]):
        sym_lines.append(f"{rel}:")
        for name, kind, sig in symbols[:_RM_SYMBOLS_PER_FILE]:
            if kind == "class":
                sym_lines.append(f"  class {name}")
            else:
                sym_lines.append(f"  def   {name}({sig})")
        if len(symbols) > _RM_SYMBOLS_PER_FILE:
            sym_lines.append(f"  … +{len(symbols) - _RM_SYMBOLS_PER_FILE} more")
    sym_lines.append("```\n")
    sym_block = "\n".join(sym_lines)
    remaining = max_chars - len(current) - 60
    if remaining > 0 and len(sym_block) > remaining:
        sym_block = sym_block[:remaining] + "\n…[symbol list truncated to fit budget]\n```\n"
    return (current + "\n" + sym_block).rstrip() + "\n"


def build_repo_map(working_dir: str, max_chars: int = _RM_DEFAULT_MAX_CHARS) -> str:
    """Whole-repo route + symbol inventory as a markdown block.

    Always reads code (not ARCHITECTURE.md), so it cannot lag MODULES. Best-
    effort: any failure returns empty (must never block session launch)."""
    try:
        wd = Path(working_dir)
        if not wd.is_dir():
            return ""
        routes, files = _rm_collect(str(wd))
        return _render_repo_map(routes, files, max_chars)
    except Exception:
        log.debug("build_repo_map failed (returning empty)", exc_info=True)
        return ""
