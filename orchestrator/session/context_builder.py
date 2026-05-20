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
        return _render_block(canonical, discovered, deprecated, entry_points)
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
