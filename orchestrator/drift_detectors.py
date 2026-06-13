"""
Deterministic drift detectors — Option 1 spike (2026-05-28).

Detectors run after each post-coder cycle and post findings as
``feature_comments`` rows with ``author="drift-scanner"``. No LLM, no
network beyond a single PM-API POST per finding, ~10ms per detector on
a small repo.

Designed to be the cheap-and-fast layer beneath the architect persona:
the architect runs every N hours with full reasoning; these detectors
run every cycle and catch the deterministic patterns that don't need
reasoning to spot. Findings show up in ``{reviewer_feedback}`` for the
next coder cycle, the same way lint-guard violations do.

Each detector returns a list of ``Finding`` dataclasses with no side
effects. ``run_all`` runs them in parallel-safe order. ``post_findings``
walks the result list and POSTs feature_comments via the PM API.

To add a detector:
  1. Write a ``detect_<name>(working_dir, features) -> list[Finding]``
     function that's pure (no I/O outside the working_dir and the
     features list passed in).
  2. Add it to ``_DETECTORS`` below.
  3. Add a test in ``tests/test_drift_detectors.py``.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger("poller.docker")


# Findings shorter than this don't carry their own metadata block when
# posted as a comment — they get a one-liner. Longer findings get a
# fenced markdown block so the reviewer/coder can scan them quickly.
_FINDING_INLINE_LIMIT = 200


@dataclass
class Finding:
    """A single drift signal.

    Fields:
        category: stable string id, e.g. "shell_artifact". Used to
            dedupe findings across runs — if the same (category,
            target_id) was already posted as a comment in the last 24h,
            skip.
        severity: "low" | "medium" | "high". Currently informational
            only; reserved for future routing (e.g. high → file chore).
        target_type: "file" | "feature" | "doc". Hints which DB row the
            finding refers to.
        target_id: identifier within target_type (file path or feature
            id stringified).
        feature_id: the feature to attach the comment to. Often the
            first assigned feature for this session; for file-scoped
            findings it's a best-effort attribution.
        detail: human-readable description, one paragraph max.
        fix_hint: one-line recommendation. Read by the next coder via
            {reviewer_feedback} injection.
    """
    category: str
    severity: str
    target_type: str
    target_id: str
    feature_id: int
    detail: str
    fix_hint: str
    # Worklist fields (PR: reconciler-chore-controller). Optional with
    # defaults so the original comment-mode detectors construct unchanged.
    #   occurrences: located violation sites ("src/history.py:20", ...) — lets
    #     a code-drift finding enumerate every offending location in the chore.
    #   product_id: product-wide drift (e.g. duplicate DDL) has no single
    #     feature anchor; the chore is filed against the product.
    #   dedupe_key: stable identity used to (a) skip re-filing a chore that's
    #     already open and (b) detect regressions. Defaults to category:target_id.
    occurrences: list[str] = field(default_factory=list)
    product_id: int | None = None
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            self.dedupe_key = f"{self.category}:{self.target_id}"

    def as_comment_body(self) -> str:
        """Render as a feature_comment body."""
        return (
            f"❌ drift-scanner: {self.category} ({self.severity})\n"
            f"- target: {self.target_type} `{self.target_id}`\n"
            f"- detail: {self.detail}\n"
            f"- fix: {self.fix_hint}"
        )


# ────────────────────────────────────────────────────────────────────────────
# Detector 1: shell-artifact filenames
# ────────────────────────────────────────────────────────────────────────────

_SHELL_ARTIFACT_LEADERS = frozenset("=<>|&")


def detect_shell_artifact_files(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Files whose basename starts with a shell comparator / redirect /
    pipe / job-control char. Created when an unquoted shell command misfires:

        pip install flask>=3.0,<4   # → file named `=3.0,` at cwd

    The post-coder commit-staging filter (PR #25) strips these from new
    commits, but older products may have them already committed.
    Canonical 2026-05-27 calcv2 incident.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    findings: list[Finding] = []
    target_feature_id = _pick_target_feature(features)
    if target_feature_id is None:
        return []
    for entry in wd.iterdir():
        if not entry.is_file():
            continue
        if entry.name and entry.name[0] in _SHELL_ARTIFACT_LEADERS:
            findings.append(Finding(
                category="shell_artifact",
                severity="medium",
                target_type="file",
                target_id=entry.name,
                feature_id=target_feature_id,
                detail=(
                    "File whose name starts with a shell metacharacter "
                    "(=, <, >, |, &). Created by an unquoted shell command, "
                    "e.g. `pip install pkg>=1.0,<2`. Never legitimate."
                ),
                fix_hint=(
                    f"Delete the file: `rm -- '{entry.name}'`. "
                    "Quote version-range arguments to pip/shell commands "
                    "going forward."
                ),
            ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 2: design_doc_path set in DB but file missing
# ────────────────────────────────────────────────────────────────────────────


def _ls_tree_origin_main(working_dir: Path) -> set[str] | None:
    """Return the set of file paths tracked on ``origin/main`` (or
    ``origin/master`` if main isn't present). Returns ``None`` when the
    lookup fails so the caller can fall back to the working-tree check.

    Used by detect_design_doc_mismatch to decide whether a feature's
    declared design_doc_path landed on main — answering the real
    question, "did the designer's commit actually merge?" — rather
    than the working-tree question, which gives stale-branch false
    positives when the scan runs on a coder session branch cut before
    sibling design-doc commits landed.

    Canonical false-positive cycles (all on coder session branches that
    pre-dated subsequent designer commits to main): 2026-06-03 cycle
    JN (8 MyTracking features wrongly reset after PR #3 merged), cycle
    JQ (DocumentSign 1301/1302 wrongly reset after 1131's PR #332
    force-push), cycle JU (9 more MyTracking features wrongly reset).
    """
    for ref in ("origin/main", "origin/master"):
        try:
            result = subprocess.run(
                ["git", "-C", str(working_dir), "ls-tree", "-r",
                 "--name-only", ref],
                capture_output=True, text=True, timeout=15, check=True,
            )
            return set(result.stdout.splitlines())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError):
            continue
    return None


def detect_design_doc_mismatch(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Features whose ``design_doc_path`` points to a file that doesn't
    exist on ``origin/main``. The DB believes the designer landed a
    doc; main disagrees. Real-world cause (2026-05-27 calcv2, features
    995/996): designer wrote the doc to /workspace/docs/ and posted
    ``{"design_doc_path": ...}`` to session_result.json, but the
    post-doc commit didn't include the file (e.g. allowlist stripped it
    or it was wiped by a workspace reset before the commit). Reconcile
    updates the DB from session_result.json; the file never lands on
    main.

    Future coder sessions on the same feature read an empty
    design_doc_path → /workspace/docs/X.md, find nothing, improvise.

    Why check origin/main rather than the working tree: drift-scanner
    runs after the post-coder pipeline, on the session branch's
    working tree. That branch was cut from main at some prior moment;
    every sibling design-doc commit that landed on main AFTER the
    branch was cut is correctly absent from the working tree but
    present on main. The working-tree check produced ~20 false
    positives across cycles JN/JQ/JU (2026-06-03), each wasting a
    designer re-run for a doc that already existed on main. Switching
    to origin/main eliminates this entirely. Fallback to working tree
    when the git lookup fails (degenerates to the prior behavior).
    """
    wd = Path(working_dir)
    main_files = _ls_tree_origin_main(wd)
    findings: list[Finding] = []
    for f in features or []:
        doc_path = f.get("design_doc_path") or ""
        if not doc_path:
            continue
        fid = f.get("id")
        if not isinstance(fid, int):
            continue
        if main_files is not None:
            if doc_path in main_files:
                continue
        else:
            if (wd / doc_path).is_file():
                continue
        findings.append(Finding(
            category="design_doc_missing",
            severity="high",
            target_type="feature",
            target_id=str(fid),
            feature_id=fid,
            detail=(
                f"Feature #{fid} has design_doc_path=`{doc_path}` in the "
                "DB, but that file does not exist on origin/main. "
                "Coder sessions on this feature have no spec to read."
            ),
            fix_hint=(
                "Either: PM clears design_doc_path so the designer "
                "re-runs and writes a fresh doc; OR an operator restores "
                "the doc from a prior branch / session_result archive."
            ),
        ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 3: placeholder template content in ARCHITECTURE.md
# ────────────────────────────────────────────────────────────────────────────


_PLACEHOLDER_MARKERS = (
    "Example rows (replace as the product grows):",
    "Example rows:",
    # The current canonical placeholder (post PR #25). Detect it ONLY
    # when there's ALSO a real MODULES row populated — meaning the
    # architect added real entries but didn't remove the placeholder.
    "_(populated by the architect persona as features land",
)


def detect_placeholder_template_content(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """ARCHITECTURE.md still contains renderer-template placeholder
    content alongside real entries. Canonical 2026-05-27 calcv2: the
    architect populated MODULES with real `Calculation engine` /
    `Calculation history` / `Flask application` rows but didn't remove
    the "Example rows" placeholder block beneath, so pre-coder context
    pulled BOTH the real entries AND phantom references to nonexistent
    `src/auth/verify.py`, `src/users/user_store.py`, etc.

    Two patterns:
      (a) Old-style "Example rows (replace as the product grows):"
          header still present — always a finding regardless of MODULES
          state. Just delete it.
      (b) The new-style "_(populated by the architect persona...)_"
          placeholder present AND there's at least one real MODULES row
          populated — architect added rows but skipped the placeholder.
    """
    wd = Path(working_dir)
    arch = wd / "ARCHITECTURE.md"
    if not arch.is_file():
        return []
    try:
        text = arch.read_text(encoding="utf-8")
    except Exception:
        return []

    findings: list[Finding] = []
    target_feature_id = _pick_target_feature(features)
    if target_feature_id is None:
        return []

    # Pattern (a): old-style "Example rows" header (template literal from
    # pre-PR-25 renderer). Always stale; remove on sight.
    for marker in ("Example rows (replace as the product grows):", "Example rows:"):
        if marker in text:
            findings.append(Finding(
                category="placeholder_template",
                severity="medium",
                target_type="doc",
                target_id="ARCHITECTURE.md",
                feature_id=target_feature_id,
                detail=(
                    f"ARCHITECTURE.md contains the renderer placeholder "
                    f"`{marker}` followed by example rows referencing "
                    "modules that don't exist in this product. Confuses "
                    "the pre-coder context builder."
                ),
                fix_hint=(
                    "Architect persona: delete the `Example rows:` block "
                    "and the bulleted module-reference lines that follow."
                ),
            ))
            break  # one finding per file is enough

    # Pattern (b): new-style placeholder row left in place after real
    # rows landed. Heuristic: the placeholder string appears AND there's
    # at least one MODULES table row that doesn't start with the
    # placeholder marker.
    new_placeholder = "_(populated by the architect persona as features land"
    if new_placeholder in text:
        # Count populated MODULES rows (any row in the MODULES table
        # that doesn't contain the placeholder marker and isn't a
        # header / separator row).
        modules_section = _extract_section(text, "## MODULES")
        if modules_section:
            populated_rows = [
                line for line in modules_section.splitlines()
                if line.startswith("|")
                and "---" not in line
                and "Concern" not in line
                and new_placeholder not in line
                and line.count("|") >= 4   # real row, not header
            ]
            if populated_rows:
                findings.append(Finding(
                    category="placeholder_template",
                    severity="low",
                    target_type="doc",
                    target_id="ARCHITECTURE.md",
                    feature_id=target_feature_id,
                    detail=(
                        f"ARCHITECTURE.md MODULES table has "
                        f"{len(populated_rows)} populated row(s) AND the "
                        "renderer placeholder row. The placeholder should "
                        "be removed once real entries land."
                    ),
                    fix_hint=(
                        "Architect persona: delete the row containing "
                        f"`{new_placeholder}...)_` — the table has real "
                        "entries above it now."
                    ),
                ))
    return findings


def _extract_section(text: str, heading: str) -> str:
    """Return everything between `heading` and the next `## ` heading,
    or empty string if heading not found."""
    idx = text.find(heading)
    if idx < 0:
        return ""
    body = text[idx + len(heading):]
    # Stop at next `## ` heading
    m = re.search(r"\n##\s", body)
    if m:
        body = body[:m.start()]
    return body


# ────────────────────────────────────────────────────────────────────────────
# Detector 4: duplicate DDL (schema declared in >1 place)  [chore-eligible]
# ────────────────────────────────────────────────────────────────────────────

# `CREATE TABLE [IF NOT EXISTS] [`"']<name>` — capture the table identifier.
_DDL_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
# Dir names where multiple CREATE TABLE statements are legitimate (one per
# migration) or irrelevant (build/scratch/tests). Pruned during the walk.
_DDL_EXCLUDE_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    "Temp", "Results", "dist", "build", ".pytest_cache", ".mypy_cache",
    "tests", "test", "migrations",
    # Framework build-output dirs — minified/vendored artifacts, never
    # hand-written source. Scanning them is noise (2026-06-13 soak:
    # timing_unsafe_compare false-fired on IndianFoodTruck
    # `.next/static/chunks/polyfills.js`). Pruning the build root drops
    # everything under it. Shared by every source-scanning detector below.
    # (Bare `static`/`vendor` are intentionally NOT excluded — they can hold
    # hand-written code; only the build-tool output roots are.)
    ".next", ".nuxt", ".svelte-kit", "out", "coverage", ".turbo",
    ".cache", ".parcel-cache",
})
_DDL_EXCLUDE_PATH_SUBSTR = ("alembic/versions", "db/migrations")


def detect_duplicate_ddl(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Schema (a given `CREATE TABLE`) declared in more than one location.

    The canonical agent-drift smell: every new persistence/query function
    defensively re-runs `CREATE TABLE IF NOT EXISTS <t>` instead of relying
    on a single init function. calc3 2026-05-28 reached 11 copies of
    `CREATE TABLE calculations` across history.py (5) + stats.py (3) and
    growing with each feature. A schema change becomes an N-site edit.

    Python-first (scans `*.py`); migrations/test dirs excluded since
    repeated DDL is legitimate there. Counts total occurrences (so the
    within-file repetition in calc3 is caught, not just cross-file).
    Emits `severity="high"` → routes to the corrective-chore sink.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    sites: dict[str, list[str]] = {}  # table_name → ["relpath:lineno", ...]
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _DDL_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, wd).replace("\\", "/")
        if any(sub in rel_root for sub in _DDL_EXCLUDE_PATH_SUBSTR):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = os.path.relpath(fpath, wd).replace("\\", "/")
            for i, line in enumerate(text.splitlines(), start=1):
                hash_idx = line.find("#")
                for m in _DDL_RE.finditer(line):
                    # Skip matches inside a Python comment.
                    if hash_idx != -1 and hash_idx < m.start():
                        continue
                    sites.setdefault(m.group(1).lower(), []).append(f"{rel}:{i}")

    pid = _product_id_from_features(features)
    anchor = _pick_target_feature(features) or 0
    findings: list[Finding] = []
    for table, locs in sorted(sites.items()):
        if len(locs) <= 1:
            continue
        findings.append(Finding(
            category="duplicate_ddl",
            severity="high",
            target_type="code",
            target_id=table,
            feature_id=anchor,
            detail=(
                f"`CREATE TABLE {table}` is declared in {len(locs)} places. "
                "Schema must be declared exactly once (in the persistence / "
                "init module); every other site should assume the table "
                "already exists. Duplicated DDL means a schema change is an "
                f"{len(locs)}-site edit and the copies will drift apart."
            ),
            fix_hint=(
                "Keep the CREATE TABLE in a single init function (e.g. "
                "init_db()) called once at startup; delete the inline "
                "re-declarations from the other functions and have them "
                "rely on the table already existing."
            ),
            occurrences=locs,
            product_id=pid,
        ))
    return findings


def _product_id_from_features(features: list[dict]) -> int | None:
    """First product_id found in the features list, else None."""
    for f in features or []:
        pid = f.get("product_id")
        if isinstance(pid, int):
            return pid
    return None


# ────────────────────────────────────────────────────────────────────────────
# Detector 5: god-file (one file holds too many HTTP routes)
# ────────────────────────────────────────────────────────────────────────────

# A `@<obj>.<method>(...)` route decorator. Matches Flask `@app.route` and
# `@app.get/post/...`; FastAPI `@router.get/post/...`; covers all the common
# Python web stacks with a single regex (cheaper than AST for a counting check).
_ROUTE_DECORATOR_RE = re.compile(
    r"@\w+\.(route|get|post|put|patch|delete)\s*\(",
    re.IGNORECASE,
)
# Threshold above which a single file is a structural clobber-risk. Picked from
# calc3 2026-05-29: main.py held 15 routes and coders editing one for an
# unrelated feature routinely dropped the other 14 (#1025/#1031). Any product
# crossing this gets bounced into a Blueprint/router split.
_GOD_FILE_ROUTE_THRESHOLD = 8


def detect_god_file(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Source files holding more than _GOD_FILE_ROUTE_THRESHOLD route handlers.

    Canonical clobber pattern: a single multi-route file (e.g. calc3 src/main.py
    with 15 routes) makes every edit a coordination problem — the coder editing
    one route has to mentally avoid the other 14, and LLM coders routinely drop
    unrelated handlers when rewriting the file. Splitting into Flask Blueprints
    (one file per concern) makes the clobber STRUCTURALLY impossible because
    the file the coder edits no longer contains the routes it would otherwise
    clobber.

    Python-first (matches `*.py`); migrations/test dirs excluded. Emits one
    high-severity finding per offending file with route count and LOC.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    findings: list[Finding] = []
    pid = _product_id_from_features(features)
    anchor = _pick_target_feature(features) or 0
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _DDL_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, wd).replace("\\", "/")
        if any(sub in rel_root for sub in _DDL_EXCLUDE_PATH_SUBSTR):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            route_count = len(_ROUTE_DECORATOR_RE.findall(text))
            if route_count <= _GOD_FILE_ROUTE_THRESHOLD:
                continue
            loc = text.count("\n") + 1
            rel = os.path.relpath(fpath, wd).replace("\\", "/")
            findings.append(Finding(
                category="god_file",
                severity="high",
                target_type="file",
                target_id=rel,
                feature_id=anchor,
                detail=(
                    f"`{rel}` holds {route_count} route handlers in {loc} lines. "
                    f"Threshold is {_GOD_FILE_ROUTE_THRESHOLD}. A single multi-route "
                    "file is the structural root of the cross-file clobber pattern "
                    "(coders editing one route drop unrelated handlers). Split into "
                    "Flask Blueprints / FastAPI routers so each file holds a focused "
                    "subset and the clobber becomes structurally impossible."
                ),
                fix_hint=(
                    "Refactor into Blueprints: one file per concern "
                    "(e.g. `src/api/auth.py`, `src/api/history.py`, `src/api/stats.py`); "
                    "keep `src/main.py` as the app factory that imports and registers "
                    "each Blueprint. Move each route's handler verbatim — do not change "
                    "the URL paths or response shapes."
                ),
                occurrences=[f"{rel}: {route_count} routes, {loc} lines"],
                product_id=pid,
            ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 6: file-level PUBLIC_ROUTE blanket coexisting with verify_auth
# ────────────────────────────────────────────────────────────────────────────


def detect_public_route_blanket_with_auth(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Files whose first non-empty line opts the whole file out of Guard 6 via
    `# PUBLIC_ROUTE:` while also containing `verify_auth(` somewhere — a
    self-contradicting state that silently turns Guard 6 OFF for any future
    state-changing route added to the file.

    Canonical 2026-05-28 calc3 src/main.py: header says "arithmetic endpoints
    have no user state" but the file went on to register `POST /api/auth/login`,
    `POST /api/auth/register`, and authed `DELETE` endpoints. The DELETEs got
    auth from the coder's good behaviour, not from enforcement — the file-level
    blanket suppressed Guard 6 for the entire file. The fix is to drop the
    blanket and annotate per-route the ones that truly are public.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    findings: list[Finding] = []
    pid = _product_id_from_features(features)
    anchor = _pick_target_feature(features) or 0
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _DDL_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, wd).replace("\\", "/")
        if any(sub in rel_root for sub in _DDL_EXCLUDE_PATH_SUBSTR):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # First non-empty line carries the file-level marker (matches the
            # same convention the post-coder lint guard uses).
            first_nonempty = ""
            for line in text.splitlines():
                if line.strip():
                    first_nonempty = line
                    break
            if "PUBLIC_ROUTE" not in first_nonempty:
                continue
            if "verify_auth(" not in text:
                continue
            rel = os.path.relpath(fpath, wd).replace("\\", "/")
            findings.append(Finding(
                category="public_route_blanket_with_auth",
                severity="high",
                target_type="file",
                target_id=rel,
                feature_id=anchor,
                detail=(
                    f"`{rel}` has a file-level `# PUBLIC_ROUTE:` annotation AND "
                    "calls `verify_auth(`. The blanket opts EVERY route in this "
                    "file out of Guard 6 (the state-changing-routes-need-auth check), "
                    "yet some routes are explicitly authed — a self-contradiction. "
                    "Any future POST/PUT/PATCH/DELETE added to this file silently "
                    "bypasses Guard 6 and could ship unauthed."
                ),
                fix_hint=(
                    "Delete the file-level `# PUBLIC_ROUTE:` line. For the routes "
                    "that genuinely don't need auth (login, register, calculate, "
                    "etc.), add a per-route comment `# PUBLIC_ROUTE: <reason>` on "
                    "the line above the route decorator. Guard 6 honours per-route "
                    "annotations too, but only one route at a time — not a whole file."
                ),
                occurrences=[f"{rel}:1 — first-line annotation: `{first_nonempty.strip()}`"],
                product_id=pid,
            ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 7: mixed error-response envelopes in one file
# ────────────────────────────────────────────────────────────────────────────

# Structured: `{"error": {"code": "...", "message": "..."}}` — the canonical
# shape per ARCHITECTURE.md REFERENCE PATTERNS.
_ERR_STRUCTURED_RE = re.compile(
    r"['\"]?error['\"]?\s*:\s*\{\s*['\"]?code['\"]?",
)
# Raw: `{"error": str(e)}` — the form REFERENCE PATTERNS explicitly says "NEVER".
_ERR_RAW_RE = re.compile(
    r"['\"]?error['\"]?\s*:\s*str\s*\(",
)


def detect_mixed_error_envelopes(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Files that emit BOTH `{"error": {"code", "message"}}` (structured) and
    `{"error": str(e)}` (raw) error responses.

    Canonical 2026-05-28 calc3 src/main.py: 24 handlers used the structured
    shape, 5 (mostly auth Unauthorized paths) leaked raw `str(e)`. Mixed shapes
    mean clients can't reliably parse error responses, and the raw form risks
    leaking internal details. ARCHITECTURE.md REFERENCE PATTERNS specifies the
    structured shape as canonical with an explicit "NEVER" on the raw form.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    findings: list[Finding] = []
    pid = _product_id_from_features(features)
    anchor = _pick_target_feature(features) or 0
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _DDL_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, wd).replace("\\", "/")
        if any(sub in rel_root for sub in _DDL_EXCLUDE_PATH_SUBSTR):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            structured_hits = len(_ERR_STRUCTURED_RE.findall(text))
            raw_hits = len(_ERR_RAW_RE.findall(text))
            if structured_hits == 0 or raw_hits == 0:
                continue   # consistent (one shape or no errors)
            rel = os.path.relpath(fpath, wd).replace("\\", "/")
            findings.append(Finding(
                category="mixed_error_envelopes",
                severity="high",
                target_type="file",
                target_id=rel,
                feature_id=anchor,
                detail=(
                    f"`{rel}` mixes error-response shapes: "
                    f"{structured_hits} structured `{{\"error\": {{\"code\", \"message\"}}}}` "
                    f"occurrence(s) and {raw_hits} raw `{{\"error\": str(...)}}` "
                    "occurrence(s). ARCHITECTURE.md REFERENCE PATTERNS specifies "
                    "the structured form as canonical with an explicit `NEVER` on "
                    "the raw form. Mixed shapes make clients unable to reliably "
                    "parse error responses."
                ),
                fix_hint=(
                    "Convert every raw `{\"error\": str(e)}` to the canonical "
                    "`{\"error\": {\"code\": \"<CODE>\", \"message\": \"<msg>\"}}` "
                    "form. Pick a stable code per error class (e.g. `UNAUTHORIZED`, "
                    "`INVALID_INPUT`, `INTERNAL_ERROR`) and a short message. Never "
                    "leak raw exception text in the response — log it server-side."
                ),
                occurrences=[
                    f"{rel}: structured={structured_hits}, raw={raw_hits}",
                ],
                product_id=pid,
            ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 8: sandbox path literals fossilized into product code
# ────────────────────────────────────────────────────────────────────────────

# Extensions scanned by the code-literal detectors (8 and 10). Broader than
# the *.py-only detectors above because the canonical incidents span stacks.
_CODE_SCAN_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".go")
# Exclude set for the code-literal detectors. Deliberately DOES include
# scanning of tests/ (unlike _DDL_EXCLUDE_DIRS) — the canonical sandbox-
# literal incidents were all IN test files.
_CODE_SCAN_EXCLUDE_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    "Temp", "Results", "dist", "build", ".pytest_cache", ".mypy_cache",
    ".next", ".nuxt", "coverage", ".nyc_output", "docs",
})

_SANDBOX_LITERALS = ("/workspace", "/home/agent")


def _walk_code_files(wd: Path):
    """Yield (Path, relpath_str) for every code file under wd, honoring
    _CODE_SCAN_EXCLUDE_DIRS. Shared by detectors 8 and 10."""
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _CODE_SCAN_EXCLUDE_DIRS]
        for fn in files:
            if not fn.endswith(_CODE_SCAN_EXTS):
                continue
            fpath = Path(root) / fn
            yield fpath, os.path.relpath(fpath, wd).replace("\\", "/")


def detect_sandbox_path_literals(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Agent-container paths (`/workspace`, `/home/agent`) hardcoded into
    product source/test files. The committed code then only works inside
    the agent sandbox: the suite is green in the factory and red on every
    other machine (CI, fresh clone, dev laptop).

    Canonical incidents (2026-06-09 five-product audit):
      - MyJira tests/test_db.py greps `/workspace/src` via subprocess
      - Mytracking tests/test_ddl_dedup.py checks `/workspace/simple_db_test.py`
      - testingcalc tests/test_alembic.py uses cwd="/workspace" and injects
        `/home/agent/.local/bin` into PATH

    One finding per offending file (occurrences carry the line numbers) so
    the fix_hint is concrete. Report-only: lives in _DETECTORS (comment
    path), not _CHORE_DETECTORS.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    anchor = _pick_target_feature(features)
    if anchor is None:
        return []
    pid = _product_id_from_features(features)
    findings: list[Finding] = []
    for fpath, rel in _walk_code_files(wd):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        hits: list[str] = []
        for i, line in enumerate(text.splitlines(), start=1):
            if any(lit in line for lit in _SANDBOX_LITERALS):
                hits.append(f"{rel}:{i}")
                if len(hits) >= 10:
                    break
        if not hits:
            continue
        findings.append(Finding(
            category="sandbox_path_literal",
            severity="medium",
            target_type="file",
            target_id=rel,
            feature_id=anchor,
            detail=(
                f"`{rel}` hardcodes an agent-container path "
                "(`/workspace` or `/home/agent`). This code only works "
                "inside the factory's sandbox — it fails on CI, fresh "
                "clones, and developer machines. Tests written this way "
                "verify the factory environment, not the product."
            ),
            fix_hint=(
                "Derive paths from the file's own location "
                "(`Path(__file__).parent`) or the current working "
                "directory — never an absolute container path. For PATH "
                "injections, rely on the environment, not a hardcoded "
                "`/home/agent/...` entry."
            ),
            occurrences=hits,
            product_id=pid,
        ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 9: build artifacts / large binaries tracked in git
# ────────────────────────────────────────────────────────────────────────────

# Path SEGMENTS that mark a tracked file as build output / cache.
_ARTIFACT_DIR_SEGMENTS = frozenset({
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "coverage", ".nyc_output",
})
_ARTIFACT_SUFFIXES = (".pack.gz", ".tsbuildinfo", ".pyc")
_ARTIFACT_SIZE_LIMIT = 1_000_000  # bytes; tracked binaries above this are flagged


def _git_tracked_files(wd: Path) -> list[str] | None:
    """`git ls-files` for the working tree. None when the lookup fails."""
    try:
        result = subprocess.run(
            ["git", "-C", str(wd), "ls-files"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return [ln for ln in result.stdout.splitlines() if ln.strip()]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        return None


def detect_tracked_build_artifacts(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Build output, dependency caches, or large binaries tracked in git.

    Canonical incident: MyCalc1 2026-06 — commit 73b3226 swept the whole
    `.next/` dev-build directory into the session branch (40 files incl. a
    9.16MB webpack `pack.gz`), reviewer approved, auto-merge landed it, and
    every rework re-committed a *grown* cache until `.git` hit 93MB on a
    558-line product. Guard 13 covers debris FILENAMES (`*.bak`, `_old_`)
    but has no concept of build directories or binary size.

    Emits one finding per matched category-style reason, with occurrences
    capped at 20 paths. severity="high" but kept on the comment path
    (_DETECTORS) for the report-only soak; promotion to _CHORE_DETECTORS
    (and a post-coder bounce guard) comes after false-positive review.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    anchor = _pick_target_feature(features)
    if anchor is None:
        return []
    tracked = _git_tracked_files(wd)
    if not tracked:
        return []
    pid = _product_id_from_features(features)

    offenders: list[str] = []
    for rel in tracked:
        segments = rel.split("/")
        reason = ""
        if any(seg in _ARTIFACT_DIR_SEGMENTS for seg in segments[:-1]):
            reason = "build/cache directory"
        elif rel.endswith(_ARTIFACT_SUFFIXES):
            reason = "build artifact suffix"
        else:
            try:
                size = (wd / rel).stat().st_size
            except OSError:
                continue
            if size > _ARTIFACT_SIZE_LIMIT:
                reason = f"large file ({size / 1_000_000:.1f}MB)"
        if reason:
            offenders.append(f"{rel} — {reason}")

    if not offenders:
        return []
    shown = offenders[:20]
    more = len(offenders) - len(shown)
    if more > 0:
        shown.append(f"... and {more} more")
    return [Finding(
        category="tracked_build_artifacts",
        severity="high",
        target_type="file",
        target_id=shown[0].split(" — ")[0],
        feature_id=anchor,
        detail=(
            f"{len(offenders)} tracked file(s) are build output, dependency "
            "caches, or >1MB binaries. These bloat the repo permanently "
            "(every rework commit re-adds a grown cache), leak local config "
            "(telemetry ids, container paths), and bury real diffs."
        ),
        fix_hint=(
            "`git rm -r --cached` the offending paths, add the directories/"
            "patterns to .gitignore (factory-side — the file is RO-mounted), "
            "and commit. For already-bloated history an operator must run "
            "`git filter-repo`."
        ),
        occurrences=shown,
        product_id=pid,
    )]


# ────────────────────────────────────────────────────────────────────────────
# Detector 10: stub-confession comments in production code
# ────────────────────────────────────────────────────────────────────────────

# High-precision phrases agents write when shipping a placeholder while
# claiming the feature complete. Deliberately narrow — each phrase comes
# from a real shipped incident, and the comment path tolerates the
# occasional benign hit.
_STUB_CONFESSION_PHRASES = (
    "in a real implementation",   # DocumentSign tasks.py — email stub that only logs
    "in a real app",
    "in a real system",
    "in production, use",         # Mytracking db.py — static-salt SHA256 confession
    "in a production system",
    "this is a placeholder",
    "for now, just log",
)
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "testcases"})


def detect_stub_confessions(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Comments in NON-TEST code confessing the implementation is a stub
    ("in a real implementation, this would send actual emails...") while
    the feature shipped as complete.

    Canonical incidents (2026-06-09 five-product audit):
      - DocumentSign src/tasks.py: completion-email task logs instead of
        sending — while a working SMTP module sat unused in src/lib/
      - MyJira src/email_worker.py: `enqueue_email` that synchronously
        sends, docstring promising a queue "in a real implementation"
      - Mytracking src/db.py: static salt + "In production, use a proper
        salt per user."

    Test files are excluded (fakes/stubs are legitimate there). One
    finding per file, occurrences carry line numbers. Report-only.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    anchor = _pick_target_feature(features)
    if anchor is None:
        return []
    pid = _product_id_from_features(features)
    findings: list[Finding] = []
    for fpath, rel in _walk_code_files(wd):
        parts = rel.lower().split("/")
        if any(p in _TEST_DIR_NAMES for p in parts[:-1]):
            continue
        base = parts[-1]
        if base.startswith("test_") or base.endswith(("_test.py", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js")):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        hits: list[str] = []
        for i, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            if any(p in low for p in _STUB_CONFESSION_PHRASES):
                hits.append(f"{rel}:{i} — {line.strip()[:100]}")
                if len(hits) >= 5:
                    break
        if not hits:
            continue
        findings.append(Finding(
            category="stub_confession",
            severity="medium",
            target_type="file",
            target_id=rel,
            feature_id=anchor,
            detail=(
                f"`{rel}` contains a comment confessing the code is a "
                "stub/placeholder ('in a real implementation...'). The "
                "feature shipped as complete; the deferred half typically "
                "never lands and the placeholder silently becomes "
                "production behavior."
            ),
            fix_hint=(
                "Either implement the real behavior now, or name the "
                "function honestly (e.g. `send_email_now`, not "
                "`enqueue_email`) and file a follow-up feature for the "
                "deferred half — then delete the confession comment."
            ),
            occurrences=hits,
            product_id=pid,
        ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 11: undeclared backend dependencies (Guard 18's blind spot)
# ────────────────────────────────────────────────────────────────────────────

# Import → (required distribution, why). Guard 18 walks DIRECT imports, so a
# package whose import resolves to a declared dep but which needs an
# UNDECLARED backend at runtime sails through — the agent image pre-bakes
# the backend, the container gate passes, and a fresh
# `pip install -r requirements.txt && pytest` fails. Canonical: MyJira
# 2026-06-09 (`from passlib.hash import bcrypt` with no bcrypt dep;
# `fastapi.testclient` with no httpx). Keep entries high-precision.
_BACKEND_DEP_PAIRS: dict[str, tuple[str, str]] = {
    "passlib": ("bcrypt", "passlib's bcrypt handler raises MissingBackendError without the bcrypt package"),
    "fastapi.testclient": ("httpx", "fastapi.testclient.TestClient is httpx-based; httpx is not a fastapi dependency"),
    "starlette.testclient": ("httpx", "starlette.testclient.TestClient is httpx-based; httpx is not a starlette dependency"),
}

_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))",
    re.MULTILINE,
)


def _declared_requirements(wd: Path) -> set[str] | None:
    """Distribution names declared in requirements*.txt (lowercased,
    version specs stripped). None when no requirements file exists —
    callers should skip (non-Python product or different dep system)."""
    found_any = False
    declared: set[str] = set()
    for name in ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt"):
        req = wd / name
        if not req.is_file():
            continue
        found_any = True
        try:
            for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                pkg = re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].strip().lower()
                if pkg:
                    declared.add(pkg)
        except Exception:
            continue
    return declared if found_any else None


def detect_undeclared_backend_deps(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Imports whose runtime BACKEND package is missing from requirements —
    the one-level-deeper sibling of Guard 18 (deps coherence). The product
    works inside the pre-baked agent image and fails on every fresh
    install. One finding per missing backend. Report-only.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    anchor = _pick_target_feature(features)
    if anchor is None:
        return []
    declared = _declared_requirements(wd)
    if declared is None:
        return []
    pid = _product_id_from_features(features)

    # module-or-prefix → first occurrence site
    triggered: dict[str, str] = {}
    for fpath, rel in _walk_code_files(wd):
        if not rel.endswith(".py"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _IMPORT_RE.finditer(text):
            mod = (m.group(1) or m.group(2) or "").lower()
            for trigger in _BACKEND_DEP_PAIRS:
                if mod == trigger or mod.startswith(trigger + "."):
                    triggered.setdefault(trigger, rel)

    findings: list[Finding] = []
    for trigger, site in sorted(triggered.items()):
        backend, why = _BACKEND_DEP_PAIRS[trigger]
        if backend.lower() in declared:
            continue
        findings.append(Finding(
            category="undeclared_backend_dep",
            severity="high",
            target_type="file",
            target_id="requirements.txt",
            feature_id=anchor,
            detail=(
                f"`{site}` imports `{trigger}` but `{backend}` is not in "
                f"requirements*.txt — {why}. The agent image pre-bakes "
                f"`{backend}` so the container gate passes, but a fresh "
                "`pip install -r requirements.txt` install fails."
            ),
            fix_hint=f"Add `{backend}` to requirements.txt.",
            occurrences=[f"{site} imports {trigger}; missing backend: {backend}"],
            product_id=pid,
            dedupe_key=f"undeclared_backend_dep:{backend}",
        ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector 12: emitted URLs that no registered route serves
# ────────────────────────────────────────────────────────────────────────────

# Route registrations: Flask `@app.route("/x")`, FastAPI/Flask
# `@router.get("/x")`, plus `add_url_rule("/x"` / `add_api_route("/x"`.
_ROUTE_PATH_RE = re.compile(
    r"@\w+\.(?:route|get|post|put|patch|delete|head|options)\s*\(\s*[fr]?['\"]([^'\"]+)['\"]"
    r"|\.(?:add_url_rule|add_api_route)\s*\(\s*[fr]?['\"]([^'\"]+)['\"]",
)
# Emitted-URL candidates: a quoted app-relative path on a line that names a
# URL-ish variable/key. Narrow on purpose — generic "/"-strings (file paths,
# regexes) must not match.
_URL_CONTEXT_RE = re.compile(r"url|href|link|redirect|location", re.IGNORECASE)
_EMITTED_PATH_RE = re.compile(r"[fr]?['\"](/[A-Za-z0-9_\-{][^'\"\s]*)['\"]")
_STATIC_SUFFIXES = (".css", ".js", ".png", ".jpg", ".svg", ".ico", ".map", ".woff", ".woff2", ".html")


def _norm_segments(path: str) -> list[str]:
    """Split a path into segments with `{param}`/`<param>` → "*"."""
    segs = [s for s in path.split("?", 1)[0].split("/") if s]
    out = []
    for s in segs:
        if "{" in s or "<" in s:
            out.append("*")
        else:
            out.append(s)
    return out


def _segs_match(a: list[str], b: list[str]) -> bool:
    """Same-length segment-wise match; "*" matches any single segment."""
    if len(a) != len(b):
        return False
    return all(x == y or x == "*" or y == "*" for x, y in zip(a, b))


def _route_serves(
    candidate: list[str], route: list[str], prefixes: list[list[str]],
) -> bool:
    """Whether a registered `route` plausibly serves the emitted
    `candidate` path. Three accepted shapes:

      (a) full-length wildcard match (route registered with its full path);
      (b) an explicitly collected mount prefix + route == candidate
          (`APIRouter(prefix=...)` / `include_router(..., prefix=...)`);
      (c) route is a TAIL of candidate with at least one LITERAL segment
          equality — the conservative fallback for prefixes we failed to
          collect. The literal-equality requirement is load-bearing: a
          bare tail-match lets any candidate ending in a {param} segment
          be "served" by every 1-segment route in the app (`/usage`
          "matching" `/sign/{token}` — the DocumentSign miss during
          detector development).
    """
    if not route:
        return False
    if _segs_match(candidate, route):
        return True
    for p in prefixes:
        if _segs_match(candidate, p + route):
            return True
    if len(route) < len(candidate):
        tail = candidate[len(candidate) - len(route):]
        literal_hit = False
        for c, r in zip(tail, route):
            if c == r and c != "*":
                literal_hit = True
            elif c != r and c != "*" and r != "*":
                return False
        return literal_hit
    return False


_MOUNT_PREFIX_RE = re.compile(r"prefix\s*=\s*[fr]?['\"](/[^'\"]+)['\"]")


def detect_unreachable_emitted_urls(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """App-relative URLs the product EMITS (signing links, redirects) that
    no registered route serves. The deterministic slice of the "no session
    owns the end-to-end journey" failure: one story generates
    `signing_url=f"/sign/{token}"`, no story ever builds `/sign/...`, every
    per-story gate passes, and the product's core flow 404s.

    Canonical incidents (2026-06-09 five-product audit):
      - DocumentSign: sending.py / zapier.py return `/sign/{token}`; no
        /sign route exists anywhere — recipients cannot sign.
      - testingcalc: OAuth callback redirects to `{FRONTEND_URL}/callback`
        with no corresponding route.

    Python-first. Candidates are quoted "/..." literals on lines that name
    a URL-ish identifier (url/href/link/redirect/location). Matching is
    segment-wise with `{param}`→"*" and TAIL-match so router prefixes
    don't false-positive. Skips entirely when the product registers no
    routes (not a web app / unparsed framework). Report-only.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    anchor = _pick_target_feature(features)
    if anchor is None:
        return []
    pid = _product_id_from_features(features)

    routes: list[list[str]] = []
    prefixes: list[list[str]] = []
    emitted: list[tuple[str, str]] = []  # (path, "rel:line")
    for fpath, rel in _walk_code_files(wd):
        if not rel.endswith(".py"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _ROUTE_PATH_RE.finditer(text):
            path = m.group(1) or m.group(2)
            if path and path.startswith("/"):
                routes.append(_norm_segments(path))
        for m in _MOUNT_PREFIX_RE.finditer(text):
            prefixes.append(_norm_segments(m.group(1)))
        for i, line in enumerate(text.splitlines(), start=1):
            if _ROUTE_PATH_RE.search(line):
                continue  # registration line, not an emission
            if not _URL_CONTEXT_RE.search(line):
                continue
            for pm in _EMITTED_PATH_RE.finditer(line):
                path = pm.group(1)
                if path.rstrip("/") == "" or path.startswith("//"):
                    continue
                if path.lower().endswith(_STATIC_SUFFIXES):
                    continue
                if any(seg in ("static", "docs", "redoc", "openapi.json")
                       for seg in path.split("/")[1:2]):
                    continue
                emitted.append((path, f"{rel}:{i}"))

    if not routes or not emitted:
        return []  # no parsed web framework, or nothing emitted — skip

    findings: list[Finding] = []
    seen: set[tuple[str, ...]] = set()
    for path, site in emitted:
        cand = _norm_segments(path)
        if not cand:
            continue
        if any(_route_serves(cand, r, prefixes) for r in routes):
            continue
        # Dedupe on NORMALIZED segments — `f"/sign/{token}"` and
        # `f"/sign/{token_row['token']}"` are the same unreachable route.
        key = tuple(cand)
        if key in seen:
            continue
        seen.add(key)
        norm_path = "/" + "/".join(cand)
        sites = [s for p, s in emitted if tuple(_norm_segments(p)) == key][:5]
        findings.append(Finding(
            category="unreachable_emitted_url",
            severity="high",
            target_type="code",
            target_id=norm_path,
            feature_id=anchor,
            detail=(
                f"The product emits the URL `{norm_path}` (assigned to a "
                "url/link/redirect value; `*` = parameter) but no "
                "registered route serves it. Users following this link "
                "get a 404 — the classic seam between two stories that "
                "each passed their own gates."
            ),
            fix_hint=(
                f"Either register a route that serves `{norm_path}` or fix "
                "the emitted URL to point at an existing route. If this is "
                "a frontend-only path served by another app, ignore — and "
                "tell the architect to note it in ARCHITECTURE.md."
            ),
            occurrences=sites,
            product_id=pid,
            dedupe_key=f"unreachable_emitted_url:{norm_path}",
        ))
    return findings[:5]  # cap per cycle


# ────────────────────────────────────────────────────────────────────────────
# Detector 13: secret-sentinel — placeholder values standing in for secrets
# ────────────────────────────────────────────────────────────────────────────

# Guard 5 catches the DIRECT form (`jwt.encode(..., os.environ.get("S", "x"))`).
# This detector catches the indirection the guard taught agents to write
# instead: a helper that returns a sentinel string when the env var is unset,
# which then flows into signing/hashing — textual compliance, semantic
# violation. Canonical: Mytracking branch coder/9693adc8, `_get_env_var()`
# returning the literal `"<MISSING_ENV_VAR_JWT_SECRET>"` used as the JWT
# signing key (a predictable key = anyone can forge tokens).
_SECRET_SENTINEL_RES = (
    # The observed evasion shape: an angle-bracket MISSING/UNSET marker.
    re.compile(r"['\"]<\s*(?:MISSING|UNSET|NO)_?ENV", re.I),
    # Sentinel-named secret literals: "placeholder-secret", "changeme_key",
    # "dummy-token", "default_password" ...
    re.compile(r"['\"](?:placeholder|change[-_]?me|dummy|default|insecure|sample)[-_]?(?:secret|key|token|password)[^'\"]*['\"]", re.I),
    # Fake credential material with the sentinel word EMBEDDED, e.g. the
    # testingcalc seed rows: "$2b$12$placeholderhashplaceholderhash..." under
    # a comment claiming they're valid bcrypt hashes (they are not — the
    # first real login attempt raises ValueError → 500).
    re.compile(r"['\"][^'\"]*placeholder[^'\"]*(?:hash|secret|key|token|password)[^'\"]*['\"]", re.I),
    # env-get with a non-empty literal default on a secret-ish var name —
    # anywhere, not just crypto call sites (Guard 5's scope).
    re.compile(r"environ\.get\(\s*['\"][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)[A-Z0-9_]*['\"]\s*,\s*['\"][^'\"]+['\"]", re.I),
)


def detect_secret_sentinel(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Sentinel/placeholder strings standing in for secrets in NON-TEST code.
    A predictable stand-in flowing into signing/hashing/auth is a hardcoded
    secret in disguise — and it ships exactly when the env var is missing,
    i.e. in the least-configured (most exposed) deployments. Test files are
    excluded (fixed test secrets are legitimate there). One finding per
    file. Report-only.
    """
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    anchor = _pick_target_feature(features)
    if anchor is None:
        return []
    pid = _product_id_from_features(features)
    findings: list[Finding] = []
    for fpath, rel in _walk_code_files(wd):
        parts = rel.lower().split("/")
        if any(p in _TEST_DIR_NAMES for p in parts[:-1]):
            continue
        base = parts[-1]
        if base.startswith("test_") or base.endswith(
            ("_test.py", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js")
        ):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        hits: list[str] = []
        for i, line in enumerate(text.splitlines(), start=1):
            if any(p.search(line) for p in _SECRET_SENTINEL_RES):
                hits.append(f"{rel}:{i} — {line.strip()[:100]}")
                if len(hits) >= 5:
                    break
        if not hits:
            continue
        findings.append(Finding(
            category="secret_sentinel",
            severity="high",
            target_type="file",
            target_id=rel,
            feature_id=anchor,
            detail=(
                f"`{rel}` uses a placeholder/sentinel value where a secret "
                "belongs (or defaults a secret-named env var to a literal). "
                "A predictable stand-in that flows into signing/hashing is "
                "a hardcoded secret in disguise — it activates precisely "
                "when the env var is missing."
            ),
            fix_hint=(
                "Fail closed instead: if the secret env var is unset, raise "
                "at startup or return 503 from the route — never substitute "
                "a constant. (This is the INTENT behind the existing "
                "hardcoded-fallback rule; routing the constant through a "
                "helper does not satisfy it.)"
            ),
            occurrences=hits,
            product_id=pid,
            dedupe_key=f"secret_sentinel:{rel}",
        ))
    return findings


# Retired 2026-05-30: `detect_architect_review_pending` filed chores from
# architect review docs but the actuator (coder) couldn't write to
# ARCHITECTURE.md (RO-mounted via `_PM_CURATED_RO_FILES` for non-architect
# personas). Coders fell back to creating `ARCHITECTURE.md.tmp`, reviewers
# rejected every attempt, supervisor.divergent_review_feedback auto-Blocked
# the chores. Replacement: the architect persona itself now applies its own
# findings on its next cadence — see orchestrator/prompts/architect.md
# "Apply your own findings" step. The architect already has RW on
# ARCHITECTURE.md, so the actuator and the writer are now the same persona.


# ────────────────────────────────────────────────────────────────────────────
# Detector: insecure CORS — wildcard origin together with credentials
# ────────────────────────────────────────────────────────────────────────────
# `allow_origins=["*"]` (or `origin: '*'`) combined with
# `allow_credentials=True` nullifies CORS for authenticated requests: any
# website can make credentialed calls and read the response. The per-commit
# lint Guard 21 catches NEW occurrences (added lines), but legacy misconfigs
# predate it — this whole-file scan catches the existing debt (canonical:
# DogTinder main.py, flagged in two human reviews, never fixed because no
# loop turned the finding into a chore). High-severity → corrective-chore
# sink. Near-zero FP: requires BOTH markers in the same file.
_CORS_WILDCARD_RE = re.compile(
    r"""allow_origins\s*[:=]\s*\[?\s*["']\*["']|origins?\s*:\s*["']\*["']""",
    re.IGNORECASE,
)
_CORS_CREDENTIALS_RE = re.compile(
    r"allow_credentials\s*[:=]\s*True|credentials\s*:\s*true", re.IGNORECASE,
)
_CORS_SOURCE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx")


def detect_insecure_cors(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Source files configuring CORS with a wildcard origin AND credentials."""
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    findings: list[Finding] = []
    pid = _product_id_from_features(features)
    anchor = _pick_target_feature(features) or 0
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _DDL_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, wd).replace("\\", "/")
        if any(sub in rel_root for sub in _DDL_EXCLUDE_PATH_SUBSTR):
            continue
        for fn in files:
            if not fn.endswith(_CORS_SOURCE_EXTS):
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if _CORS_WILDCARD_RE.search(text) and _CORS_CREDENTIALS_RE.search(text):
                rel = os.path.relpath(fpath, wd).replace("\\", "/")
                findings.append(Finding(
                    category="insecure_cors",
                    severity="high",
                    target_type="file",
                    target_id=rel,
                    feature_id=anchor,
                    detail=(
                        f"`{rel}` configures CORS with a wildcard origin "
                        "(`allow_origins=['*']`) AND `allow_credentials=True`. "
                        "This combination disables CORS protection for "
                        "authenticated requests — any website can make "
                        "credentialed calls on behalf of a logged-in user and "
                        "read the response (cross-site token/data theft)."
                    ),
                    fix_hint=(
                        "Pin the allowed origins to an explicit, "
                        "env-configurable list (e.g. read CORS_ORIGINS), or "
                        "drop allow_credentials if a wildcard origin is "
                        "genuinely required. Never combine the two."
                    ),
                    occurrences=[rel],
                    product_id=pid,
                ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector: timing-unsafe comparison of secret material (comment-path soak)
# ────────────────────────────────────────────────────────────────────────────
# `==` / `!=` on password/token/hash/secret/signature values leaks
# information through comparison timing — a constant-time compare
# (hmac.compare_digest / crypto.timingSafeEqual) is required. Canonical:
# DogTinder auth/utils.py `return expected.hex() == dk_hex`. Higher FP
# surface than the CORS/schema detectors (==-matching is broad), so this
# soaks on the COMMENT path first per the Guard-17-tuning protocol; promote
# to the chore sink after a clean two-audit soak.
_SECRET_VAR_RE = re.compile(
    r"\b\w*(password|passwd|secret|token|hmac|signature|digest|"
    r"hashed?|pwhash|api[_-]?key)\w*\b",
    re.IGNORECASE,
)
# A hash/MAC digest output being compared is the classic timing leak even
# when the operands aren't named like secrets — DogTinder's canonical line
# is `return expected.hex() == dk_hex` (operands `expected`/`dk_hex`, no
# secret-ish name; the signal is the .hex() digest call).
_DIGEST_CALL_RE = re.compile(r"\.(hexdigest|digest|hex)\s*\(\s*\)")
_UNSAFE_CMP_RE = re.compile(r"[^=!<>]=\=[^=]|!\=[^=]")  # a == b / a != b, not >= <= == in chains


def detect_timing_unsafe_compare(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Direct == / != comparisons involving secret-looking identifiers."""
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    findings: list[Finding] = []
    anchor = _pick_target_feature(features)
    if anchor is None:
        return []
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _DDL_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, wd).replace("\\", "/")
        if any(sub in rel_root for sub in _DDL_EXCLUDE_PATH_SUBSTR):
            continue
        for fn in files:
            if not fn.endswith((".py", ".js", ".jsx", ".ts", ".tsx")):
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = os.path.relpath(fpath, wd).replace("\\", "/")
            hits: list[str] = []
            for i, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith(("#", "//", "*")):
                    continue
                if "==" not in line and "!=" not in line:
                    continue
                if not _UNSAFE_CMP_RE.search(line):
                    continue
                # Both operands must look like values (skip `x == None`,
                # `len(x) == 0`, numeric/bool comparisons — those aren't
                # secret-equality checks).
                if re.search(r"==\s*(None|null|true|false|\d|len\()", line, re.IGNORECASE):
                    continue
                if not (_SECRET_VAR_RE.search(line) or _DIGEST_CALL_RE.search(line)):
                    continue
                hits.append(f"{rel}:{i}")
            for loc in hits:
                findings.append(Finding(
                    category="timing_unsafe_compare",
                    severity="medium",
                    target_type="code",
                    target_id=loc,
                    feature_id=anchor,
                    detail=(
                        f"`{loc}` compares secret material (password / token / "
                        "hash / signature) with `==` or `!=`. String/bytes "
                        "equality short-circuits on the first differing byte, "
                        "leaking the value through timing."
                    ),
                    fix_hint=(
                        "Use a constant-time compare: Python "
                        "`hmac.compare_digest(a, b)`; JS/TS "
                        "`crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b))`."
                    ),
                ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Detector: schema dual-source — tables created in code but not in migrations
# ────────────────────────────────────────────────────────────────────────────
# A product whose runtime init (e.g. init_db()) CREATE TABLEs a table that no
# alembic/migration file creates has two schema sources of truth: a fresh
# `alembic upgrade head` deploy is missing the table. Canonical: DogTinder
# (init_db creates 7 tables, alembic covers 2) and MyJira (#1329/49/51
# duplicate_ddl siblings). Complements duplicate_ddl (same-table N copies);
# this catches code-vs-migration divergence. High-severity → chore sink.
_OP_CREATE_TABLE_RE = re.compile(
    r"""(?:op\.create_table|create_table)\s*\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""",
    re.IGNORECASE,
)


def detect_schema_dual_source(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Tables CREATEd in application source but absent from migrations."""
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    # No migrations at all → not a dual-source problem (single source: code).
    migration_files: list[Path] = []
    for sub in ("alembic/versions", "db/migrations", "migrations"):
        mdir = wd / sub
        if mdir.is_dir():
            migration_files.extend(mdir.rglob("*.py"))
    if not migration_files:
        return []

    migration_tables: set[str] = set()
    for mf in migration_files:
        try:
            mt = mf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _DDL_RE.finditer(mt):
            migration_tables.add(m.group(1).lower())
        for m in _OP_CREATE_TABLE_RE.finditer(mt):
            migration_tables.add(m.group(1).lower())

    # Tables created in non-migration application source.
    source_tables: dict[str, str] = {}  # table → first "relpath:lineno"
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in _DDL_EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, wd).replace("\\", "/")
        if any(sub in rel_root for sub in _DDL_EXCLUDE_PATH_SUBSTR) \
                or rel_root.split("/")[0] in ("migrations",):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fpath = Path(root) / fn
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = os.path.relpath(fpath, wd).replace("\\", "/")
            for i, line in enumerate(text.splitlines(), start=1):
                hash_idx = line.find("#")
                for m in _DDL_RE.finditer(line):
                    if hash_idx != -1 and hash_idx < m.start():
                        continue
                    source_tables.setdefault(m.group(1).lower(), f"{rel}:{i}")

    missing = sorted(t for t in source_tables if t not in migration_tables)
    if not missing:
        return []
    pid = _product_id_from_features(features)
    anchor = _pick_target_feature(features) or 0
    return [Finding(
        category="schema_dual_source",
        severity="high",
        target_type="code",
        target_id="init_db_vs_migrations",
        feature_id=anchor,
        detail=(
            f"{len(missing)} table(s) are CREATEd in application source but "
            f"absent from the migrations: {', '.join(missing)}. The schema has "
            "two sources of truth — a fresh `alembic upgrade head` deploy is "
            "missing these tables, and the code/migration copies will drift."
        ),
        fix_hint=(
            "Add an alembic migration that creates the missing table(s), then "
            "make the runtime init rely on migrations having run (drop the "
            "inline CREATE TABLE, or keep it only as an idempotent "
            "CREATE TABLE IF NOT EXISTS that mirrors the migration exactly)."
        ),
        occurrences=[source_tables[t] for t in missing],
        product_id=pid,
    )]


# ────────────────────────────────────────────────────────────────────────────
# Detector: overlapping features — siblings racing to co-create the same file
# ────────────────────────────────────────────────────────────────────────────
# Two or more IN-FLIGHT features whose design docs both declare the same
# NEW source file (one not yet on origin/main) are racing to create it:
# they dispatch as separate coder sessions / separate PRs, and whichever
# lands first turns the others into merge conflicts / cap-blocks. This is
# parallel-module drift at the PLANNING layer — the per-coder context
# builder can't see it (the coder for one feature doesn't know the sibling
# exists), and detect_overlapping_prs only catches it at PR time, after the
# compute is spent. Comment-path safety net behind the designer's
# shared-entrypoint cohesion rule (designer.md sizing gate); the primary
# fix is not splitting file-sharing foundations in the first place.
# Canonical 2026-06-13 IndianFoodTruck: ~9 NextAuth features all declared
# `src/pages/api/auth/[...nextauth].ts` and `_app.tsx`; 4 cap-blocked, 5
# rejected, zero shipped.
_DOC_SOURCE_PATH_RE = re.compile(
    r"(?:src|pages|app|lib)/[\w./\[\]\-]+\.(?:ts|tsx|js|jsx|py|go|rb)",
)
_OVERLAP_ACTIVE_STATUSES = frozenset({
    "Designed", "Implementing", "Reviewing", "Reviewed",
})


def detect_overlapping_features(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Active features whose design docs declare the same not-yet-on-main file."""
    wd = Path(working_dir)
    if not wd.is_dir():
        return []
    on_main = _ls_tree_origin_main(wd)  # tracked paths on main, or None on failure

    file_to_fids: dict[str, set[int]] = {}
    for f in features or []:
        if f.get("status") not in _OVERLAP_ACTIVE_STATUSES:
            continue
        fid = f.get("id")
        doc_rel = f.get("design_doc_path")
        if not isinstance(fid, int) or not doc_rel:
            continue
        try:
            doc = (wd / doc_rel).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _DOC_SOURCE_PATH_RE.finditer(doc):
            path = m.group(0)
            # Files co-CREATED collide; files merely imported don't. Skip
            # paths already on main — referencing a shipped module is normal
            # reuse, not a create-race. (ls-tree failure → on_main is None →
            # keep all paths; the 2+-feature requirement still bounds FP.)
            if on_main is not None and path in on_main:
                continue
            file_to_fids.setdefault(path, set()).add(fid)

    pid = _product_id_from_features(features)
    findings: list[Finding] = []
    for path, fids in sorted(file_to_fids.items()):
        if len(fids) < 2:
            continue
        ids = sorted(fids)
        anchor = ids[0]
        findings.append(Finding(
            category="overlapping_features",
            severity="high",
            target_type="code",
            target_id=path,
            feature_id=anchor,
            detail=(
                f"{len(ids)} in-flight features {ids} all declare creating the "
                f"same not-yet-shipped file `{path}`. They dispatch as separate "
                "coder sessions / PRs and will collide — whichever merges first "
                "turns the others into conflicts or cap-blocks (the planning-"
                "layer parallel-module-drift pattern; canonical IndianFoodTruck "
                "NextAuth cascade)."
            ),
            fix_hint=(
                f"Consolidate {ids} into ONE feature that owns `{path}` (reject "
                "the overlapping siblings, fold their ACs in), or re-scope so "
                "only one creates the file and the others import from it. "
                "Foundation setup sharing a new entrypoint must ship as a "
                "single cohesive feature — see the designer sizing gate's "
                "shared-entrypoint cohesion rule."
            ),
            occurrences=[f"{path}: features {ids}"],
            product_id=pid,
            dedupe_key=f"overlapping_features:{path}",
        ))
    return findings


# ────────────────────────────────────────────────────────────────────────────
# Orchestration
# ────────────────────────────────────────────────────────────────────────────


_DETECTORS = (
    detect_shell_artifact_files,
    detect_design_doc_mismatch,
    detect_placeholder_template_content,
    # 2026-06-09 five-product-audit batch, comment path. The medium-severity
    # detectors stay here (file-scoped feedback the next coder session acts
    # on via {reviewer_feedback}); secret_sentinel is high-severity but
    # deployed only 2026-06-10 — it finishes its report-only soak before
    # promotion (Guard-17-tuning protocol).
    detect_sandbox_path_literals,
    detect_stub_confessions,
    detect_secret_sentinel,
    # Wave-6 security loop (2026-06-13): timing-unsafe secret comparison.
    # On the comment path to soak (==-matching has a broader FP surface than
    # the CORS/schema detectors); promote to the chore sink after a clean
    # two-audit soak, per the Guard-17-tuning protocol that secret_sentinel
    # followed.
    detect_timing_unsafe_compare,
    # Wave-7 (2026-06-13): planning-layer collision detector — in-flight
    # features racing to co-create the same not-yet-shipped file. Comment-
    # path safety net behind the designer's shared-entrypoint cohesion rule
    # (the primary, prevention fix). Soaks here; consolidation is a judgment
    # call, never auto-mutated, so it stays comment-only.
    detect_overlapping_features,
)

# Objective code-drift detectors whose high-severity findings are routed to
# the corrective-chore sink (file_corrective_chores). Kept SEPARATE from
# _DETECTORS so the existing comment-only path (run_all + post_findings) and
# its three detectors are untouched — this is the preserved fallback.
_CHORE_DETECTORS = (
    detect_duplicate_ddl,
    detect_god_file,
    detect_public_route_blanket_with_auth,
    detect_mixed_error_envelopes,
    # Promoted from the comment path 2026-06-11 after a clean two-audit
    # soak (zero false positives across all five products, host and
    # in-container runs). Rationale: the testingcalc re-review showed
    # comment-path findings get READ but never REPAIRED — every known
    # defect sat untouched for 36h while new features shipped around it.
    # Chores give findings an owner.
    detect_tracked_build_artifacts,
    detect_undeclared_backend_deps,
    detect_unreachable_emitted_urls,
    # Wave-6 security loop (2026-06-13): the two near-zero-FP security
    # detectors go straight to the chore sink — the whole point (DogTinder
    # re-review) is that comment-path security findings get READ but never
    # REPAIRED. Both require a highly specific pattern, so FP risk is low
    # enough to skip the comment-path soak. Still gated by
    # RECONCILER_CHORES_ENABLED / product.config.reconciler_chores.
    detect_insecure_cors,
    detect_schema_dual_source,
)


def run_chore_detectors(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Run the chore-eligible detectors and concatenate findings.

    Best-effort, same contract as run_all: a detector that raises is logged
    and skipped. Returns Findings (typically severity="high") destined for
    file_corrective_chores. Does NOT touch run_all / the comment path.
    """
    out: list[Finding] = []
    for detector in _CHORE_DETECTORS:
        try:
            out.extend(detector(working_dir, features))
        except Exception as e:
            log.warning(
                "reconciler: detector %s raised %s; skipping",
                detector.__name__, e,
            )
    return out


def _pick_target_feature(features: list[dict]) -> int | None:
    """The feature to attach file/doc-scoped findings to. Picks the
    first assigned feature with an int id; returns None if none exists
    (caller should skip — no point posting a comment with no anchor)."""
    for f in features or []:
        fid = f.get("id")
        if isinstance(fid, int):
            return fid
    return None


def run_all(working_dir: str | Path, features: list[dict]) -> list[Finding]:
    """Run every detector and concatenate findings.

    Best-effort: any single detector that raises is logged and skipped;
    other detectors still run. Drift detection should never break a
    coder cycle.
    """
    out: list[Finding] = []
    for detector in _DETECTORS:
        try:
            out.extend(detector(working_dir, features))
        except Exception as e:
            log.warning(
                "drift-scanner: detector %s raised %s; skipping",
                detector.__name__, e,
            )
    return out


def _recent_drift_comments(pm_client, feature_id: int) -> list[str]:
    """Return the bodies of drift-scanner-authored comments on a feature.
    Used by post_findings to dedupe. Best-effort: GET failure → empty
    list (will let the finding through). The PM API returns comments
    newest-first; we take all of them since the comments table is
    typically small per feature."""
    try:
        resp = pm_client.get(f"/api/features/{feature_id}/comments")
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        return [
            c.get("body", "") for c in data
            if isinstance(c, dict) and c.get("author") == "drift-scanner"
        ]
    except Exception:
        return []


def _heal_design_doc_missing(pm_client, finding: "Finding", product_name: str) -> bool:
    """Auto-heal for category="design_doc_missing": clear the phantom
    design_doc_path so the next designer cycle re-authors a fresh doc.
    Also resets status Designed→Approved so the designer dispatcher
    actually picks the feature up (the persona router treats Designed
    features as ready for coder, which would just fail again on the
    missing file).

    Safe because the only authoritative source of truth for the design
    doc is the file at design_doc_path on the default branch. When the
    file is missing the path IS stale; clearing it loses no information.
    The post-doc pipeline already enforces that designer docs commit to
    main (not a feature branch), so the "doc lives on a different
    branch" case shouldn't occur under correct system behavior.

    Returns True on a successful heal, False on PM API failure (the
    finding's comment was still posted — operator can still manually
    triage).

    Canonical 2026-06-01 incidents: DocumentSign #1178 at 19:40:02 and
    #1177 at 20:01:00 both surfaced the same residual phantom-path
    state. Cycle CQ shipped the prevention guard in
    state_machine._apply_session_entry; this cycle adds the cleanup
    actuator so existing residue self-heals.
    """
    try:
        patch = {
            "design_doc_path": None,
            "changed_by": "drift-scanner:auto-heal",
        }
        # Demote Designed → Approved so the persona dispatcher routes the
        # feature back to the designer. For features already at Approved/
        # Implementing/Reviewing/etc., leave status alone — clearing
        # design_doc_path is sufficient and the right next step depends on
        # downstream pipeline state.
        try:
            r = pm_client.get(f"/api/features/{finding.feature_id}")
            if 200 <= r.status_code < 300:
                cur_status = (r.json() or {}).get("status")
                if cur_status == "Designed":
                    patch["status"] = "Approved"
        except Exception:
            # Skip the status demotion on GET failure; just clear the path.
            pass
        resp = pm_client.patch(
            f"/api/features/{finding.feature_id}", json=patch,
        )
        if 200 <= resp.status_code < 300:
            log.info(
                "[drift-scanner] %s: auto-healed design_doc_missing on "
                "feature #%s (cleared design_doc_path, status=%s)",
                product_name, finding.feature_id,
                patch.get("status", "<unchanged>"),
            )
            return True
        log.warning(
            "[drift-scanner] %s: auto-heal PATCH for feature #%s "
            "returned %s",
            product_name, finding.feature_id, resp.status_code,
        )
        return False
    except Exception as e:
        log.warning(
            "[drift-scanner] %s: auto-heal raised %s; finding's comment "
            "remains for manual triage",
            product_name, e,
        )
        return False


# Auto-heal actuators by finding category. A category absent from this
# map is comment-only (the prior behavior). Keep entries conservative —
# auto-heal is only safe when the action is deterministic, idempotent,
# and loses no information vs the existing comment-only flow.
_AUTO_HEAL_ACTIONS = {
    "design_doc_missing": _heal_design_doc_missing,
}


def post_findings(
    findings: Iterable[Finding],
    pm_client,
    product_name: str = "?",
) -> int:
    """POST each finding as a feature_comment with author="drift-scanner".

    Returns the count of successfully posted findings. Best-effort —
    individual POST failures are logged and don't abort the loop.

    Dedupe: before posting, GET the feature's existing comments and
    skip the finding if a drift-scanner-authored comment with the
    SAME exact body already exists. The finding body has a stable
    deterministic structure (category, severity, target, detail,
    fix_hint — none depend on timestamps), so byte-equal duplicates
    are real duplicates. This prevents noise when the same feature
    cycles through coder sessions repeatedly with the same drift
    still uncleared.

    Cache the per-feature comment list across findings in the same
    cycle so we make 1 GET per affected feature, not 1 per finding.
    """
    posted = 0
    skipped_dupe = 0
    _per_feature_cache: dict[int, list[str]] = {}
    for f in findings:
        body = f.as_comment_body()
        existing = _per_feature_cache.get(f.feature_id)
        if existing is None:
            existing = _recent_drift_comments(pm_client, f.feature_id)
            _per_feature_cache[f.feature_id] = existing
        is_dupe = body in existing
        if is_dupe:
            skipped_dupe += 1
        else:
            try:
                resp = pm_client.post(
                    f"/api/features/{f.feature_id}/comments",
                    json={"author": "drift-scanner", "body": body},
                )
                if 200 <= resp.status_code < 300:
                    posted += 1
                    # Remember our own post so a second finding with the
                    # same body in the same cycle doesn't double-post.
                    existing.append(body)
                    log.info(
                        "[drift-scanner] %s: filed %s on feature #%s",
                        product_name, f.category, f.feature_id,
                    )
                else:
                    log.warning(
                        "[drift-scanner] %s: POST comment for feature #%s "
                        "returned %s",
                        product_name, f.feature_id, resp.status_code,
                    )
            except Exception as e:
                log.warning(
                    "[drift-scanner] %s: post finding raised %s; skipping",
                    product_name, e,
                )

        # Auto-heal actuator runs REGARDLESS of comment dedupe (cycle CU
        # 2026-06-01). The dedupe check is for the diagnostic comment
        # only — it prevents log spam when the same finding cycles. The
        # heal is the actual state-cleanup actuator and must run on
        # every finding, including the dedupe path, so residual data
        # state (like a phantom design_doc_path with an existing
        # drift-scanner comment from before the heal was wired up) gets
        # corrected. The heal itself is idempotent: PATCHing
        # design_doc_path=null on a feature already null is harmless;
        # the Designed→Approved demote is a no-op once status is no
        # longer Designed.
        heal = _AUTO_HEAL_ACTIONS.get(f.category)
        if heal is not None:
            heal(pm_client, f, product_name)
    if skipped_dupe:
        log.info(
            "[drift-scanner] %s: deduped %s finding(s) already on feature(s)",
            product_name, skipped_dupe,
        )
    return posted


# ────────────────────────────────────────────────────────────────────────────
# Corrective-chore sink (reconciler-as-controller)
# ────────────────────────────────────────────────────────────────────────────
#
# Turns high-severity findings into Approved chore features that the existing
# coder→guard→reviewer→merge pipeline picks up and fixes. The pipeline is the
# actuator, so corrections get the same verification as any feature change —
# the reconciler can't silently break working code.
#
# Statuses that mean the chore is closed (don't dedupe against these).
_RECONCILER_CLOSED_STATUSES = frozenset({"Pushed", "Rejected", "Deferred", "Reverted"})
_RECONCILER_KEY_RE = re.compile(r"<!--\s*reconciler-key:\s*(\S+)\s*-->")


def _chore_name(f: Finding) -> str:
    n = len(f.occurrences)
    where = f" ({n} sites)" if n > 1 else ""
    return f"[reconciler] {f.category}: {f.target_id}{where}"


def _chore_body(f: Finding) -> str:
    # Locations go in a FENCED CODE BLOCK, not markdown `- ` bullets. The
    # /api/features story-sizing guard counts lines starting with `- ` or `* `
    # as acceptance criteria and rejects >4 with 422 ("story too big"). A
    # dedup chore with N>4 sites would otherwise be rejected — backwards, since
    # the worst dups have the most sites. Canonical 2026-05-29 calc3:
    # duplicate_ddl:calculations (11 sites) got 422 while :users (4) squeaked
    # through. Code-fence lines (`src/x.py:20`) don't match the bullet pattern.
    locs = "\n".join(f.occurrences) if f.occurrences else "(see detail)"
    n = len(f.occurrences)
    return (
        f"{f.detail}\n\n"
        f"Locations ({n} site{'s' if n != 1 else ''}):\n```\n{locs}\n```\n\n"
        f"Fix: {f.fix_hint}\n\n"
        "_Filed automatically by the drift reconciler. This is mechanical "
        "cleanup — keep the change tightly scoped to the locations above._\n"
        f"<!-- reconciler-key: {f.dedupe_key} -->"
    )


def _open_reconciler_chore_keys(pm_client, product_id: int) -> set[str]:
    """dedupe_keys of reconciler chores already OPEN for this product.

    Reads the reconciler-key marker out of each open chore's description so
    dedupe survives an orchestrator restart (no in-memory state). Best-effort:
    any API failure → empty set (lets the finding through; the per-cycle cap
    bounds the blast radius of a transient miss)."""
    keys: set[str] = set()
    try:
        resp = pm_client.get(f"/api/products/{product_id}/features")
        if not (200 <= resp.status_code < 300):
            return keys
        data = resp.json()
        if not isinstance(data, list):
            return keys
        for feat in data:
            if not isinstance(feat, dict):
                continue
            if feat.get("feature_type") != "chore":
                continue
            if feat.get("status") in _RECONCILER_CLOSED_STATUSES:
                continue
            m = _RECONCILER_KEY_RE.search(feat.get("description") or "")
            if m:
                keys.add(m.group(1))
    except Exception:
        return keys
    return keys


def file_corrective_chores(
    findings: Iterable[Finding],
    pm_client,
    product_id: int | None,
    product_name: str = "?",
    max_chores: int = 3,
) -> int:
    """File high-severity findings as Approved chore features.

    Returns the count filed. Behaviour:
      - Only `severity == "high"` findings are filed (others stay on the
        comment path via post_findings).
      - Most-occurrences-first, capped at `max_chores` per call so a messy
        product isn't flooded with dozens of chores in one cycle.
      - Deduped against currently-open reconciler chores by dedupe_key, so
        the same drift isn't re-filed every cycle until it's fixed.

    Chore field choices (validated against website/schemas.py FeatureCreate):
      - status="Approved"  → skips the PM gate; immediately eligible. The
        internal API bypasses PM_ALLOWED_TRANSITIONS, so create-as-Approved
        is permitted for this caller.
      - priority=1         → LOWEST number = HIGHEST rank under the unified
        ASC convention. Both the PM API
        (`/api/features/next-for-persona`, `website/main.py:739`) and
        the session-launcher path
        (`orchestrator/docker_runner._fetch_assigned_features`, as of
        cycle GM 2026-06-04) sort priority ASC. Chores at priority=1
        jump ahead of standard features (default=50) and PM-urgent
        (manual values 2–10). Pre-cycle-GM the dispatcher used DESC
        and this filer used priority=99 to compensate; the convention
        is now unified ASC and priority=1 is correct. Existing in-DB
        chores at priority=99 from the old convention are mostly in
        Deferred/Blocked status and don't get picked anyway; no
        migration needed.
      - source="ai"        → schema validator allows only 'pm'|'ai'.
      - feature_type="chore".
    (skip_design and labels are not FeatureCreate fields — omitted in v1;
    the chore goes through the designer first, which is acceptable for a
    mechanical task. skip_design routing is a future optimization.)
    """
    high = [f for f in findings if f.severity == "high"]
    if not high or product_id is None:
        return 0
    high.sort(key=lambda f: len(f.occurrences), reverse=True)
    open_keys = _open_reconciler_chore_keys(pm_client, product_id)
    filed = 0
    for f in high:
        if filed >= max_chores:
            break
        if f.dedupe_key in open_keys:
            continue
        try:
            resp = pm_client.post("/api/features", json={
                "product_id":   product_id,
                "name":         _chore_name(f),
                "description":  _chore_body(f),
                "feature_type": "chore",
                "status":       "Approved",
                "priority":     1,
                "source":       "ai",
            })
            if 200 <= resp.status_code < 300:
                filed += 1
                open_keys.add(f.dedupe_key)  # don't double-file within this call
                log.info(
                    "[reconciler] %s: filed chore for %s (%s site(s))",
                    product_name, f.dedupe_key, len(f.occurrences),
                )
            else:
                log.warning(
                    "[reconciler] %s: POST /api/features for %s returned %s",
                    product_name, f.dedupe_key, resp.status_code,
                )
        except Exception as e:
            log.warning(
                "[reconciler] %s: filing chore for %s raised %s; skipping",
                product_name, f.dedupe_key, e,
            )
    return filed
