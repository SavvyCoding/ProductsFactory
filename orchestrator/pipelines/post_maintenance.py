"""
Path B post-session pipeline for the on-demand maintenance personas
(documenter, analytics, recommender, devops, refactorer).

Same delegation as post_coder / post_doc: agents are responsible for *editing
files*; this pipeline owns the git ceremony (`add`, `commit`, `push` to
`origin/main`). Before this lived, the maintenance prompts asked the agent
to run `git commit && git push` themselves — and Ollama models (kimi-k2.6
in particular) routinely called `task_done` after writing the files but
*before* running the git block, leaving the work in the local working tree
where the next `_cleanup_workspace_post_session` reset would discard it.
Real incident: documenter session 2316 (e72365da, 2026-05-12) — kimi spent
24.7 min and 524k tokens producing a high-quality README/ARCHITECTURE
rewrite, then ended via task_done without pushing; the changes were
silently dropped on the next workspace reset.

Always pushes to `origin/main`. These personas are sprint-independent —
they document, analyse, or audit the *whole product* — so there is no
sprint_branch to target. No feature DB writes either (the agent already
creates Pending features via the PM API where applicable; see
recommender/devops/refactorer prompts).

Idempotent: if `git status --porcelain` shows no changes (which is the
expected steady-state for recommender/devops/refactorer, which only POST
to the PM API and don't touch the file tree), the pipeline returns early.
On any push failure the local commit is left in place; the next
`_cleanup_workspace_post_session` discards it, and the next on-demand run
redoes the work.
"""

import logging
import os
import re
import subprocess as _sp

from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


# Per-persona write allowlists for maintenance commits. Same shape as
# post_doc's allowlist but persona-keyed because architect needs to write
# ARCHITECTURE.md (the whole point of putting it on this persona is so the
# document stays current) while every other maintenance persona must not.
# Paths are fnmatch globs against forward-slashed relative paths.
#
# Architect specifically gains write access to ARCHITECTURE.md as the *only*
# persona authorized to maintain it (designer is blocked by post_doc's
# allowlist; coders write through post_coder which is feature-scoped).
# Architect's prompt scopes WHAT it may edit inside ARCHITECTURE.md (MODULES
# and DEPRECATED rows only -- no section rewrites); this allowlist enforces
# the PATH boundary, the prompt enforces the in-file scope.
_MAINTENANCE_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "architect": (
        "ARCHITECTURE.md",
        "docs/architecture_review_*.md",
        "product_memory.md",
        "session_summary.md",
    ),
    "documenter": (
        "README.md",
        "docs/*.md",
        "docs/**/*.md",
        "product_memory.md",
        "session_summary.md",
    ),
    "analytics": (
        "docs/analytics_*.md",
        "docs/*_analytics.md",
        "product_memory.md",
        "session_summary.md",
    ),
    "refactorer": (
        # Refactorer needs broad source-code access by definition.
        # Locking it down properly needs a separate review pass; for now
        # keep the existing behaviour (no enforcement) by allowing the
        # universal pattern. Future work: lock to specific dirs per stack.
        "*",
        "**/*",
    ),
    "devops": (
        ".github/**/*",
        "Dockerfile",
        "docker-compose.yml",
        "Makefile",
        "scripts/*",
        "product_memory.md",
        "session_summary.md",
    ),
    "recommender": (
        # Recommender only POSTs to the PM API (creates Pending features);
        # it doesn't write files. Keep the bookkeeping appends allowed in
        # case the prompt is later expanded.
        "product_memory.md",
        "session_summary.md",
    ),
    "product_trainer": (
        "docs/*.md",
        "product_memory.md",
        "session_summary.md",
    ),
}


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    import fnmatch
    normalized = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatchcase(normalized, pat):
            return True
    return False


