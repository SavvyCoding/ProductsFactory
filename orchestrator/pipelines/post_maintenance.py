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

    # Soft-allowlist staging: for personas with an allowlist (architect,
    # documenter, devops, etc.), stage ONLY paths that match the allowlist.
    # Out-of-scope changes (e.g., architect's repeated features.md deletion
    # attempt, sed -i backup artifacts) stay in the working tree and get
    # wiped by the next _reset_workspace; they never enter the commit.
    # Personas without an allowlist (or unknown ones) fall back to git add -A.
    staged_count, stripped = _stage_allowed_paths(persona, working_dir, _run, pname)

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
