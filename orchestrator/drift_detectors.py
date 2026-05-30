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


def detect_design_doc_mismatch(
    working_dir: str | Path, features: list[dict]
) -> list[Finding]:
    """Features whose ``design_doc_path`` points to a file that doesn't
    exist on the current branch. The DB believes the designer landed a
    doc; the filesystem disagrees. Real-world cause (2026-05-27 calcv2,
    features 995/996): designer wrote the doc to /workspace/docs/ and
    posted ``{"design_doc_path": ...}`` to session_result.json, but the
    post-doc commit didn't include the file (e.g. allowlist stripped it
    or it was wiped by a workspace reset before the commit). Reconcile
    updates the DB from session_result.json; the file never lands.

    Future coder sessions on the same feature read an empty
    design_doc_path → /workspace/docs/X.md, find nothing, improvise.
    """
    wd = Path(working_dir)
    findings: list[Finding] = []
    for f in features or []:
        doc_path = f.get("design_doc_path") or ""
        if not doc_path:
            continue
        fid = f.get("id")
        if not isinstance(fid, int):
            continue
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
                "DB, but that file does not exist in the working tree. "
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
# Orchestration
# ────────────────────────────────────────────────────────────────────────────


_DETECTORS = (
    detect_shell_artifact_files,
    detect_design_doc_mismatch,
    detect_placeholder_template_content,
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
        if body in existing:
            skipped_dupe += 1
            continue
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
      - priority=1         → selection orders by `Feature.priority` ASC, so
        the LOWEST number is picked first; 1 is the highest urgency the
        schema allows (validator floor is 1 — 0 is rejected). Do NOT "fix"
        this to a high number; that would sort the chore to the BACK.
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