# The six Phase-1 section headers that must remain present in ARCHITECTURE.md.
# Downstream consumers (orchestrator/session/context_builder.py, post_coder
# Guards 13/14, the architect persona itself) all depend on these. If the
# architect's inline edit accidentally strips a header, the consumer breaks
# silently.
_REQUIRED_ARCHITECTURE_SECTIONS = (
    "ENTRY POINTS",
    "MODULES",
    "RULES",
    "REFERENCE PATTERNS",
    "CONFIG GATES",
    "DEPRECATED",
)

# The full set of section headers permitted in ARCHITECTURE.md. Anything else
# is unauthorized creation (e.g. architect-ce3601f7 on MyDocusign 2026-05-21
# added a 58-line `## AWS Shield Standard` section out of nowhere).
# Includes the six required plus the soft/template sections that ship in the
# stack templates (templates/stacks/*/ARCHITECTURE.md). The presence check
# refuses BOTH: any required header missing AND any non-canonical header
# present. Effectively: set(headers_in_file) must equal a subset of
# _ALLOWED_ARCHITECTURE_SECTIONS, and must contain every required header.
_ALLOWED_ARCHITECTURE_SECTIONS = _REQUIRED_ARCHITECTURE_SECTIONS + (
    "Directory structure",
    "Patterns in use",
    "Naming conventions",
    "Do not change without PM approval",
    "Pre-existing test failures (brownfield only)",
)


def _check_required_arch_sections(working_dir: str) -> list[str]:
    """Return a violation list if ARCHITECTURE.md's section headers don't
    match the canonical set: every required header must be present AND no
    unauthorized header may have been added.

    Two failure modes both produce violations:

      1. Missing required header. Downstream consumers break silently.
         Real incident: designer-63e95f37 commit 5296af4 (MyDocusign 2026-05-20)
         deleted RULES, REFERENCE PATTERNS, CONFIG GATES, DEPRECATED.

      2. Unauthorized header added. The architect's prompt forbids creating
         new sections; the §5 helper only edits rows. But an LLM bypassing
         the helper can extend the file freely. Real incident: architect
         session ce3601f7 (MyDocusign 2026-05-21) added a 58-line `## AWS
         Shield Standard` section -- documentation creation, not drift
         correction. The architect ran twice and each run made ARCHITECTURE.md
         worse rather than better.

    Headers are matched case-sensitively against `^## <NAME>` since
    context_builder._section_blob uses exact-case + word-boundary matching.
    """
    import re as _re
    arch_path = os.path.join(working_dir, "ARCHITECTURE.md")
    try:
        with open(arch_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return []  # no file = nothing to check (greenfield first-arch run)
    except Exception as e:
        return [f"ARCHITECTURE.md unreadable: {e}"]

    # All h2 headers actually in the file.
    actual = [m.group(1).strip() for m in
              _re.finditer(r"^##\s+(.+?)\s*$", content, _re.MULTILINE)]
    actual_set = set(actual)
    required_set = set(_REQUIRED_ARCHITECTURE_SECTIONS)
    allowed_set = set(_ALLOWED_ARCHITECTURE_SECTIONS)

    violations: list[str] = []

    missing = required_set - actual_set
    if missing:
        violations.append(
            f"ARCHITECTURE.md is missing required section header(s): "
            f"{', '.join('`## ' + s + '`' for s in sorted(missing))}. "
            f"Downstream consumers (pre-coder context, post-coder lint guards, "
            f"architect persona itself) depend on these. Use the §5 Python "
            f"helper in the architect prompt -- its fence_off check refuses "
            f"to touch contract sections."
        )

    unauthorized = actual_set - allowed_set
    if unauthorized:
        violations.append(
            f"ARCHITECTURE.md has unauthorized section header(s): "
            f"{', '.join('`## ' + s + '`' for s in sorted(unauthorized))}. "
            f"The architect (and any agent that touches ARCHITECTURE.md) is "
            f"restricted to row-level edits in the canonical sections "
            f"({', '.join(_ALLOWED_ARCHITECTURE_SECTIONS)}). Adding new "
            f"sections is out of scope -- if the content is documentation "
            f"that doesn't fit those sections, write it to "
            f"`docs/architecture_review_<date>.md` for PM review instead."
        )

    return violations


def _path_exists_exact_case(working_dir: str, relpath: str) -> bool:
    """True iff `relpath` exists under `working_dir`, matching case EXACTLY at
    every path component.

    `os.path.exists` is case-INSENSITIVE on Windows/macOS bind mounts (Docker
    Desktop surfaces NTFS that way), so `os.path.exists(wd/'SRC')` returns True
    when only `src/` exists — which is exactly how the architect's DEPRECATED
    `SRC/` phantom survives (and why its own `find SRC/` inventory can falsely
    succeed). Walking listdir() per component compares against the REAL stored
    names, so it's correct on case-sensitive and case-insensitive filesystems
    alike.
    """
    parts = [p for p in relpath.replace("\\", "/").split("/") if p]
    cur = working_dir
    for part in parts:
        try:
            entries = os.listdir(cur)
        except Exception:
            return False
        if part not in entries:
            return False
        cur = os.path.join(cur, part)
    return True


def _prune_phantom_deprecated(working_dir: str) -> int:
    """Remove DEPRECATED entries that name a concrete path which doesn't exist.

    The architect persona has no existence-check on its DEPRECATED list, so it
    carries phantom entries forward indefinitely — and even "maintains" them
    (calc3 2026-05-28: the `SRC/` entry, a directory that never existed, had
    its file count updated 5→8 to track `src/`'s growth). Every phantom is
    injected into the pre-coder context as a bogus "⚠ DEPRECATED — do not use"
    warning and arms Guard 13 against a path no one will ever create.

    Only concrete paths are pruned. Glob/pattern entries (`*.bak`, `temp_*`)
    and the renderer placeholder (`_(...)_`) are left untouched — they can't be
    existence-checked. Returns the number of entries removed; rewrites
    ARCHITECTURE.md in place only if something was removed. Best-effort: never
    raises. Preserves the `## DEPRECATED` header (so the section-integrity
    check still passes).
    """
    import re as _re
    arch = os.path.join(working_dir, "ARCHITECTURE.md")
    try:
        with open(arch, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return 0
    m = _re.search(r"(^##\s+DEPRECATED\b[^\n]*\n)(.*?)(?=\n##\s|\Z)",
                   content, _re.S | _re.M)
    if not m:
        return 0
    body = m.group(2)
    kept: list[str] = []
    removed = 0
    for line in body.split("\n"):
        bm = _re.match(r"^\s*-\s+`([^`]+)`", line)
        if bm:
            token = bm.group(1).rstrip("/")
            is_concrete = bool(token) and "*" not in token and not token.startswith("_(")
            if is_concrete and not _path_exists_exact_case(working_dir, token):
                removed += 1
                continue  # drop the phantom bullet
        kept.append(line)
    if removed == 0:
        return 0
    new_content = content[:m.start(2)] + "\n".join(kept) + content[m.end(2):]
    try:
        with open(arch, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception:
        return 0
    return removed


# Claim patterns the architect writes into bookkeeping files about edits to
# ARCHITECTURE.md. Matched against the ADDED lines of the staged diff only.
# Deliberately narrow — each pattern comes from a real false claim (MyJira /
# testingcalc 2026-06-09 audit: "3 DEPRECATED entries added" reported across
# two sessions while the DEPRECATED section stayed empty; testingcalc
# architect claimed a `src/db.py::init_db` DEPRECATED entry that never
# landed).
_ARCH_EDIT_CLAIM_RES = (
    re.compile(r"DEPRECATED\s+entr(?:y|ies)\b.{0,60}\b(added|updated|removed)", re.I),
    re.compile(r"\b(added|updated|removed)\b.{0,60}DEPRECATED\s+entr", re.I),
    re.compile(r"MODULES\s+(?:row|entr(?:y|ies))\b.{0,60}\b(added|updated|removed)", re.I),
    re.compile(r"\b(added|updated|removed)\b.{0,60}\bMODULES\s+(?:row|entr)", re.I),
    re.compile(r"\b(added|updated|edited)\b.{0,40}\bARCHITECTURE\.md", re.I),
)

_CLAIM_SCAN_FILES = ("product_memory.md", "session_summary.md")


def _verify_architect_edit_claims(
    working_dir: str, _run, product_name: str = "?",
) -> int:
    """Architect sessions report file edits ("3 DEPRECATED entries added")
    in product_memory.md / review docs that were never actually persisted to
    ARCHITECTURE.md. Once a false claim enters the bookkeeping files, later
    sessions re-assert it as observation — the 2026-06-09 audit found the
    same phantom claim propagated across 5 consecutive architect sessions.

    Deterministic check, post-staging: if the staged ADDED lines of the
    bookkeeping files (or a staged docs/architecture_review_*.md) claim an
    ARCHITECTURE.md edit but ARCHITECTURE.md itself is NOT in the staged
    diff, append a correction note to product_memory.md (and re-stage it)
    so the false history is flagged at the source the next session reads.

    Returns the number of unverified claims annotated. Best-effort: never
    raises.
    """
    try:
        staged_r = _run(["git", "diff", "--cached", "--name-only"], timeout=15)
        if staged_r.returncode != 0:
            return 0
        staged_names = [ln.strip() for ln in (staged_r.stdout or "").splitlines() if ln.strip()]
        if any(n.endswith("ARCHITECTURE.md") for n in staged_names):
            return 0  # the claimed edit (whatever its content) exists — pass
        claim_files = [
            n for n in staged_names
            if n in _CLAIM_SCAN_FILES
            or (n.startswith("docs/") and "architecture_review" in n)
        ]
        if not claim_files:
            return 0
        claims: list[str] = []
        for name in claim_files:
            diff_r = _run(
                ["git", "diff", "--cached", "-U0", "--", name], timeout=15,
            )
            if diff_r.returncode != 0:
                continue
            for raw in (diff_r.stdout or "").splitlines():
                if not raw.startswith("+") or raw.startswith("+++"):
                    continue
                line = raw[1:].strip()
                if any(p.search(line) for p in _ARCH_EDIT_CLAIM_RES):
                    claims.append(f"{name}: {line[:120]}")
        if not claims:
            return 0
        note_lines = "\n".join(f"> {c}" for c in claims[:5])
        note = (
            "\n[ORCHESTRATOR-NOTE post-architect] The following claim(s) "
            "this session about ARCHITECTURE.md edits are UNVERIFIED — "
            "ARCHITECTURE.md is not in the session's staged diff. Treat "
            "them as not done; the next architect session should re-apply "
            "the edit and verify it appears in `git diff` before reporting "
            "it:\n" + note_lines + "\n"
        )
        memory_path = os.path.join(working_dir, "product_memory.md")
        with open(memory_path, "a", encoding="utf-8") as f:
            f.write(note)
        _run(["git", "add", "--", "product_memory.md"], timeout=15)
        log.warning(
            f"[post-architect] {product_name}: {len(claims)} ARCHITECTURE.md "
            f"edit claim(s) with no staged ARCHITECTURE.md change — "
            f"annotated product_memory.md"
        )
        return len(claims)
    except Exception:
        log.debug("architect claim verification raised", exc_info=True)
        return 0


def _stage_allowed_paths(
    persona: str, working_dir: str, _run, product_name: str = "?",
) -> tuple[int, list[str]]:
    """For personas with an allowlist, selectively stage ONLY paths that match
    the allowlist. Out-of-scope changes are left in the working tree (next
    `_reset_workspace` wipes them) and never enter the commit.

    Returns (staged_count, stripped_paths). For personas with no allowlist,
    falls back to `git add -A` and returns (-1, []) to signal "no filtering
    applied". The pipeline interprets staged_count == 0 as "nothing to
    commit after filtering" and exits early.

    This is the soft-allowlist enforcement path -- the alternative to the
    strict-refusal `_post_maintenance_allowlist_check`. Both exist:
    soft-allowlist runs first (filters at stage time), strict check runs
    after as defense in depth (catches anything that snuck through).

    Real failure mode (2026-05-20 sessions 2928 + 2933): architect repeatedly
    deleted features.md and left sed backup artifacts because the LLM has
    a strong "deprecated file → delete" prior that the strengthened prompt
    didn't override. Strict refusal cost the legitimate ARCHITECTURE.md edits
    on both runs. Soft-allowlist lets the good work land while silently
    discarding the bad ops.
    """
    allowlist = _MAINTENANCE_ALLOWLISTS.get(persona)
    if allowlist is None:
        # Unknown persona — preserve existing behaviour
        add_r = _run(["git", "add", "-A"], timeout=60)
        if add_r.returncode != 0:
            log.warning(
                f"[post-{persona}] {product_name}: git add -A failed -- "
                f"{add_r.stderr.strip()[:200]}"
            )
        return (-1, [])

    status_r = _run(["git", "status", "--porcelain"], timeout=30)
    if status_r.returncode != 0:
        log.warning(
            f"[post-{persona}] {product_name}: git status failed -- "
            f"falling back to git add -A (will trigger strict allowlist check)"
        )
        _run(["git", "add", "-A"], timeout=60)
        return (-1, [])

    staged: list[str] = []
    stripped: list[tuple[str, str]] = []  # (status_letter, path)
    for line in status_r.stdout.splitlines():
        if not line.strip():
            continue
        # `git status --porcelain` line format: `XY path` where X is index
        # status, Y is worktree status; X/Y can be ` MADRCU?!`. For renames
        # the line is `RY origpath -> newpath`. Take the worktree status
        # (Y) for our routing decision and the LAST path token.
        status_pair = line[:2]
        rest = line[2:].strip()
        # Handle rename arrow
        path = rest.split(" -> ")[-1].strip().strip('"')
        if not path:
            continue
        # Skip the same Temp/Results filter the original `changed` list used.
        if "/Temp/" in path or "/Results/" in path:
            continue
        status_letter = status_pair[1] if status_pair[1] != " " else status_pair[0]
        if status_letter == " ":
            continue
        if _path_matches(path, allowlist):
            staged.append(path)
        else:
            stripped.append((status_letter, path))

    # Stage the allowed paths. `git add -A -- <path>` handles M/A/D/R uniformly.
    for path in staged:
        _run(["git", "add", "-A", "--", path], timeout=30)

    if stripped:
        verbs = {
            "M": "modify", "D": "delete", "R": "rename",
            "C": "copy", "T": "change type of", "A": "add", "?": "add (untracked)",
        }
        log.warning(
            f"[post-{persona}] {product_name}: stripped {len(stripped)} "
            f"out-of-scope change(s) from commit (left in working tree, will "
            f"be wiped by next workspace reset):"
        )
        for st, p in stripped:
            verb = verbs.get(st, st)
            log.warning(f"  - {verb} `{p}` -- not in {persona}'s allowlist")

    return (len(staged), [p for _, p in stripped])


def _post_maintenance_allowlist_check(
    persona: str, working_dir: str, _run, product_name: str = "?",
) -> list[str]:
    """Refuse a maintenance commit that touches paths outside the persona's
    allowed write set.

    Returns a list of violation strings; empty list = clean (commit may proceed).

    The architect is currently the only persona that may modify ARCHITECTURE.md
    -- this is what fixes the post-2026-05-20 gap where designer was locked
    out of ARCHITECTURE.md edits (post_doc allowlist) but no other persona
    was authorized to update it either. If a new maintenance persona later
    needs ARCHITECTURE.md write access, add it to _MAINTENANCE_ALLOWLISTS.

    Personas not listed in _MAINTENANCE_ALLOWLISTS are treated as "unknown" --
    we don't second-guess; the check passes them through (logged as info).
    This matches the existing failure mode where unknown personas just run
    without an allowlist.
    """
    allowlist = _MAINTENANCE_ALLOWLISTS.get(persona)
    if allowlist is None:
        log.info(
            f"[post-{persona}] {product_name}: no allowlist defined "
            f"for persona; commit passes through unfiltered"
        )
        return []

    try:
        diff_r = _run(["git", "diff", "--cached", "--name-status"], timeout=15)
    except Exception:
        return []
    if diff_r.returncode != 0:
        return []

    violations: list[str] = []
    for raw in (diff_r.stdout or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        status_raw = parts[0]
        if not status_raw:
            continue
        status = status_raw[0]
        path = parts[-1].strip()
        if not path:
            continue
        if _path_matches(path, allowlist):
            continue
        verb = {
            "M": "modify", "D": "delete", "R": "rename",
            "C": "copy", "T": "change type of", "A": "add",
        }.get(status, status)
        violations.append(
            f"Persona `{persona}` attempts to {verb} `{path}` -- "
            f"outside its maintenance allowlist. Permitted patterns: "
            f"{', '.join('`' + p + '`' for p in allowlist)}."
        )

    # Architect-specific defensive check: if ARCHITECTURE.md is being modified,
    # verify all six required Phase-1 section headers remain present. Catches
    # the case where the architect bypassed the prompt's §5 helper and edited
    # the file by hand, stripping a contract section header.
    if persona == "architect":
        arch_touched = any(
            line.endswith("ARCHITECTURE.md")
            for line in (diff_r.stdout or "").splitlines()
        )
        if arch_touched:
            violations.extend(_check_required_arch_sections(working_dir))

    return violations


def _run_post_maintenance_pipeline(product: dict, session_uid: str,
                                    working_dir: str, persona: str) -> None:
    pname = product.get("name", "?")

    _chmod_workspace_via_alpine(working_dir, pname)

    def _run(cmd: list[str], **kw) -> _sp.CompletedProcess:
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True,
                       timeout=timeout, **kw)

    status = _run(["git", "status", "--porcelain"])
    changed = [ln for ln in status.stdout.splitlines() if ln.strip()
               and "/Temp/" not in ln and "/Results/" not in ln]
    if not changed:
        log.info(f"[post-{persona}] {pname}: no changes to commit")
        return
    log.info(f"[post-{persona}] {pname}: {len(changed)} changed file(s) — sample {changed[:3]}")

    # Sync local main against origin without losing the agent's uncommitted
    # writes. Same stash-then-checkout-then-pop pattern as post_doc, with
    # conflict resolution favouring the agent's content (their writes are
    # the new docs / analytics report; origin/main is the baseline).
    from orchestrator.integrations.git_ops import git_fetch_authenticated
    git_fetch_authenticated(["origin"], cwd=working_dir, product_name=pname, timeout=120)
    stash_r = _run(["git", "stash", "push", "-u", "-m",
                    f"post-{persona}-{session_uid}"], timeout=300)
    stashed = (stash_r.returncode == 0
               and "No local changes to save" not in (stash_r.stdout or ""))
    co = _run(["git", "checkout", "-B", "main", "origin/main"])
    if co.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git checkout -B main origin/main "
                    f"failed — rc={co.returncode} {co.stderr.strip()[:200]}")
        if stashed:
            _run(["git", "stash", "pop"])  # best-effort restore
        return
    if stashed:
        pop_r = _run(["git", "stash", "pop"])
        if pop_r.returncode != 0:
            conflicts = _run(["git", "diff", "--name-only", "--diff-filter=U"])
            paths = [p for p in conflicts.stdout.splitlines() if p.strip()]
            if paths:
                _run(["git", "checkout", "--theirs", "--"] + paths)
                _run(["git", "add", "--"] + paths)
                log.info(f"[post-{persona}] {pname}: resolved {len(paths)} "
                         f"stash-pop conflict(s) in favor of agent")
            _run(["git", "stash", "drop"])

    # Architect-only: prune phantom DEPRECATED entries (paths that don't
    # exist, exact-case) the LLM keeps carrying forward. Runs after the
    # origin/main sync so it checks against the current tree, and before
    # staging so the pruned ARCHITECTURE.md is committed alongside the
    # architect's other edits. Deterministic — the prompt already asks the
    # architect to remove deleted entries and it doesn't comply.
    if persona == "architect":
        try:
            pruned = _prune_phantom_deprecated(working_dir)
            if pruned:
                log.info(
                    f"[post-{persona}] {pname}: pruned {pruned} phantom "
                    f"DEPRECATED entry/entries (path does not exist)"
                )
        except Exception:
            log.debug("phantom-DEPRECATED prune raised", exc_info=True)

    # Soft-allowlist staging: for personas with an allowlist (architect,
    # documenter, devops, etc.), stage ONLY paths that match the allowlist.
    # Out-of-scope changes (e.g., architect's repeated features.md deletion
    # attempt, sed -i backup artifacts) stay in the working tree and get
    # wiped by the next _reset_workspace; they never enter the commit.
    # Personas without an allowlist (or unknown ones) fall back to git add -A.
    staged_count, stripped = _stage_allowed_paths(persona, working_dir, _run, pname)

    # Architect-only: verify that claims about ARCHITECTURE.md edits in the
    # staged bookkeeping files are backed by a staged ARCHITECTURE.md change;
    # annotate product_memory.md when they aren't (false-history containment
    # — see _verify_architect_edit_claims).
    if persona == "architect":
        _verify_architect_edit_claims(working_dir, _run, pname)

    cached = _run(["git", "diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        if stripped:
            log.info(
                f"[post-{persona}] {pname}: nothing in scope to commit "
                f"({len(stripped)} out-of-scope change(s) stripped) — done"
            )
        else:
            log.info(f"[post-{persona}] {pname}: nothing staged after add — done")
        return

    # Defense-in-depth allowlist check. With soft-allowlist staging above,
    # this should be a no-op for personas with an allowlist; kept as a
    # belt-and-suspenders gate in case the soft path missed something
    # (e.g., a path-matcher bug). For unknown personas without an allowlist,
    # this is the primary enforcement.
    violations = _post_maintenance_allowlist_check(persona, working_dir, _run, pname)
    if violations:
        log.warning(
            f"[post-{persona}] {pname}: allowlist refused commit "
            f"({len(violations)} out-of-scope path(s)):"
        )
        for v in violations:
            log.warning(f"  - {v}")
        log.warning(
            f"[post-{persona}] {pname}: maintenance commit discarded; "
            f"the local edits will be cleaned by the next workspace reset"
        )
        return

    # --no-verify: pre-commit hooks (husky, lint-staged) installed by the
    # agent commonly fail outside the agent container because their binaries
    # aren't on the orchestrator's PATH. The maintenance commit is mechanical
    # docs/report content; lint hooks add no value here.
    commit_msg = f"{persona}: maintenance update [{persona}-{session_uid}]"
    commit_r = _run(["git", "commit", "--no-verify", "-m", commit_msg])
    if commit_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git commit failed — "
                    f"{commit_r.stderr.strip()[:200]}")
        return

    from orchestrator.integrations.git_ops import git_push_authenticated
    push_r = git_push_authenticated(
        ["--no-verify", "origin", "main"],
        cwd=working_dir, product_name=pname, timeout=180,
    )
    if push_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git push origin main failed — "
                    f"{push_r.stderr.strip()[:200]}")
        return

    log.info(f"[post-{persona}] {pname}: pushed maintenance commit to origin/main")
