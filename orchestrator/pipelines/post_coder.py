"""
Post-session pipeline for coder runs — the orchestrator-side git ceremony.

Path B (current architecture): the coder agent ONLY writes code to the
workspace. This pipeline takes care of every subsequent git step:
  1. Detect agent-handled vs not-handled features (verifies claimed PRs
     have real ``[feature-N]`` commit tags on GitHub before trusting them)
  2. Detect uncommitted changes / unpushed commits in the workspace
  3. Resolve target branch (1-PR model, migration 043):
       - rework mode  → all features point at one open PR; force-push to it
       - session mode → cut a fresh ``coder/<session_uid>`` branch off the
         default branch tip and open a session PR direct to main
  4. add + commit (with ``[feature-N]`` tags) + push (force-with-lease in rework)
  5. Append Reviewing entries to session_result.json (with PM-API fallback PATCH
     if the file write fails — keeps a real GitHub PR from being stranded)

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
This is the largest single function in the orchestrator (~500 LOC). Phase 3
will decompose it into 4 helpers (detect_unpushed_work, verify_agent_pr_tags,
commit_and_push, record_reviewing_entries).
"""

import ast as _ast
import contextlib
import json as _json
import logging
import os
import re
import shlex as _shlex
import shutil
import subprocess as _sp
from pathlib import Path

import httpx

from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine
from orchestrator.integrations.github import _get_gh_token, _parse_repo_slug
from orchestrator.session.result_io import _filter_session_result_by_id
from orchestrator.paths import host_path

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


# Files coders may NOT modify. The coder's scope is broad (src/, tests/,
# alembic/, requirements.txt, etc.) so a denylist is more compact than an
# allowlist. These paths are PM-curated contracts or other-persona territory:
#
#   - ARCHITECTURE.md       — architect-only (post-maintenance allowlist)
#   - CLAUDE.md             — stack-template, PM-curated at greenfield
#   - AGENT_WORKFLOW.md     — stack-template, PM-curated
#   - CONTRIBUTING.md       — stack-template, PM-curated
#   - quality_gates.json    — PM-curated quality bars (Guard 14 cross-checks)
#   - product_config.json   — PM-set greenfield config
#   - product_memory.md     — architect / cross-session bookkeeping
#   - .gitignore            — PM-set at greenfield (Phase 1 of quality-specs)
#   - session_summary.md    — other-persona working file
#
# Real incident driving this denylist: MyDocusign 2026-05-21 PR #36 was a
# coder commit that added a 58-line `## AWS Shield Standard` section to
# ARCHITECTURE.md (out of coder scope) AND wrote
# tests/docs/test_architecture_docs.py to verify the section exists. Both
# slipped through because post_coder.py had no path enforcement -- only
# post_doc.py (designer) and post_maintenance.py (architect + others) did.
# Soft enforcement (silently strip out-of-scope changes from the staged
# set) lets the legitimate work still ship and the PR still open.
_CODER_DENYLIST = (
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "AGENT_WORKFLOW.md",
    "CONTRIBUTING.md",
    "quality_gates.json",
    "product_config.json",
    "product_memory.md",
    ".gitignore",
    "session_summary.md",
)


def _coder_stage_with_denylist(working_dir: str, _run, product_name: str = "?") -> tuple[int, list[str]]:
    """Stage all changed paths EXCEPT those in _CODER_DENYLIST. Mirrors the
    post-maintenance soft allowlist (filter at staging time, leave out-of-scope
    changes in working tree, let next workspace reset wipe them).

    Returns (staged_count, stripped_paths). Out-of-scope changes don't make
    it into the commit; the PR opens with only in-scope changes.

    Soft enforcement rationale (mirrors post-maintenance soft allowlist):
    the alternative is strict refusal of the entire commit, which would burn
    fix_attempts on the feature for what is often an unrelated agent-debris
    issue. With soft, the coder's legitimate code change still ships through
    the PR pipeline; the unauthorized ARCHITECTURE.md edit silently dies.
    """
    import fnmatch as _fnmatch
    status_r = _run(["git", "status", "--porcelain"], timeout=30)
    if status_r.returncode != 0:
        log.warning(
            f"[post-coder] {product_name}: git status failed -- falling back "
            f"to git add -A (denylist bypassed for this commit)"
        )
        _run(["git", "add", "-A"], timeout=60)
        return (-1, [])

    staged: list[str] = []
    stripped: list[tuple[str, str]] = []  # (status_letter, path)
    for line in status_r.stdout.splitlines():
        if not line.strip():
            continue
        status_pair = line[:2]
        rest = line[2:].strip()
        # Handle rename arrow `R  old -> new` (take destination).
        path = rest.split(" -> ")[-1].strip().strip('"')
        if not path:
            continue
        # Skip the same Temp/Results filter post_doc / post_maintenance use.
        if "/Temp/" in path or "/Results/" in path:
            continue
        status_letter = status_pair[1] if status_pair[1] != " " else status_pair[0]
        if status_letter == " ":
            continue
        # Denylist match -- forward-slash normalized for Windows safety.
        normalized = path.replace("\\", "/")
        # Shell-artifact filename: basename starts with a shell
        # comparator / redirect / pipe / job-control char. These are never
        # legitimate source files — they're created when a coder agent
        # runs an unquoted shell command like
        #   pip install flask>=3.0,<4
        # and the shell parses `>=3.0,<4` as redirection + literal,
        # producing files like `=3.0,` or `<4` in the cwd. `git add -A`
        # then picks them up. Canonical 2026-05-27 calcv2 incident.
        _basename = normalized.rsplit("/", 1)[-1]
        if _basename and _basename[0] in "=<>|&":
            stripped.append((status_letter, normalized + "  [shell-artifact filename]"))
            continue
        if any(_fnmatch.fnmatchcase(normalized, deny) for deny in _CODER_DENYLIST):
            stripped.append((status_letter, normalized))
            continue
        staged.append(normalized)

    # Stage allowed paths. `git add -A -- <path>` handles M / A / D / R.
    for path in staged:
        _run(["git", "add", "-A", "--", path], timeout=30)

    if stripped:
        verbs = {
            "M": "modify", "D": "delete", "R": "rename",
            "C": "copy", "T": "change type of", "A": "add", "?": "add (untracked)",
        }
        log.warning(
            f"[post-coder] {product_name}: stripped {len(stripped)} "
            f"out-of-scope path(s) from commit (denylist; coder cannot "
            f"touch PM-curated files):"
        )
        for st, p in stripped:
            verb = verbs.get(st, st)
            log.warning(f"  - {verb} `{p}` -- in coder denylist")

    return (len(staged), [p for _, p in stripped])


# Directory names to skip when populating /workspace. .git is the largest
# (12MB+ for DocumentSign) and never referenced by Verify/test recipes;
# .pytest_cache and .coverage rebuild themselves; __pycache__ / .venv /
# node_modules are tooling caches the recipes never read. Excluding them
# brings the copy cost from ~21s (full repo) down to ~0.5s.
_WORKSPACE_COPY_EXCLUDE = frozenset({
    ".git", ".pytest_cache", ".coverage", "__pycache__",
    "node_modules", ".tox", ".venv", ".venv_review", "venv", "env",
})

# Path SEGMENTS that mark a file as vendored / tooling debris rather than
# first-party source. The lint guards must never judge these — a committed
# virtualenv (e.g. the review process's `.venv_review/`, whose name is not
# the standard `.venv`) once tripped Guard 2 by flagging pytest's own
# bundled `_pytest/*.py` as "skipped tests" and blocked feature #1799.
# Filtering at the `files` source protects every guard at once.
_VENDORED_PATH_SEGMENTS = frozenset({
    ".git", ".venv", ".venv_review", "venv", "env", "site-packages",
    "node_modules", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", "Temp", "Results",
})


def _is_vendored_path(path: str) -> bool:
    """True if any path segment is a vendored/tooling/scratch dir."""
    return any(seg in _VENDORED_PATH_SEGMENTS for seg in path.replace("\\", "/").split("/"))


@contextlib.contextmanager
def _workspace_symlink(working_dir: str, product_name: str = "?"):
    """Populate /workspace as a real-file MIRROR of working_dir for the
    duration of the wrapped block. (Name kept for back-compat with
    callsites; the strategy is now copy-farm, not symlink-farm — see Why.)

    Why: agent containers mount the product at /workspace, so designer
    Verify recipes (and the test code coders write following those recipes)
    routinely use absolute /workspace/... paths (`grep /workspace/src/...`,
    `with open("/workspace/...")`, etc). The post-coder pipeline runs
    inside the pf-orchestrator container where the product is mounted at
    /products/<name>/ — so /workspace/... resolves to a nonexistent path
    and every such test/recipe fails. Canonical 2026-05-31 cascade:
    feature 1147 (audit_log DDL) shipped a real consolidation but the
    test's `os.popen("grep ... /workspace/src/")` returned 0 matches and
    rapid_flap'd the feature; same pattern hit 1148/1149/1140/etc.

    History of strategies (failed → working):
      1. 2026-05-31 commit 5f31d80 — `os.symlink('/products/X', '/workspace')`.
         Failed: orchestrator runs as uid 999 (per bootstrap.sh re-exec)
         and cannot create entries at /. Inert; logged perm-denied every
         time.
      2. 2026-05-31 commit b90519a — bootstrap.sh pre-creates /workspace
         as orchestrator-owned dir; CM populates it with subdir symlinks
         (/workspace/src -> /products/X/src). Worked for direct path
         access (cat /workspace/src/db.py) but BROKE under `grep -r
         /workspace/ --include='*.py'`: GNU grep with -r does not follow
         symlinks found during recursion (only when given as command-line
         args). So recipes targeting the workspace root silently returned
         0 matches. Canonical 2026-06-01 fire: feature 1148 grep recipe.
      3. 2026-06-01 (this) — file-level mirror via shutil.copytree. The
         shadow tree is real directories + real files (not symlinks),
         so grep -r, find, python's os.walk, and pytest's collection all
         see it as if it were the original repo. ~0.5s for DocumentSign
         (2.3MB after excluding .git etc).

    Excluded from the copy: see _WORKSPACE_COPY_EXCLUDE — `.git`,
    `.pytest_cache`, `.coverage`, `__pycache__`, `node_modules`, `.tox`,
    `.venv`, `venv`, `env`. These are tooling state / dep caches that
    Verify recipes never reference and would multiply the copy size by
    10x+.

    Concurrency: the /workspace mirror is single-global. Safe under the
    orchestrator's per-product mutex (`tools.run_cycle` serializes
    post-coder pipelines product-by-product) and while only one product
    is in flight at a time. Multi-product concurrent post-coder pipelines
    would clash — flag for replacement with a chroot or per-process
    overlay mount if that's ever wanted.

    Idempotency: on entry we wipe /workspace's contents (a previous
    crashed pipeline may have left files). On exit we wipe again so the
    next product's mirror starts clean.

    Band-aid: proper fix is designer/coder prompts forbidding absolute
    /workspace/... paths in Verify recipes and test code. Tracked
    separately.
    """
    workspace = Path("/workspace")
    if not workspace.is_dir() or workspace.is_symlink():
        log.warning(
            f"[workspace-mirror] {product_name}: /workspace is not a "
            f"plain directory (is_symlink={workspace.is_symlink()}, "
            f"exists={workspace.exists()}). Check that bootstrap.sh ran "
            f"the workspace-mkdir step. Test/verify checks will run "
            f"without the band-aid."
        )
        yield
        return
    src_root = Path(working_dir)
    if not src_root.is_dir():
        log.warning(
            f"[workspace-mirror] {product_name}: source {src_root} is "
            f"not a directory; skipping band-aid"
        )
        yield
        return

    def _purge_workspace() -> None:
        try:
            for entry in workspace.iterdir():
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink()
                except OSError as e:
                    log.warning(
                        f"[workspace-mirror] {product_name}: could not "
                        f"remove {entry} during purge ({e}); continuing"
                    )
        except OSError as e:
            log.warning(
                f"[workspace-mirror] {product_name}: could not iterate "
                f"/workspace during purge ({e}); continuing"
            )

    _purge_workspace()
    copied = 0
    try:
        try:
            entries = list(src_root.iterdir())
        except OSError as e:
            log.warning(
                f"[workspace-mirror] {product_name}: cannot list "
                f"{src_root} ({e}); skipping band-aid"
            )
            yield
            return
        for entry in entries:
            if entry.name in _WORKSPACE_COPY_EXCLUDE:
                continue
            target = workspace / entry.name
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.copytree(
                        entry, target,
                        symlinks=True,
                        ignore_dangling_symlinks=True,
                        ignore=shutil.ignore_patterns(
                            *_WORKSPACE_COPY_EXCLUDE,
                        ),
                    )
                else:
                    shutil.copy2(entry, target, follow_symlinks=False)
                copied += 1
            except (OSError, shutil.Error) as e:
                log.warning(
                    f"[workspace-mirror] {product_name}: could not copy "
                    f"{entry.name} -> /workspace/{entry.name} ({e}); "
                    f"continuing"
                )
        if copied:
            log.info(
                f"[workspace-mirror] {product_name}: mirrored {copied} "
                f"top-level entries to /workspace from {working_dir} "
                f"(band-aid for absolute /workspace/... paths; "
                f"grep -r compatible)"
            )
        yield
    finally:
        _purge_workspace()


def _post_coder_lint_check(
    working_dir: str, _run, product_name: str = "?",
    debris_files_out: list[str] | None = None,
) -> list[str]:
    """
    Deterministic lint guards on the files in the most recent commit.

    Returns a list of human-readable violation strings. Empty = clean.

    Targets the rejection categories that dominate the reviewer's
    `changes_requested` outcomes per the 2026-05-07 audit (10/10 sampled
    rejections clustered in these categories):

      1. **Info disclosure** — raw `error.message` / `err.message` /
         `*.stack` returned in HTTP responses (4 of 10 sampled rejections)
      2. **Skipped/todo'd tests** — `.skip`, `.todo`, `xit(`, `xdescribe(`,
         `@unittest.skip` in changed test files (2 of 10 sampled)
      3. **Hardcoded credentials** — `api_key`, `password`, `secret`, `token`
         literally embedded in code (catches a class of security flags
         that the audit hasn't seen yet but is on the auditor's radar)

    Returns early on any tooling failure — the lint check is best-effort,
    not load-bearing. A failed grep should never block a feature.

    The `_run` callable is the same `subprocess.run` wrapper the post-coder
    pipeline uses (cwd=working_dir, capture_output, text). Reusing it
    keeps the timeout discipline + cwd consistent.
    """
    # Single `git show HEAD --name-status` for the whole lint check. The
    # guards below used to issue five separate `git show HEAD --name-only
    # --diff-filter=...` subprocesses (AM here, an MD probe, Guard 17's MD +
    # AMD, Guard 20's name-status) — all derivable from this one parse.
    # Status letters: A/M/D plus Rxxx/Cxxx for renames/copies; the
    # --diff-filter=AM/MD/AMD outputs exclude R/C, so the derived sets below
    # filter on exact letters to match byte-for-byte.
    try:
        ns_top_r = _run(["git", "show", "HEAD", "--name-status", "--pretty="],
                        timeout=20)
        if ns_top_r.returncode != 0:
            return []
        _ns_entries: list[tuple[str, str]] = []
        for _raw in (ns_top_r.stdout or "").splitlines():
            _parts = _raw.split("\t")
            if len(_parts) < 2:
                continue
            _ns_entries.append((_parts[0][:1], _parts[-1].strip()))
        files = [
            p for st, p in _ns_entries
            if st in ("A", "M")
            and p
            and not p.endswith("session_result.json")
            and not p.endswith("session_summary.md")
            and not p.startswith(".sprint-79")  # scaffold marker
            and not _is_vendored_path(p)        # never lint venv/site-packages debris
        ]
    except Exception:
        return []

    # Guard 17 (AST-diff deletion safety) also needs to run on deletion-only
    # commits, where `files` (AM-only) is empty but a `D` entry exists.
    _has_md = any(st in ("M", "D") for st, _p in _ns_entries)
    if not files and not _has_md:
        return []

    # Single `-U0` content diff for the whole lint check: ADDED lines per
    # file. Guard 4 (placeholder markers) and Guard 21 (CORS misconfig)
    # judge only what THIS commit wrote — scanning whole-file content
    # bounced bystander commits on pre-existing lines (canonical:
    # testingcalc #1418, four features bounced on a legacy `# placeholder`
    # none of them wrote).
    _added_by_file: dict[str, list[str]] = {}
    try:
        _u0_r = _run(["git", "show", "HEAD", "-U0", "--pretty="], timeout=30)
        if _u0_r.returncode == 0:
            _cur_file = None
            for _raw in (_u0_r.stdout or "").splitlines():
                if _raw.startswith("+++ b/"):
                    _cur_file = _raw[6:].strip()
                    continue
                if _raw.startswith("+++"):
                    _cur_file = None
                    continue
                if not _raw.startswith("+") or _raw.startswith("+++"):
                    continue
                if _cur_file is None:
                    continue
                _added_by_file.setdefault(_cur_file, []).append(_raw[1:])
    except Exception:
        _added_by_file = {}

    violations: list[str] = []

    # --- Guard 1: raw error.message in HTTP responses ---
    # Inverted predicate (2026-05-14): only flag a line if it BOTH references
    # error.message/stack AND contains an explicit HTTP-response marker.
    # Previous version (file-level grep, then line-level skip of recognized
    # log calls) over-fired on every legitimate try/catch in Next.js / Express
    # codebases — 17 lint-guard bounces on 11 features in 24h, all on the
    # same pattern. Real disclosure looks like `res.json({error: error.message})`
    # or `return Response.json({error: e.message})`; matching THAT directly
    # is both tighter and more accurate than enumerating every safe call
    # the agent might write.
    src_files = [
        f for f in files
        if any(f.startswith(p) for p in ("SRC/", "src/", "lib/", "app/", "pages/"))
        and any(f.endswith(ext) for ext in (".js", ".ts", ".jsx", ".tsx", ".py"))
    ]
    if src_files:
        try:
            r = _run(["grep", "-n", "-E",
                      r"(error|err)\.message|(error|err)\.stack"] + src_files,
                     timeout=15)
            if r.returncode == 0:
                raw_hits = [h for h in (r.stdout or "").splitlines() if h.strip()]
                # Lines that are themselves an HTTP response — Express/Next.js
                # Pages API, Fetch-style App Router, Fastify, Koa, Flask,
                # FastAPI. If error.message appears on a line with one of
                # these markers, that's the actual leak vector. Anything else
                # (logger calls, error chaining, throw new Error(...) wrapping,
                # variable assignment) is fine.
                _RESPONSE_MARKERS = (
                    # Express / Next.js Pages API
                    "res.send", "res.json", "res.status",
                    "res.write", "res.end",
                    # Fetch / Next.js App Router
                    "Response.json(", "new Response(",
                    "NextResponse.json(", "new NextResponse(",
                    # Fastify
                    "reply.send", "reply.code(",
                    # Koa
                    "ctx.body", "ctx.response",
                    # Python — Flask / FastAPI
                    "jsonify(", "JSONResponse(",
                    "raise HTTPException",
                )
                bad_files = set()
                for h in raw_hits:
                    # grep output: `filename:lineno:line_content`.
                    parts = h.split(":", 2)
                    if len(parts) < 3:
                        continue
                    fname, _lineno, line = parts
                    if not any(m in line for m in _RESPONSE_MARKERS):
                        continue  # not in an HTTP response — safe
                    bad_files.add(fname)
                if bad_files:
                    files_sample = sorted(bad_files)
                    sample = ", ".join(files_sample[:3])
                    more = "..." if len(files_sample) > 3 else ""
                    violations.append(
                        f"raw error.message/stack inside an HTTP response (info "
                        f"disclosure): {sample}{more}. Return a generic message "
                        f"to the client; log the raw error server-side."
                    )
        except Exception:
            pass  # grep failure is non-fatal

    # --- Guard 2: skipped/todo'd tests in changed test files ---
    test_files = [
        f for f in files
        if ("test" in f.lower() or "spec" in f.lower())
        and any(f.endswith(ext) for ext in (".js", ".ts", ".jsx", ".tsx", ".py"))
    ]
    if test_files:
        try:
            r = _run(["grep", "-l", "-E",
                      r"\.skip|\.todo|xit\(|xdescribe\(|@unittest\.skip"]
                     + test_files,
                     timeout=15)
            if r.returncode == 0:
                hits = [h for h in (r.stdout or "").splitlines() if h.strip()]
                if hits:
                    sample = ", ".join(hits[:3])
                    more = "..." if len(hits) > 3 else ""
                    violations.append(
                        f"skipped/todo'd tests in changed test files: "
                        f"{sample}{more}. Un-skip and make them pass, or "
                        f"delete them. Reviewers treat .skip as missing coverage."
                    )
        except Exception:
            pass

    # --- Guard 3: hardcoded credentials ---
    if src_files:
        try:
            r = _run(["grep", "-l", "-n", "-E",
                      r"""(api[_-]?key|password|secret|token)\s*[:=]\s*["'][^"']{4,}"""]
                     + src_files,
                     timeout=15)
            if r.returncode == 0:
                hits = [h for h in (r.stdout or "").splitlines() if h.strip()]
                if hits:
                    sample = ", ".join(hits[:3])
                    more = "..." if len(hits) > 3 else ""
                    violations.append(
                        f"hardcoded secret-looking literals in source: "
                        f"{sample}{more}. Move to env vars / secrets store."
                    )
        except Exception:
            pass

    # --- Guard 4: placeholder / stub / TODO in changed implementation files ---
    # Reviewer comments on feature #405 (Interactive Stock Chart Engine, 2026-05-14)
    # flagged that 5 indicator functions were "placeholders and not integrated."
    # Pattern across the day: coder declares done on scaffolding rather than
    # complete implementations. Catching that here, BEFORE the reviewer burns
    # a session, is much cheaper than letting it through.
    #
    # Detects: TODO/FIXME/XXX/HACK markers; explicit "not implemented" /
    # "placeholder" / "stub" strings inside thrown Errors or as comment markers;
    # raise NotImplementedError. Skips test files (Guard 2 owns those) and
    # legitimate JSX `placeholder="..."` attributes.
    # 2026-06-11 diff-scoping rewrite: the guard used to grep the ENTIRE
    # CONTENT of every changed implementation file, so a PRE-EXISTING
    # placeholder line anywhere in a file the commit merely touched bounced
    # the feature. Canonical: testingcalc #1418/#1465/#1466/#1467 — four
    # unrelated features (LaTeX output, async-DB infra, auth/health
    # refactors) all bounced on a legacy `# placeholder` in src/oauth2.py
    # that none of them wrote. The guard now scans only the commit's ADDED
    # lines (`git show -U0`), so it judges what THIS coder wrote — the
    # pre-existing debt is the drift-scanner's/architect's job, not a
    # reason to bounce bystanders. TODO/FIXME matching is also now
    # case-SENSITIVE (the marker convention is uppercase; case-insensitive
    # \btodo\b flagged every `todo.id` identifier in todo-app products).
    if src_files:
        impl_files = {f for f in src_files if "test" not in f.lower()}
        try:
            _MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
            _STUB_RE = re.compile(
                r"throw new Error\([\"'][^\"']*((not |un)?implemented|todo|placeholder|stub|coming soon)[^\"']*[\"']\)"
                r"|raise NotImplementedError"
                r"|NotImplementedError\(\)"
                r"|//\s*(placeholder|not implemented|stub)"
                r"|#\s*(placeholder|not implemented|stub)",
                re.IGNORECASE,
            )
            # Skip JSX/HTML `placeholder="..."` attributes (common UX
            # text, not a stub marker).
            _ATTR_SKIPS = (
                'placeholder="', "placeholder='",
                "placeholder={",
            )
            # Reuses the single top-of-check -U0 parse (_added_by_file).
            bad_files = set()
            for cur_file, added in _added_by_file.items():
                if cur_file not in impl_files:
                    continue
                for line in added:
                    if any(a in line for a in _ATTR_SKIPS):
                        continue
                    if _MARKER_RE.search(line) or _STUB_RE.search(line):
                        bad_files.add(cur_file)
                        break
            if bad_files:
                files_sample = sorted(bad_files)
                sample = ", ".join(files_sample[:3])
                more = "..." if len(files_sample) > 3 else ""
                violations.append(
                    f"placeholder / TODO / NotImplementedError in "
                    f"lines ADDED by this commit: {sample}{more}. Every "
                    f"acceptance criterion in the design doc must be "
                    f"backed by code that actually does the thing, "
                    f"not a stub or comment marker. Remove the "
                    f"markers and implement the real behavior."
                )
        except Exception:
            pass

    # ── Quality-spec Phase 2 additions (2026-05-19) ─────────────────────────
    # Eight new guards motivated by the StockAnalysis + Calculator code
    # reviews. Each is a tight regex + a deterministic verdict, scoped to
    # specific file classes to keep false-positive rate low. Failures here
    # are best-effort like the guards above; a broken regex never blocks a
    # feature, it just skips the check.

    import hashlib as _hashlib
    import re as _re

    # Content cache: Guards 5-11/13/18 each iterate the same changed-file
    # list — without this, a 15-file commit meant ~90 redundant disk reads
    # per lint pass. Safe because the commit is already made; the working
    # tree is static for the duration of the check.
    _read_cache: dict[str, str] = {}

    def _read(rel_path: str) -> str:
        cached = _read_cache.get(rel_path)
        if cached is not None:
            return cached
        try:
            from pathlib import Path as _P
            content = (_P(working_dir) / rel_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""
        _read_cache[rel_path] = content
        return content

    # --- Guard 5: hardcoded secret fallback in token/crypto calls ---
    # Calculator's SRC/main.py + StockAnalysis's tokenGenerator.js both shipped
    # `os.environ.get("JWT_SECRET", "fallback_secret_key_for_development")` —
    # tokens forgeable in any env missing the var. Flag any os.environ.get /
    # process.env-OR with a string fallback when the env name contains
    # SECRET / KEY / TOKEN.
    _PY_SECRET_FALLBACK = _re.compile(
        r'''os\.environ\.get\(\s*["'][^"']*(SECRET|KEY|TOKEN)[^"']*["']\s*,\s*["'][^"']{3,}["']''',
        _re.I,
    )
    _JS_SECRET_FALLBACK = _re.compile(
        r'''process\.env\.\w*(SECRET|KEY|TOKEN)\w*\s*(\|\||\?\?)\s*["'][^"']{3,}["']''',
        _re.I,
    )
    secret_hits = []
    for f in src_files:
        if f.startswith("tests/") or f.startswith("TestCases/"):
            continue
        content = _read(f)
        if not content:
            continue
        rgx = _PY_SECRET_FALLBACK if f.endswith(".py") else _JS_SECRET_FALLBACK
        m = rgx.search(content)
        if m:
            secret_hits.append(f"{f} ({m.group(0)[:80]}...)")
    if secret_hits:
        violations.append(
            f"hardcoded secret fallback in token/crypto calls: "
            f"{', '.join(secret_hits[:3])}{'...' if len(secret_hits) > 3 else ''}. "
            f"Replace with: if not os.environ.get('X'): return 503 — never "
            f"substitute a literal as the secret."
        )

    # --- Guard 5b: literal fallback inside secret-getter functions (AST) ---
    # DogTinder 2026-06-11 evasion of Guard 5's regex: instead of the two-arg
    # os.environ.get("KEY", "literal") form, the coder wrote
    #     key = os.environ.get("ENCRYPTION_KEY")
    #     if key is None:
    #         return b'a' * 32
    # — a hardcoded key by another shape. AST rule: a function whose name
    # mentions key/secret/token/passw that BOTH reads the environment AND
    # returns a non-empty str/bytes literal (incl. constant expressions like
    # b'a' * 32) is a secret fallback. Requiring the env read keeps FPs out:
    # get_token_type() returning "Bearer" never touches the environment.
    # Python-only for v1 (same precedent as Guard 17).
    _SECRET_FN_RE = _re.compile(r"key|secret|token|passw", _re.I)

    def _is_const_strbytes(node) -> bool:
        if isinstance(node, _ast.Constant) and isinstance(node.value, (str, bytes)):
            return len(node.value) >= 3
        if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Mult):
            # b'a' * 32 — repeated short literal is still a constant key
            def _sb(n):
                return isinstance(n, _ast.Constant) and isinstance(n.value, (str, bytes))
            def _i(n):
                return isinstance(n, _ast.Constant) and isinstance(n.value, int)
            return (_sb(node.left) and _i(node.right)) or (_sb(node.right) and _i(node.left))
        if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Add):
            return _is_const_strbytes(node.left) and _is_const_strbytes(node.right)
        return False

    def _reads_environ(fn_node) -> bool:
        for sub in _ast.walk(fn_node):
            if isinstance(sub, _ast.Attribute) and sub.attr == "environ":
                return True
            if isinstance(sub, _ast.Call):
                callee = sub.func
                if isinstance(callee, _ast.Attribute) and callee.attr == "getenv":
                    return True
                if isinstance(callee, _ast.Name) and callee.id == "getenv":
                    return True
        return False

    secret_fn_hits = []
    for f in src_files:
        if not f.endswith(".py") or f.startswith(("tests/", "TestCases/")):
            continue
        content = _read(f)
        if not content:
            continue
        try:
            tree5b = _ast.parse(content)
        except (SyntaxError, ValueError):
            continue
        for node in _ast.walk(tree5b):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if not _SECRET_FN_RE.search(node.name):
                continue
            if not _reads_environ(node):
                continue
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Return) and sub.value is not None \
                        and _is_const_strbytes(sub.value):
                    secret_fn_hits.append(f"{f}::{node.name}")
                    break
    if secret_fn_hits:
        violations.append(
            f"secret-getter function returns a literal fallback: "
            f"{', '.join(secret_fn_hits[:3])}{'...' if len(secret_fn_hits) > 3 else ''}. "
            f"A function that reads an env var for key/secret/token material "
            f"must FAIL CLOSED when the var is missing (raise / return an "
            f"error response) — never substitute a literal default key."
        )

    # --- Guard 6: state-changing API route without auth check ---
    # StockAnalysis's profile.js / alerts/trigger.js, Calculator's API_KEY-unset
    # bypass. Files under src/pages/api/ or app/api/ that handle POST/PUT/PATCH/
    # DELETE must call one of the recognized auth functions, or be annotated
    # with `// PUBLIC_ROUTE: <reason>` (or `# PUBLIC_ROUTE:` for Python) on
    # the first non-empty line.
    _AUTH_TOKENS = (
        "verifyAuth", "requireAuth", "withAuth", "verifyToken",
        "getServerSession", "requireSession", "getToken",
        "verify_auth", "require_auth", "current_user", "Depends(verify",
        "_require_api_key", "require_api_key",
    )
    _STATE_CHANGE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
    _METHOD_HINTS = (
        # JS/Express/Next
        "method ===", "method===", "method =", "req.method",
        "@router.post", "@router.put", "@router.patch", "@router.delete",
        "@app.post", "@app.put", "@app.patch", "@app.delete",
        "@app.route",
    )
    api_route_dirs = ("src/pages/api/", "app/api/", "pages/api/",
                      "SRC/routes/", "src/routes/", "SRC/main.py", "src/main.py")
    api_files = [f for f in src_files
                 if any(f.startswith(d) for d in api_route_dirs)]
    auth_missing = []
    for f in api_files:
        content = _read(f)
        if not content:
            continue
        first_non_empty = next((ln for ln in content.splitlines() if ln.strip()), "")
        if "PUBLIC_ROUTE:" in first_non_empty:
            continue
        # Does this file have a state-changing route at all?
        has_state_route = any(m in content for m in _METHOD_HINTS) and any(
            method in content for method in _STATE_CHANGE_METHODS
        )
        if not has_state_route:
            continue
        if any(tok in content for tok in _AUTH_TOKENS):
            continue
        auth_missing.append(f)
    if auth_missing:
        # Pick the comment syntax for the worked example based on the first
        # offending file's extension — `.py` files take `# PUBLIC_ROUTE:`,
        # everything else takes `// PUBLIC_ROUTE:`. The guard's own
        # recognizer at line 444 looks for the literal `PUBLIC_ROUTE:`
        # substring so both syntaxes pass; the example just has to match
        # the file the agent is editing or it gets ignored as not-applicable.
        _is_py = any(f.endswith(".py") for f in auth_missing)
        _ann = "# PUBLIC_ROUTE:" if _is_py else "// PUBLIC_ROUTE:"
        violations.append(
            f"state-changing route(s) without recognized auth check: "
            f"{', '.join(auth_missing[:3])}{'...' if len(auth_missing) > 3 else ''}.\n"
            f"\n"
            f"CORRECT FIX — pick ONE based on whether the endpoint should "
            f"require auth:\n"
            f"  (a) Endpoint genuinely needs no auth (public/anonymous "
            f"use, e.g. health probe, unauthenticated calculator). Add "
            f"this as the literal FIRST non-empty line of the file:\n"
            f"        {_ann} <one-sentence justification>\n"
            f"      Example: `{_ann} arithmetic endpoints have no user "
            f"state`.\n"
            f"  (b) Endpoint should require auth. Import and call a "
            f"recognized auth helper (verify_auth, require_api_key, "
            f"Depends(verify_...), etc.) at the start of each handler. "
            f"See ARCHITECTURE.md REFERENCE PATTERNS for the canonical "
            f"snippet on this stack.\n"
            f"\n"
            f"WRONG FIX (do NOT do this): change POST/PUT/PATCH/DELETE to "
            f"GET to dodge the check. The guard inspects the method names "
            f"but the architecture's auth contract still requires the "
            f"annotation; the reviewer will reject."
        )

    # --- Guard 7: ESM/CJS module-system mismatch ---
    # StockAnalysis had package.json type=module but several src/lib files
    # used `module.exports`. Inconsistent module style breaks Jest config
    # and produces import order dependencies.
    pkg_json = _read("package.json")
    if pkg_json and '"type": "module"' in pkg_json:
        cjs_re = _re.compile(r"^\s*(module\.exports\s*=|const\s+\w+\s*=\s*require\()", _re.M)
        cjs_hits = []
        for f in src_files:
            if not f.endswith((".js", ".mjs")):
                continue
            if "node_modules" in f:
                continue
            content = _read(f)
            if not content:
                continue
            m = cjs_re.search(content)
            if m:
                cjs_hits.append(f"{f}: {m.group(0).strip()[:50]}")
        if cjs_hits:
            violations.append(
                f"package.json declares type=module but file(s) use CommonJS: "
                f"{', '.join(cjs_hits[:3])}{'...' if len(cjs_hits) > 3 else ''}. "
                f"Convert to ESM (import/export) — mixed style breaks Jest "
                f"+ import-order behavior."
            )

    # --- Guard 8: byte-identical duplicate sibling ---
    # StockAnalysis's testcopy.js was an exact copy of register.js. Calculator
    # has multiple "_qa.py" pairs. Detect: for each new source file in this
    # commit, sha256 its content and walk the parent directory for an
    # existing file with the same hash. Same-content same-dir = copy-paste.
    dupe_hits = []
    for f in src_files:
        if not f.endswith((".js", ".jsx", ".ts", ".tsx", ".py")):
            continue
        content = _read(f)
        if not content or len(content) < 100:
            continue   # tiny files (one-liners, re-exports) are uninteresting
        h = _hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        try:
            from pathlib import Path as _P
            sib_dir = (_P(working_dir) / f).parent
            for sib in sib_dir.iterdir():
                rel_sib = sib.relative_to(_P(working_dir))
                if str(rel_sib).replace("\\", "/") == f:
                    continue
                if not sib.is_file():
                    continue
                if sib.stat().st_size != len(content.encode("utf-8", errors="replace")):
                    continue   # cheap pre-check, avoid hashing every neighbor
                sib_hash = _hashlib.sha256(sib.read_bytes()).hexdigest()
                if sib_hash == h:
                    dupe_hits.append(f"{f} ≡ {rel_sib}")
                    break
        except Exception:
            continue
    if dupe_hits:
        violations.append(
            f"byte-identical duplicate file(s): "
            f"{', '.join(dupe_hits[:3])}{'...' if len(dupe_hits) > 3 else ''}. "
            f"Delete one — almost certainly a copy-paste during rework. "
            f"If you genuinely need a parallel file, change at least one byte "
            f"and add a comment on the first line explaining why."
        )

    # --- Guard 9: bare except / catch swallowing all errors ---
    # Calculator's SRC/main.py had 5 bare `except Exception: pass` blocks
    # around ALTER TABLE statements. Intended for "column already exists" —
    # actually swallowed "out of disk", "connection lost", everything.
    _PY_BARE_EXCEPT = _re.compile(
        r"except(\s+(Exception|BaseException))?\s*(\s+as\s+\w+)?\s*:\s*\n\s+pass\b",
        _re.M,
    )
    _JS_EMPTY_CATCH = _re.compile(
        r"catch\s*(\([^)]*\))?\s*\{\s*(\}|//[^\n]*\n\s*\})",
        _re.M,
    )
    swallow_hits = []
    for f in src_files:
        if f.startswith(("tests/", "TestCases/")) or "test" in Path(f).name.lower():
            continue
        content = _read(f)
        if not content:
            continue
        rgx = _PY_BARE_EXCEPT if f.endswith(".py") else _JS_EMPTY_CATCH
        if rgx.search(content):
            swallow_hits.append(f)
    if swallow_hits:
        violations.append(
            f"bare except: pass / empty catch in non-test code: "
            f"{', '.join(swallow_hits[:3])}{'...' if len(swallow_hits) > 3 else ''}. "
            f"Catch the specific exception class you need; let others propagate "
            f"(or log + re-raise). Bare swallow hides 'out of disk', 'connection "
            f"lost' — the failures you most want to see."
        )

    # --- Guard 10: auth default-deny anti-pattern ---
    # Calculator's _require_api_key: `if expected_key and api_key != expected_key:
    # return 401`. When expected_key is falsy (env var unset), comparison is
    # skipped and any non-empty header passes. Classic "if X and X-condition:"
    # default-allow shape.
    _AUTH_DEFAULT_ALLOW = _re.compile(
        r"if\s+(\w+)\s+and\s+\w+\s*(!=|==)\s*\1\s*:",
        _re.M,
    )
    auth_default_hits = []
    for f in src_files:
        if not f.endswith(".py"):
            continue
        fname_l = f.lower()
        if not any(k in fname_l for k in ("auth", "verify", "token", "session", "key")):
            continue
        content = _read(f)
        if not content:
            continue
        if _AUTH_DEFAULT_ALLOW.search(content):
            auth_default_hits.append(f)
    if auth_default_hits:
        violations.append(
            f"auth check has default-allow shape ('if expected and key != expected'): "
            f"{', '.join(auth_default_hits[:3])}{'...' if len(auth_default_hits) > 3 else ''}. "
            f"Fix: `if not expected: return 503` BEFORE the comparison. Else any "
            f"request bypasses auth when the secret env var is unset."
        )

    # --- Guard 11: eval / exec / pickle.loads on user input ---
    # Calculator's safe AST evaluator allowed ast.Call + exposed pow/min/max.
    # `pow(2, 10**8)` is a memory bomb. Broader: any eval/exec/pickle.loads
    # in route or handler files is high-risk and needs explicit review.
    _DANGER = _re.compile(
        r"""\b(eval|exec|compile|pickle\.loads|os\.system|subprocess\.(call|run|Popen)\(.*shell\s*=\s*True)\(""",
    )
    danger_hits = []
    for f in src_files:
        if f.startswith(("tests/", "TestCases/")):
            continue
        if not f.endswith((".py", ".js", ".ts")):
            continue
        content = _read(f)
        if not content:
            continue
        for m in _DANGER.finditer(content):
            # narrow: only flag if file is in a routes / handlers / API dir
            if any(d in f for d in ("api/", "routes/", "handlers/", "main.py", "server.py")):
                danger_hits.append(f"{f} ({m.group(1)})")
                break
    if danger_hits:
        violations.append(
            f"eval/exec/pickle/shell-exec call in request-handling file: "
            f"{', '.join(danger_hits[:3])}{'...' if len(danger_hits) > 3 else ''}. "
            f"These are typically used to evaluate user input — review for "
            f"injection / DoS. Use ast.literal_eval, parametrized parsers, "
            f"or an explicit allow-list with size + recursion caps."
        )

    # --- Guard 13: agent-debris detector (Phase 3 of quality-specs) ---
    # Calculator accumulated: temp_fixed_top.py, src_head_end_correctly,
    # SRC/main.py.backup, SRC/main.py.bak.before_temperature, main_complete.py,
    # temp_storage/ (full product clone), test_X_qa.py pairs. Each was an
    # agent-debris artifact from rework cycles that no session cleaned up.
    # Refuse commits that introduce or modify files matching these patterns,
    # except when the file's first line is `# AGENT_DEBRIS_EXEMPT: <reason>`
    # (or the JS/TS comment variant) — the override is for legitimate
    # operator-committed backups before a risky migration.
    _DEBRIS_PATTERNS = [
        _re.compile(r"\.(bak|backup|orig)(\.|$)"),
        _re.compile(r"_temp[._]"),
        _re.compile(r"_old[._]"),
        _re.compile(r"_fixed[._]"),
        _re.compile(r"^temp_fixed"),
        _re.compile(r"^src_(head|tail)_"),
    ]
    # Patterns that ALSO match legitimate naming conventions — `api_v2.py`
    # (REST API versioning), `mark_complete.py` / `order_complete.ts`
    # (verb_noun handlers). 2026-06-09 five-product audit: because 13a is
    # the AUTO-RM category, an unconditional match doesn't just bounce a
    # legit file, it `git rm -f`s it out of the commit. These only count
    # as debris when a bare-stem sibling exists in the same directory
    # (`main.py` alongside `main_complete.py` = rework copy; `api_v2.py`
    # with no `api.py` = versioned module, pass through).
    _DEBRIS_SIBLING_PATTERNS = [
        _re.compile(r"_v[0-9]+[._]"),
        _re.compile(r"_complete[._]"),
    ]
    _SCRATCH_DIRS = ("Temp/", "temp/", "temp_storage/")
    _SOURCE_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb")
    # 13e suffix pattern — compiled once here, not per-file in the loop below.
    _SIBLING_SUFFIX_RE = _re.compile(
        r"^(.*?test_[A-Za-z0-9_]+)"
        r"_(qa|manager|class|cases|extra|additional|more|new|v2)"
        r"(\.[A-Za-z]+)$"
    )
    debris_hits = []
    qa_pair_hits = []
    from pathlib import Path as _PP
    for f in files:
        fname = _PP(f).name
        # 13a: name patterns
        debris_match = next((p.pattern for p in _DEBRIS_PATTERNS if p.search(fname)), None)
        if debris_match is None:
            # Sibling-required patterns: debris ONLY when the bare-stem
            # sibling exists (see _DEBRIS_SIBLING_PATTERNS comment).
            for p in _DEBRIS_SIBLING_PATTERNS:
                m_sib = p.search(fname)
                if not m_sib:
                    continue
                # api_v2.py → api.py ; main_complete.py → main.py
                sibling = fname[:m_sib.start()] + fname[m_sib.end() - 1:]
                try:
                    if sibling != fname and (
                        (_PP(working_dir) / f).parent / sibling
                    ).exists():
                        debris_match = f"{p.pattern} (sibling {sibling} exists)"
                        break
                except Exception:
                    pass
        if debris_match:
            # Honour the exemption marker on the first non-empty line
            first = next((ln for ln in _read(f).splitlines() if ln.strip()), "")
            if "AGENT_DEBRIS_EXEMPT" in first:
                continue
            debris_hits.append(f"{f} (matches {debris_match})")
            # Cycle IV (2026-06-02): expose the raw file path so the
            # caller can auto-rm and self-heal instead of bouncing the
            # whole feature. Only category 13a (filename antipattern)
            # is safe to auto-clean — the filename itself signals
            # debris regardless of content. Categories 13b/13c/13d may
            # contain content the agent meant to write, so they stay
            # hard-bounce-only.
            if debris_files_out is not None:
                debris_files_out.append(f)
            continue
        # 13b: tracked files in scratch directories the template marks
        # as never-committed
        if any(f.startswith(d) for d in _SCRATCH_DIRS):
            first = next((ln for ln in _read(f).splitlines() if ln.strip()), "")
            if "AGENT_DEBRIS_EXEMPT" in first:
                continue
            debris_hits.append(f"{f} (in scratch dir — template forbids commits here)")
            continue
        # 13f: test file at repo ROOT when a tests/ dir exists — agents'
        # scratch test scripts left behind from debugging (canonical:
        # DogTinder 2026-06-11, five zero-assertion test_*.py at root).
        # Hard-bounce-only, NOT auto-rm: the file may contain content the
        # agent meant to move into tests/.
        if "/" not in f and fname.startswith("test_") and fname.endswith(".py"):
            try:
                if (_PP(working_dir) / "tests").is_dir():
                    first = next((ln for ln in _read(f).splitlines() if ln.strip()), "")
                    if "AGENT_DEBRIS_EXEMPT" not in first:
                        debris_hits.append(
                            f"{f} (test file at repo root — move it into tests/)")
                        continue
            except Exception:
                pass
        # 13c: empty source file — but allow empty __init__.py (standard
        # Python package marker idiom). Surfaced by 2026-05-26 SmokeTest
        # smoke test: greenfield Python product was bounced for
        # `src/__init__.py`, `tests/__init__.py`, `migrations/__init__.py`,
        # all of which are intentional, standard, and required by Python.
        try:
            full = _PP(working_dir) / f
            if (any(f.endswith(ext) for ext in _SOURCE_EXTENSIONS)
                    and full.is_file() and full.stat().st_size == 0
                    and fname != "__init__.py"):
                debris_hits.append(f"{f} (empty source file)")
                continue
        except Exception:
            pass
        # 13d: source dir file with no extension at all (e.g. src_head_end_correctly)
        if "." not in fname and not f.endswith("/") and any(
            f.startswith(d) for d in ("SRC/", "src/", "lib/", "internal/", "app/", "pages/")
        ):
            try:
                if (_PP(working_dir) / f).is_file():
                    debris_hits.append(f"{f} (no extension in source dir)")
                    continue
            except Exception:
                pass
        # 13e: test_X_<suffix>.py sibling pair when test_X.py already exists.
        # Older form (_qa.py) was the canonical case; broadened 2026-05-26
        # after SmokeTest shipped both `tests/test_database.py` AND
        # `tests/test_database_manager.py` from two different coder sessions
        # — same `DatabaseManager` class tested in both, no review caught it.
        # The blocklist is a closed set of suffixes; legitimate per-method
        # splits (`_async`, `_sync`, `_unit`, `_integration`, `_e2e`) pass
        # through unchanged. (Pattern compiled once above the loop.)
        m = _SIBLING_SUFFIX_RE.match(fname)
        if m:
            base = m.group(1) + m.group(3)
            try:
                if ((_PP(working_dir) / f).parent / base).exists():
                    qa_pair_hits.append(
                        f"{f} (sibling of existing {base})"
                    )
            except Exception:
                pass
    if debris_hits:
        violations.append(
            f"agent-debris file(s) in commit: "
            f"{', '.join(debris_hits[:5])}{'...' if len(debris_hits) > 5 else ''}. "
            f"Calculator accumulated 12+ of these (main.py.backup, "
            f"src_head_end_correctly, temp_fixed_top.py, etc.) — each was "
            f"never cleaned up. Delete them and re-commit. If a backup is "
            f"intentional, annotate the first line with `# AGENT_DEBRIS_EXEMPT: "
            f"<reason>`."
        )
    if qa_pair_hits:
        violations.append(
            f"sibling test file pair(s) — test_X.py already exists: "
            f"{', '.join(qa_pair_hits[:3])}{'...' if len(qa_pair_hits) > 3 else ''}. "
            f"Don't create `test_X_qa.py` / `test_X_manager.py` / `test_X_cases.py` "
            f"alongside `test_X.py`. Merge the new assertions into the original "
            f"file and delete the sibling. (Per-method splits like `_async`, "
            f"`_sync`, `_unit`, `_integration`, `_e2e` are allowed.)"
        )

    # --- Guard 14: config-as-gate integrity (Phase 4 of quality-specs) ---
    # Calculator shipped `pytest.ini --cov-fail-under=0` despite the python
    # template default being 70. The agent edited the gate value down to
    # make tests pass. Reads the per-product quality_gates.json (installed
    # by templates/renderer.py at discovery) and verifies pytest.ini /
    # package.json / jest.config.cjs / go.mod values match.
    #
    # Skipped silently if quality_gates.json is absent (legacy products
    # discovered before this file shipped). Backfill via the architect
    # persona (Phase 8) for those.
    try:
        import json as _json
        import configparser as _cp
        gates_blob = _read("quality_gates.json")
        if gates_blob.strip():
            gates_doc = _json.loads(gates_blob)
            gates = (gates_doc or {}).get("gates") or {}
            gate_hits: list[str] = []

            def _parse_addopts(value: str) -> dict[str, str]:
                """Parse pytest.ini's `addopts = --x=1 --y=2 -v` into a flag→value dict."""
                out: dict[str, str] = {}
                if not value:
                    return out
                tokens = value.split()
                i = 0
                while i < len(tokens):
                    tok = tokens[i]
                    if "=" in tok and tok.startswith("--"):
                        k, v = tok.split("=", 1)
                        out[k] = v
                    elif tok.startswith("--") and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                        out[tok] = tokens[i + 1]
                        i += 1
                    i += 1
                return out

            # gate_file: e.g. "pytest.ini"
            for gate_file, gate_settings in gates.items():
                if not isinstance(gate_settings, dict):
                    continue  # null/None gates = "this stack doesn't enforce X"
                file_blob = _read(gate_file)
                if not file_blob:
                    # Required gate file is missing in working tree. Flag.
                    gate_hits.append(f"{gate_file} (file missing in working tree but required by quality_gates.json)")
                    continue
                # setting_key: e.g. "addopts.--cov-fail-under"
                for setting_key, rule in gate_settings.items():
                    if not isinstance(rule, dict):
                        continue
                    kind = rule.get("kind") or ""
                    # ---- pytest.ini --cov-fail-under integer ----
                    if kind == "ini_addopts_int":
                        try:
                            cp = _cp.ConfigParser()
                            cp.read_string(file_blob)
                            sec = None
                            for cand in ("pytest", "tool:pytest"):
                                if cp.has_section(cand):
                                    sec = cand
                                    break
                            if sec is None:
                                continue
                            addopts = cp.get(sec, "addopts", fallback="")
                            flag = setting_key.split(".", 1)[1]
                            current = _parse_addopts(addopts).get(flag)
                            if current is None:
                                gate_hits.append(f"{gate_file}: {flag} not set in addopts (required by quality_gates.json)")
                                continue
                            try:
                                if int(current) < int(rule.get("min", 0)):
                                    gate_hits.append(
                                        f"{gate_file}: {flag}={current} < required minimum "
                                        f"{rule.get('min')} ({rule.get('rationale','')})"
                                    )
                            except ValueError:
                                gate_hits.append(f"{gate_file}: {flag}={current!r} not an integer")
                        except Exception:
                            pass
                    # ---- pytest.ini --x string forbidden_patterns ----
                    elif kind == "ini_addopts_str":
                        try:
                            cp = _cp.ConfigParser()
                            cp.read_string(file_blob)
                            sec = next((s for s in ("pytest", "tool:pytest") if cp.has_section(s)), None)
                            if sec is None:
                                continue
                            addopts = cp.get(sec, "addopts", fallback="")
                            flag = setting_key.rsplit(".forbidden_values", 1)[0].split(".", 1)[1] \
                                   if ".forbidden_values" in setting_key \
                                   else setting_key.split(".", 1)[1]
                            current = _parse_addopts(addopts).get(flag, "")
                            for pat in rule.get("forbidden_patterns", []):
                                if _re.search(pat, current):
                                    gate_hits.append(
                                        f"{gate_file}: {flag}={current!r} matches forbidden "
                                        f"pattern {pat!r}"
                                    )
                                    break
                        except Exception:
                            pass
                    # ---- package.json string forbidden_patterns ----
                    elif kind == "json_string":
                        try:
                            doc = _json.loads(file_blob)
                            # navigate dotted path: scripts.test → doc["scripts"]["test"]
                            cur: object = doc
                            for part in setting_key.split("."):
                                if isinstance(cur, dict):
                                    cur = cur.get(part)
                                else:
                                    cur = None
                                    break
                            if not isinstance(cur, str):
                                continue
                            for pat in rule.get("forbidden_patterns", []):
                                if _re.search(pat, cur):
                                    gate_hits.append(
                                        f"{gate_file}: {setting_key}={cur!r} matches forbidden "
                                        f"pattern {pat!r} ({rule.get('rationale','')})"
                                    )
                                    break
                        except Exception:
                            pass
                    # ---- jest.config.cjs integer (regex extraction) ----
                    elif kind == "js_object_int":
                        # setting_key like "coverageThreshold.global.lines"
                        # Build a regex like: lines\s*:\s*(\d+) preceded by global: { ... lines:
                        try:
                            parts = setting_key.split(".")
                            # Walk the regex incrementally: each parent must appear before child
                            pattern = ""
                            for p in parts:
                                pattern += _re.escape(p) + r"\s*:\s*\{?[\s\S]*?"
                            # Last component should grab the integer
                            last = parts[-1]
                            value_pattern = (
                                r"\b" + _re.escape(last) + r"\s*:\s*(\d+)"
                            )
                            # Use the last-component pattern (simpler than threading all parents):
                            m = _re.search(value_pattern, file_blob)
                            if not m:
                                continue
                            current_int = int(m.group(1))
                            if current_int < int(rule.get("min", 0)):
                                gate_hits.append(
                                    f"{gate_file}: {setting_key}={current_int} < required "
                                    f"minimum {rule.get('min')}"
                                )
                        except Exception:
                            pass
                    # ---- go.mod go-version ----
                    elif kind == "go_mod_version":
                        try:
                            m = _re.search(r"^go\s+(\d+\.\d+)", file_blob, _re.M)
                            if not m:
                                continue
                            cur_tuple = tuple(int(p) for p in m.group(1).split("."))
                            req_tuple = tuple(int(p) for p in str(rule.get("min", "0.0")).split("."))
                            if cur_tuple < req_tuple:
                                gate_hits.append(
                                    f"{gate_file}: go {m.group(1)} < required {rule.get('min')}"
                                )
                        except Exception:
                            pass
                    # else: unknown kind — silently skip (forward compat)

            if gate_hits:
                violations.append(
                    f"config-gate integrity violation(s): "
                    f"{'; '.join(gate_hits[:5])}{'...' if len(gate_hits) > 5 else ''}. "
                    f"Quality bars are encoded in quality_gates.json — do not edit "
                    f"pytest.ini / package.json / jest.config.cjs / go.mod to bypass. "
                    f"If you genuinely need to lower a bar, edit "
                    f"product.config.quality_gates_override via the PM website."
                )
    except Exception:
        pass  # any parser failure → skip check; never blocks push

    # --- Guard 12: DB connection lifecycle (heuristic, warning-level) ---
    # Calculator's SRC/main.py:782-802 raised after a metric bump without a
    # finally close. Detection is heuristic: file opens a connection, has a
    # `raise` somewhere, but no `with` block. False-positive prone, so the
    # message is phrased as a warning rather than a hard refusal.
    conn_leak_hits = []
    for f in src_files:
        if not f.endswith(".py"):
            continue
        if f.startswith(("tests/", "TestCases/")):
            continue
        content = _read(f)
        if not content:
            continue
        opens_conn = _re.search(
            r"(mysql\.connector\.connect|psycopg2\.connect|sqlite3\.connect|get_(db_)?connection\s*\()",
            content,
        )
        if not opens_conn:
            continue
        has_with = (
            _re.search(r"with\s+\w+(_connection|get_connection|_conn)\s*[(\[]", content)
            or "with get_db_connection" in content
            # Stdlib form: `with sqlite3.connect(...) as conn:` /
            # `with psycopg2.connect(...)` / `with mysql.connector.connect(...)`.
            # The original regex only matched helper names ending in
            # _connection/_conn, so the common stdlib pattern read as a leak.
            # Canonical 2026-05-28 calc3 #1022 incident: src/auth/users.py used
            # `with sqlite3.connect()` everywhere (no leak), but the heuristic
            # fired every cycle and the remedy hint pointed at a get_db_connection()
            # helper that didn't exist → unsatisfiable → infinite coder loop.
            or _re.search(r"with\s+[\w.]*connect\s*\(", content)
            # contextlib.closing(...) is also a valid lifecycle wrapper.
            or "with closing(" in content
        )
        has_raise = "raise" in content
        has_finally_close = _re.search(r"finally\s*:[^}]*?\.close\(\)", content)
        if has_raise and not has_with and not has_finally_close:
            conn_leak_hits.append(f)
    if conn_leak_hits:
        violations.append(
            f"potential DB connection leak (open + raise without `with` or "
            f"`finally: close`): {', '.join(conn_leak_hits[:3])}"
            f"{'...' if len(conn_leak_hits) > 3 else ''}. "
            f"Heuristic — verify; if false positive, wrap the connection in a "
            f"`with` block (e.g. `with sqlite3.connect(...) as conn:`) or close "
            f"it in a `finally:` clause."
        )

    # --- Guard 15: alembic-branch detection ---
    # When two coder sessions independently create migrations for the same or
    # related schema concerns, both can end up branching off the same
    # down_revision, producing parallel alembic heads that require a manual
    # `alembic merge heads` to reconcile. Real 2026-05-20 incident: MyDocusign
    # had session A ship a stub migration (`232cdbb426a1`, upgrade() pass)
    # and session B ship the real one (`232cdbb426a2`), both `Revises:
    # 20260520043729`. A third merge-heads migration (`dffefb102f96`) had to
    # be added later to reunite the chain.
    #
    # This guard scans alembic/versions/ on disk for any two files sharing a
    # single-parent down_revision, when a new migration file is being added
    # by this commit. Skips merge migrations (which legitimately have
    # tuple/sequence parents) because their down_revision doesn't match the
    # single-string pattern.
    from pathlib import Path as _PP
    new_alembic_files = [
        f for f in files
        if f.startswith("alembic/versions/") and f.endswith(".py")
    ]
    if new_alembic_files:
        versions_dir = _PP(working_dir) / "alembic" / "versions"
        revises_map: dict[str, list[str]] = {}
        if versions_dir.is_dir():
            # Match single-string `down_revision = 'X'` (with or without a
            # type annotation). Tuple/sequence values (merge migrations) and
            # `= None` (initial migrations) are intentionally skipped.
            _down_re = _re.compile(
                r"^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*[\"']([^\"']+)[\"']\s*$",
                _re.MULTILINE,
            )
            for migration_path in versions_dir.glob("*.py"):
                try:
                    content = migration_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except Exception:
                    continue
                m = _down_re.search(content)
                if m:
                    revises_map.setdefault(m.group(1), []).append(migration_path.name)
        new_alembic_basenames = {_PP(f).name for f in new_alembic_files}
        branch_hits = []
        for down_rev, migrations in revises_map.items():
            if len(migrations) > 1 and any(m in new_alembic_basenames for m in migrations):
                branch_hits.append(
                    f"`{down_rev}` ← {', '.join(sorted(migrations))}"
                )
        if branch_hits:
            violations.append(
                "alembic branch detected — multiple migrations share the same "
                "down_revision, which will require an `alembic merge heads` "
                "to reconcile: " + "; ".join(branch_hits) + ". "
                "Either rebase the new migration on the existing head, or "
                "delete the duplicate before commit. Canonical incident: "
                "MyDocusign 2026-05-20 #618 shipped a stub + real pair "
                "(`232cdbb426a1` empty, `232cdbb426a2` real), both "
                "branching from `20260520043729`."
            )

    # --- Guard 16: alembic revision-ID coherence ---
    # Per-migration `down_revision` must be one of:
    #   - None / null (the initial migration)
    #   - A single string matching another migration's `revision` field
    #   - A tuple/list (merge migration; not validated here, covered by 15)
    # Catches the case where a coder agent invents a down_revision value
    # (a filename slug, a feature-id alias, a typo) that doesn't actually
    # resolve to any migration on disk. The migration imports but
    # `alembic upgrade head` errors on the dangling reference.
    # Defensive guard -- no known incident yet, but the cost of a broken
    # migration chain is high (every fresh DB setup fails) and the check
    # is cheap (single regex per migration file).
    if new_alembic_files:
        # Re-use the versions_dir captured by Guard 15; for safety reconstruct.
        versions_dir = _PP(working_dir) / "alembic" / "versions"
        if versions_dir.is_dir():
            # Collect every existing `revision = '...'` value on disk.
            _rev_re = _re.compile(
                r"^revision(?:\s*:\s*[^=\n]+)?\s*=\s*[\"']([^\"']+)[\"']\s*$",
                _re.MULTILINE,
            )
            _down_re_g16 = _re.compile(
                r"^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*(.+?)\s*$",
                _re.MULTILINE,
            )
            known_revisions: set[str] = set()
            for migration_path in versions_dir.glob("*.py"):
                try:
                    content = migration_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except Exception:
                    continue
                rm = _rev_re.search(content)
                if rm:
                    known_revisions.add(rm.group(1))
            # For each NEW migration in this commit, check its down_revision.
            phantom_hits: list[str] = []
            new_alembic_paths = [_PP(working_dir) / f for f in new_alembic_files]
            for migration_path in new_alembic_paths:
                if not migration_path.is_file():
                    continue
                try:
                    content = migration_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except Exception:
                    continue
                dm = _down_re_g16.search(content)
                if not dm:
                    continue
                raw_down = dm.group(1).strip()
                # Skip None and tuple/list (merge migrations, covered by 15).
                if raw_down == "None" or raw_down.startswith(("(", "[")):
                    continue
                # Strip quotes if a bare string literal.
                if raw_down.startswith(("'", '"')) and raw_down.endswith(raw_down[0]):
                    value = raw_down[1:-1]
                else:
                    continue  # not a recognizable string literal
                if value not in known_revisions:
                    phantom_hits.append(
                        f"`{migration_path.name}` references "
                        f"`down_revision = '{value}'` but no migration on "
                        f"disk has `revision = '{value}'`"
                    )
            if phantom_hits:
                violations.append(
                    "alembic migration has phantom parent revision -- "
                    "down_revision doesn't match any existing migration's "
                    "revision field. The migration will fail to import or "
                    "fail `alembic upgrade head`: " + "; ".join(phantom_hits) +
                    ". Use the parent's `revision = 'XXXX'` value (the "
                    "alphanumeric ID), NOT its filename slug. Canonical "
                    "incident: MyDocusign 2026-05-20 "
                    "`20260520171100_add_signers_table_for_feature_612.py` "
                    "had `down_revision = 'add_template_model_for_feature_629'` "
                    "(parent's slug) instead of `'20260520171004'` (parent's "
                    "revision ID). Migration imported but never ran "
                    "end-to-end on a fresh DB."
                )

    # --- Guard 17: AST-diff deletion safety ---
    # Catch deletions of top-level Python defs/classes/module-level assignments
    # that other files in the repo still reference. AST-parse the pre- and
    # post-commit version of each modified/deleted .py file, take the set
    # difference of public top-level names, then word-grep surviving callers
    # in the rest of the tracked tree (excluding files co-modified in this
    # commit — those the coder is already aware of). Catches the
    # "deleted foo() but bar.py still calls foo()" mistake before tests run.
    #
    # Best-effort and Python-only for v1: HEAD~1 may not exist on first commit,
    # files may have syntax errors, products may not be Python — all soft fail.
    # JS/TS has no stdlib AST parser; a future iteration could use tree-sitter.
    # Whole-word grep has false positives on common names (`name`, `run`,
    # `get`) inside docstrings/strings/local vars; the error message names
    # the symbol + caller paths so the coder can verify in seconds.
    # Derived from the single top-of-check `git show --name-status` parse
    # (was two more `git show --diff-filter=MD/AMD` subprocesses).
    md_files = {p for st, p in _ns_entries
                if st in ("M", "D") and p.endswith(".py")}

    if md_files:
        all_changed = {p for st, p in _ns_entries
                       if st in ("A", "M", "D") and p} or set(md_files)

        def _top_level_names(src: str) -> set[str]:
            try:
                tree = _ast.parse(src)
            except (SyntaxError, ValueError):
                return set()
            names: set[str] = set()
            for node in tree.body:
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                     _ast.ClassDef)):
                    if not node.name.startswith("_"):
                        names.add(node.name)
                elif isinstance(node, _ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, _ast.Name) and not tgt.id.startswith("_"):
                            names.add(tgt.id)
            return names

        removed_symbols: list[tuple[str, str]] = []
        for path in md_files:
            try:
                old_r = _run(["git", "show", f"HEAD~1:{path}"], timeout=10)
                if old_r.returncode != 0:
                    continue  # path absent in HEAD~1 (e.g., first commit)
                new_r = _run(["git", "show", f"HEAD:{path}"], timeout=10)
                new_src = new_r.stdout if new_r.returncode == 0 else ""
            except Exception:
                continue
            for name in (_top_level_names(old_r.stdout)
                         - _top_level_names(new_src)):
                # Skip single-character symbol names — `d`, `c`, `x` etc. are
                # essentially never meaningful public API and the word-grep
                # `git grep -w <name>` produces overwhelming false positives
                # against any local variable in any tracked .py file.
                # Canonical 2026-06-02 cycle FZ DocumentSign #1075: coder
                # correctly deleted stray `test_ac3.py` (flagged by reviewer)
                # which had a module-level `d = tempfile.mkdtemp()`. Guard 17
                # flagged `d` as "still referenced in tests/test_*.py" and
                # bounced the rework. fix_attempts hit 5, feature cap-Blocked
                # despite the coder doing exactly what the reviewer asked.
                if len(name) < 2:
                    continue
                removed_symbols.append((path, name))

        if removed_symbols:
            # Single batched word-grep for ALL removed symbols (was one
            # `git grep` subprocess per symbol — 0.5-2s on deletion-heavy
            # commits). `-o` prints `path:match`, from which the per-symbol
            # file sets are rebuilt; `-w` keeps the same whole-word
            # semantics as the old per-symbol `-w` calls. Symbol names are
            # Python identifiers, so the joined alternation is regex-safe.
            _sym_names = sorted({n for _p, n in removed_symbols})
            hits_by_name: dict[str, set[str]] = {n: set() for n in _sym_names}
            try:
                g = _run(["git", "grep", "-o", "-w", "-E",
                          "(" + "|".join(_sym_names) + ")", "--", "*.py"],
                         timeout=30)
                if g.returncode == 0:
                    for _raw in (g.stdout or "").splitlines():
                        _hit_path, _, _hit_sym = _raw.partition(":")
                        _hit_sym = _hit_sym.strip()
                        if _hit_sym in hits_by_name and _hit_path.strip():
                            hits_by_name[_hit_sym].add(_hit_path.strip())
            except Exception:
                hits_by_name = {}  # grep failure → no dangling claims (soft fail)
            dangling: list[str] = []
            for path, name in removed_symbols:
                surviving = [h for h in sorted(hits_by_name.get(name, ()))
                             if h not in all_changed]
                if surviving:
                    sample = ", ".join(surviving[:3])
                    more = " ..." if len(surviving) > 3 else ""
                    dangling.append(
                        f"`{name}` (removed from {path}) still referenced in "
                        f"{sample}{more}"
                    )
            if dangling:
                violations.append(
                    "removed top-level symbol still referenced elsewhere "
                    "(deletion-safety): " + "; ".join(dangling[:5])
                    + (" ..." if len(dangling) > 5 else "")
                    + ". Restore the symbol or update all surviving callers."
                    " Before pushing the rework, verify locally with:"
                    " `python check_deletion_safety.py` (exits 0 when clean)."
                )

    # --- Guard 18: deps coherence (imports vs requirements.txt) ---
    # Catch the MyDocusign-2026-05-22 failure mode: the coder's container
    # has fastapi/celery/passlib/etc. pre-installed in the agent image, so
    # `python -m pytest` succeeds inside the container even though
    # requirements.txt is missing those declarations. The reviewer (and
    # anyone outside the agent image) then sees ModuleNotFoundError on
    # collect, and the project ships broken.
    #
    # AST-walk changed .py files, extract top-level package names from
    # `import X` / `from X.Y import Z` statements, filter out:
    #   - stdlib modules (sys.stdlib_module_names — Python 3.10+)
    #   - first-party imports (top-level dir in working_dir)
    #   - well-known import→pypi name mappings (yaml→pyyaml, jwt→PyJWT)
    # Then diff against the union of declared deps in requirements.txt +
    # requirements-dev.txt. Any leftover = undeclared package.
    #
    # Python-only (skips silently when no requirements.txt is present —
    # Node/Go products handled by their own future guards). Diff-based: a
    # pre-existing undeclared import in an unchanged file is NOT flagged,
    # since the agent didn't introduce it. The first time the agent edits
    # such a file, the guard fires — a deliberately incremental cleanup.
    _PYPI_NAME_MAP = {
        # import name → pypi distribution name
        "yaml":         "pyyaml",
        "jwt":          "PyJWT",
        "jose":         "python-jose",  # cycle: MyJira 2026-06-03 #1334
        "cv2":          "opencv-python",
        "PIL":          "Pillow",
        "sklearn":      "scikit-learn",
        "bs4":          "beautifulsoup4",
        "dateutil":     "python-dateutil",
        "dotenv":       "python-dotenv",
        "magic":        "python-magic",
        "MySQLdb":      "mysqlclient",
        "google":       "google-cloud-storage",  # best-effort; google.* is huge
        "OpenSSL":      "pyOpenSSL",
        "Crypto":       "pycryptodome",
        "serial":       "pyserial",
        "Levenshtein":  "python-Levenshtein",
    }
    try:
        from pathlib import Path as _PPath
        _wd_path = _PPath(working_dir)
        _req_files = ("requirements.txt", "requirements-dev.txt")
        _req_present = [f for f in _req_files if (_wd_path / f).exists()]
        # Skip silently for non-Python projects (no requirements.txt at all).
        if _req_present:
            import sys as _sys
            # stdlib detection: Python 3.10+ ships sys.stdlib_module_names.
            _stdlib = set(getattr(_sys, "stdlib_module_names", ()))
            # First-party: any top-level dir in working_dir that's not a
            # virtualenv/build/scratch dir.
            _skip_dirs = {".git", ".venv", ".venv_review", "venv", "env",
                          "__pycache__", "node_modules", "Temp", "Results",
                          "dist", "build", ".pytest_cache", ".mypy_cache"}
            try:
                _first_party = {
                    p.name for p in _wd_path.iterdir()
                    if p.is_dir() and p.name not in _skip_dirs
                    and not p.name.startswith(".")
                }
            except Exception:
                _first_party = set()

            def _canon(name: str) -> str:
                # PEP 503: lowercase, collapse runs of [-_.] to single '-'.
                return _re.sub(r"[-_.]+", "-", name.lower())

            # Parse declared deps from requirements files. Strip extras
            # ([bcrypt]), version specs, env markers, comments. Best-effort.
            _declared: set[str] = set()
            for rf in _req_present:
                try:
                    for raw in (_wd_path / rf).read_text(
                            encoding="utf-8", errors="replace").splitlines():
                        line = raw.split("#", 1)[0].strip()
                        if not line or line.startswith("-"):
                            continue  # pip flags like -r foo.txt, -e .
                        m = _re.match(r"^([A-Za-z0-9_.\-]+)", line)
                        if m:
                            _declared.add(_canon(m.group(1)))
                except Exception:
                    continue

            # Imports collected from changed .py files in this commit (AM).
            _imported: dict[str, str] = {}  # canon-pypi → first source file
            for f in files:
                if not f.endswith(".py"):
                    continue
                src = _read(f)  # shared content cache (Guards 5-11 read these too)
                if not src:
                    continue
                try:
                    tree = _ast.parse(src)
                except (SyntaxError, ValueError):
                    continue
                for node in _ast.walk(tree):
                    top = None
                    if isinstance(node, _ast.Import):
                        for alias in node.names:
                            top = alias.name.split(".", 1)[0]
                            if not top or top.startswith("_") or top in _stdlib \
                                    or top in _first_party or top == "__future__":
                                continue
                            pypi = _canon(_PYPI_NAME_MAP.get(top, top))
                            _imported.setdefault(pypi, f)
                    elif isinstance(node, _ast.ImportFrom) and node.level == 0:
                        # node.level > 0 is a relative import (always first-party)
                        if not node.module:
                            continue
                        top = node.module.split(".", 1)[0]
                        if not top or top.startswith("_") or top in _stdlib \
                                or top in _first_party or top == "__future__":
                            continue
                        pypi = _canon(_PYPI_NAME_MAP.get(top, top))
                        _imported.setdefault(pypi, f)

            # Namespace packages: many PyPI distributions can install a single
            # import namespace. e.g. `opentelemetry-api`, `opentelemetry-sdk`,
            # `opentelemetry-instrumentation-fastapi`, `opentelemetry-exporter-
            # otlp-proto-grpc` all provide modules under the `opentelemetry`
            # namespace, but no single distribution is named `opentelemetry`.
            # Naive strict-equality (`opentelemetry` in _declared`) fires a
            # false positive when any hyphenated member is declared but the
            # bare prefix is not. Canonical 2026-06-01 DocumentSign #1101
            # cascade: requirements.txt declared all four opentelemetry-*
            # distributions, code did `from opentelemetry.trace import
            # get_tracer_provider`, Guard 18 reported `opentelemetry` missing
            # and bounced the feature; agent could not fix because
            # `pip install opentelemetry` is not a real package.
            # Conservative whitelist — extend only when a confirmed FP hits
            # a new namespace family.
            _NAMESPACE_PREFIXES = {
                "opentelemetry",  # otel-api/sdk/instrumentation-*/exporter-*
                "azure",          # azure-core/storage-*/identity/...
            }
            _missing = sorted(
                pkg for pkg in _imported
                if pkg not in _declared and not (
                    pkg in _NAMESPACE_PREFIXES
                    and any(d.startswith(pkg + "-") for d in _declared)
                )
            )
            if _missing:
                # Common Python "the import name is NOT the package name"
                # gotchas. When one of these surfaces as a "missing dep",
                # the fix is almost always to use the correct import,
                # NOT to add the wrong name to requirements.txt. Canonical
                # 2026-06-03 MyJira #1336/1337: agent wrote `import pyyaml`
                # (invalid — package PyYAML installs as `yaml` module),
                # Guard 18 reported `pyyaml` missing, agent added
                # `pyyaml` to requirements.txt, `pip install pyyaml` then
                # `import pyyaml` still failed at test time, test-env-
                # broken fired, rapid_flap eventually Blocked the
                # feature. The error message steers the agent to the
                # correct import rather than the wrong dep.
                _IMPORT_GOTCHAS = {
                    "pyyaml":             "use `import yaml` (the package PyYAML installs as the `yaml` module — `import pyyaml` is invalid Python)",
                    "python-jose":        "use `from jose import jwt` (the package python-jose installs as the `jose` module)",
                    "pyjwt":              "use `import jwt` (the package PyJWT installs as the `jwt` module)",
                    "opencv-python":      "use `import cv2` (the package opencv-python installs as the `cv2` module)",
                    "pillow":             "use `from PIL import Image` (the package Pillow installs as `PIL`)",
                    "scikit-learn":       "use `import sklearn` (the package scikit-learn installs as `sklearn`)",
                    "beautifulsoup4":     "use `from bs4 import BeautifulSoup` (the package beautifulsoup4 installs as `bs4`)",
                    "python-dateutil":    "use `import dateutil` (the package python-dateutil installs as `dateutil`)",
                    "python-dotenv":      "use `from dotenv import load_dotenv` (the package python-dotenv installs as `dotenv`)",
                    "python-magic":       "use `import magic` (the package python-magic installs as `magic`)",
                    "mysqlclient":        "use `import MySQLdb` (the package mysqlclient installs as `MySQLdb`)",
                    "pyopenssl":          "use `import OpenSSL` (the package pyOpenSSL installs as `OpenSSL`)",
                    "pycryptodome":       "use `from Crypto.Cipher import AES` (the package pycryptodome installs as `Crypto`)",
                    "pyserial":           "use `import serial` (the package pyserial installs as `serial`)",
                    "python-levenshtein": "use `import Levenshtein` (the package python-Levenshtein installs as `Levenshtein`)",
                }
                _gotchas: list[str] = []
                for pkg in _missing:
                    hint = _IMPORT_GOTCHAS.get(pkg)
                    if hint:
                        _gotchas.append(f"  - `{pkg}`: {hint}")
                sample = ", ".join(
                    f"`{pkg}` (used in {_imported[pkg]})" for pkg in _missing[:5]
                )
                more = f" ... ({len(_missing) - 5} more)" if len(_missing) > 5 else ""
                violation_msg = (
                    "imports use packages not declared in "
                    + " / ".join(_req_present) + " (deps-coherence): "
                    + sample + more
                    + ". The agent image masks this because it pre-installs "
                    "common Python libs; the reviewer and any fresh `pip "
                    "install -r requirements.txt && pytest` will fail.\n"
                )
                if _gotchas:
                    violation_msg += (
                        "\n"
                        "⚠️ DETECTED PYTHON IMPORT GOTCHA(S) — fix the IMPORT "
                        "in the source file, do NOT just add the wrong name to "
                        "requirements.txt:\n"
                        + "\n".join(_gotchas) + "\n"
                    )
                violation_msg += (
                    "\n"
                    "CORRECT FIX (when there is no import gotcha above): append "
                    "the missing package(s) to requirements.txt (or "
                    "requirements-dev.txt for test-only deps). Pin a version "
                    "range, e.g. `pytest>=8.0,<10`.\n"
                    "\n"
                    "WRONG FIX (do NOT do this): removing the import "
                    "from the source file, or deleting tests that "
                    "trigger the check. The check is purely static — "
                    "AST-walk imports vs requirements.txt. A passing "
                    "`pip install && pytest` locally does not satisfy "
                    "it; the violating import must remain AND the "
                    "package must appear in a requirements file."
                )
                violations.append(violation_msg)
    except Exception:
        log.debug("Guard 18 deps-coherence raised", exc_info=True)

    # --- Guard 19: doc-only commit (false-success commit detector) ---
    # Catches the "fake done" pattern where the coder ships a commit
    # containing ONLY documentation files (markdown/text) — no source code,
    # no tests — typically alongside a `final_verification.md` /
    # `implementation_summary.md` / `task_complete.md` file falsely
    # asserting the AC is satisfied. Canonical incident: 2026-06-01
    # cycle J — feature #1062 commit 501f2ec added only
    # `final_verification.md` claiming all ACs verified; reviewer caught
    # it but only after a full reviewer session. Reviewer's own words:
    # "ZERO implementation code. The only file added is final_verification.md
    # which falsely claims all ACs are verified. No src/lib/rate_limiter.py
    # was created, no modifications to src/auth/deps.py, no test file."
    #
    # Heuristic: classify each changed file as source/test/doc/config/other.
    # If every changed file is a doc AND there are zero source AND zero
    # test files, bounce as false-success. Allows pure doc updates
    # (designer/documenter persona; chore feature_type) by being narrow:
    # only fires when the coder pipeline is the running pipeline AND the
    # commit shape is unambiguously "claimed implementation, shipped doc."
    #
    # We DO NOT count `session_summary.md` / `session_result.json` as docs
    # for this check — they're orchestrator I/O channels, not the coder's
    # claimed deliverable. They're already filtered out of `files` at the
    # top of this function.
    try:
        # Doc extensions: markdown, plain text, RST. Specifically NOT
        # README.md (which can be a legitimate single-file change for a
        # docs chore) — the guard fires when ONLY doc files changed AND
        # at least one of them has a suspicious "completion-claim" name.
        _DOC_EXT = (".md", ".txt", ".rst", ".adoc")
        # Source-code extensions across the stacks this orchestrator
        # supports. Anything in this list counts as "real implementation"
        # and immunizes the commit from Guard 19.
        _SRC_EXT = (
            ".py", ".js", ".jsx", ".ts", ".tsx",
            ".mjs", ".cjs", ".go", ".rb", ".java",
            ".kt", ".swift", ".rs", ".c", ".cc", ".cpp",
            ".h", ".hpp", ".php", ".cs", ".sql", ".sh",
            ".yml", ".yaml", ".json", ".toml", ".ini",
            ".cfg", ".dockerfile", "Dockerfile", "Makefile",
        )
        # Filenames that strongly suggest a false-success completion claim.
        # Lowercased substring match against the filename (not path).
        _COMPLETION_CLAIM_PATTERNS = (
            "verification", "verify_complete",
            "implementation_summary", "implementation_complete",
            "task_complete", "task_done", "feature_complete",
            "summary_of_changes", "completion_report",
            "ac_verification", "final_report", "done_marker",
        )
        if files:
            from pathlib import Path as _PP_g19
            doc_files: list[str] = []
            src_files: list[str] = []
            other_files: list[str] = []
            for f in files:
                fl = f.lower()
                name = _PP_g19(f).name.lower()
                # Check src extensions first (some configs like .yml could
                # be either; we err on the side of "treat as source" so
                # legitimate config-only commits pass).
                if any(fl.endswith(e.lower()) for e in _SRC_EXT) or name in {
                    "dockerfile", "makefile",
                }:
                    src_files.append(f)
                elif any(fl.endswith(e) for e in _DOC_EXT):
                    doc_files.append(f)
                else:
                    other_files.append(f)
            # Fires iff: at least one doc, zero source, zero "other"
            # (the "other" check prevents weird edge cases — symlinks,
            # binary files — from accidentally satisfying the guard).
            # Additional safety: at least one doc filename must match a
            # completion-claim pattern. A coder genuinely fixing only a
            # README (no completion-claim name) passes through.
            if doc_files and not src_files and not other_files:
                claim_files = [
                    f for f in doc_files
                    if any(p in _PP_g19(f).name.lower()
                           for p in _COMPLETION_CLAIM_PATTERNS)
                ]
                if claim_files:
                    violations.append(
                        f"doc-only commit with false-success completion-"
                        f"claim file(s): {claim_files[0]!r} (and "
                        f"{len(doc_files) - 1} other doc(s) — no source or "
                        f"test changes). This commit shape is the canonical "
                        f"'fake done' pattern: ship a markdown asserting all "
                        f"ACs pass without actually changing any code. "
                        f"DELETE the completion-claim file and ship real "
                        f"implementation + tests for the AC. If the feature "
                        f"genuinely needs only doc changes, file it as a "
                        f"`chore` or `documenter` feature, not as a regular "
                        f"feature."
                    )
    except Exception:
        log.debug("Guard 19 doc-only-commit raised", exc_info=True)

    # --- Guard 20: net test-file deletion (all languages) ---
    # Guard 17's deletion safety is Python-AST-only and symbol-level; nothing
    # protected whole test FILES in other stacks. Canonical incident
    # (MyCalc1, 2026-06 audit): the third rework attempt on the scaffolding
    # feature wholesale-deleted `tests/Calculator.test.tsx` — classic
    # delete-the-test-to-pass-the-gate behavior, approved and merged.
    #
    # Rule: a commit that DELETES test file(s) while ADDING none is bounced.
    #   - D + A in the same commit (rename / split / replace) passes — the
    #     suite still has the coverage somewhere.
    #   - Deleting a test listed in ARCHITECTURE.md's DEPRECATED section
    #     passes — that's the sanctioned removal queue (e.g. the deprecated
    #     source-grep antipattern tests the architect queues for deletion).
    try:
        # Reuses the single top-of-check name-status parse (_ns_entries);
        # an empty parse no-ops the guard, same as the old returncode check.
        if _ns_entries:
            def _is_test_file(path: str) -> bool:
                pl = path.replace("\\", "/").lower()
                name = pl.rsplit("/", 1)[-1]
                in_test_dir = any(seg in ("tests", "test", "__tests__", "testcases")
                                  for seg in pl.split("/")[:-1])
                test_named = (
                    name.startswith("test_")
                    or name.endswith(("_test.py", "_test.go", "_test.rb",
                                      ".test.ts", ".test.tsx", ".test.js",
                                      ".test.jsx", ".spec.ts", ".spec.js",
                                      ".spec.tsx"))
                )
                code_ext = name.endswith((".py", ".js", ".jsx", ".ts", ".tsx",
                                          ".go", ".rb", ".java", ".rs"))
                return code_ext and (test_named or in_test_dir)

            deleted_tests: list[str] = []
            added_tests = 0
            for st, path in _ns_entries:
                if not _is_test_file(path):
                    continue
                if st == "D":
                    deleted_tests.append(path)
                elif st in ("A", "R", "C"):
                    added_tests += 1
            if deleted_tests and added_tests == 0:
                # DEPRECATED-list exemption: the architect queues sanctioned
                # test removals (e.g. source-grep antipattern tests) there.
                deprecated_blob = ""
                try:
                    arch_p = _PP(working_dir) / "ARCHITECTURE.md"
                    if arch_p.is_file():
                        txt = arch_p.read_text(encoding="utf-8", errors="replace")
                        m_dep = _re.search(
                            r"^##\s+DEPRECATED\b[^\n]*\n(.*?)(?=\n##\s|\Z)",
                            txt, _re.S | _re.M)
                        if m_dep:
                            deprecated_blob = m_dep.group(1)
                except Exception:
                    pass
                unsanctioned = [
                    p for p in deleted_tests
                    if p not in deprecated_blob
                    and p.rsplit("/", 1)[-1] not in deprecated_blob
                ]
                if unsanctioned:
                    violations.append(
                        f"test file(s) deleted with no replacement: "
                        f"{', '.join(unsanctioned[:4])}"
                        f"{'...' if len(unsanctioned) > 4 else ''}. "
                        f"Deleting tests is not a fix — the coverage they "
                        f"provided is gone and the gate they failed will "
                        f"pass vacuously. Restore the file and fix the code "
                        f"under test (or fix the test if IT is wrong). If "
                        f"the test is genuinely obsolete (e.g. a deprecated "
                        f"source-grep antipattern test), it must be listed "
                        f"in ARCHITECTURE.md's DEPRECATED section by the "
                        f"architect BEFORE a coder may delete it."
                    )
    except Exception:
        log.debug("Guard 20 test-deletion raised", exc_info=True)

    # --- Guard 21: CORS wildcard origin + credentials (added lines) ---
    # DogTinder 2026-06-11: `allow_origins=["*"]` together with
    # `allow_credentials=True` — the combination nullifies CORS for
    # authenticated requests (any site can make credentialed calls).
    # Judged on the commit's ADDED lines only (per-file), so pre-existing
    # misconfigs in a touched file don't bounce bystanders — the architect
    # owns legacy debt.
    try:
        _CORS_CRED_RE = _re.compile(
            r"allow_credentials\s*[:=]\s*True|credentials\s*:\s*true", _re.I)
        _CORS_WILD_RE = _re.compile(
            r"""allow_origins\s*[:=]\s*\[?\s*["']\*["']|origin[s]?\s*:\s*["']\*["']""",
            _re.I)
        _cors_hits = []
        for _cf, _added in _added_by_file.items():
            if not _cf.endswith((".py", ".js", ".jsx", ".ts", ".tsx")):
                continue
            _blob = "\n".join(_added)
            if _CORS_CRED_RE.search(_blob) and _CORS_WILD_RE.search(_blob):
                _cors_hits.append(_cf)
        if _cors_hits:
            violations.append(
                f"CORS misconfiguration — wildcard origins WITH credentials: "
                f"{', '.join(sorted(_cors_hits)[:3])}"
                f"{'...' if len(_cors_hits) > 3 else ''}. "
                f"`allow_origins=['*']` + `allow_credentials=True` lets any "
                f"website make authenticated requests. Either pin the origin "
                f"list (env-configurable) or drop allow_credentials."
            )
    except Exception:
        log.debug("Guard 21 CORS raised", exc_info=True)

    # --- Guard 22: zero-assertion test files (newly ADDED) ---
    # DogTinder 2026-06-11: five test_*.py committed with literally zero
    # assertions (one was a bare `print("Simple test")`) — debugging
    # scaffolds that pollute discovery and inflate the suite count without
    # testing anything. Scoped to files ADDED by this commit so legacy
    # suites don't bounce bystanders. Python: AST — any Assert node,
    # pytest.raises/fail, or self.assert* call counts. JS/TS: regex for
    # expect( / assert. First-line AGENT_DEBRIS_EXEMPT honoured (same
    # contract as Guard 13).
    try:
        def _is_testish(path: str) -> bool:
            name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
            return (
                name.startswith("test_")
                or name.endswith(("_test.py", ".test.ts", ".test.tsx",
                                  ".test.js", ".test.jsx", ".spec.ts",
                                  ".spec.js", ".spec.tsx"))
            )

        _hollow: list[str] = []
        for st, p in _ns_entries:
            if st != "A" or not _is_testish(p):
                continue
            content22 = _read(p)
            if not content22.strip():
                continue  # empty files are Guard 13c's call
            first22 = next((ln for ln in content22.splitlines() if ln.strip()), "")
            if "AGENT_DEBRIS_EXEMPT" in first22:
                continue
            if p.endswith(".py"):
                try:
                    tree22 = _ast.parse(content22)
                except (SyntaxError, ValueError):
                    continue
                has_assert = False
                for n22 in _ast.walk(tree22):
                    if isinstance(n22, _ast.Assert):
                        has_assert = True
                        break
                    if isinstance(n22, _ast.Call):
                        fn22 = n22.func
                        # pytest.raises / pytest.fail / self.assertEqual etc.
                        if isinstance(fn22, _ast.Attribute) and (
                                fn22.attr.startswith("assert")
                                or fn22.attr in ("raises", "fail")):
                            has_assert = True
                            break
                if not has_assert:
                    _hollow.append(p)
            else:
                if not _re.search(r"\b(expect|assert)\s*\(", content22):
                    _hollow.append(p)
        if _hollow:
            violations.append(
                f"test file(s) with ZERO assertions: "
                f"{', '.join(sorted(_hollow)[:4])}"
                f"{'...' if len(_hollow) > 4 else ''}. "
                f"A test that asserts nothing passes vacuously and inflates "
                f"the suite without testing anything. Add real assertions "
                f"on behavior, or delete the file if it was a debugging "
                f"scaffold."
            )
    except Exception:
        log.debug("Guard 22 zero-assert raised", exc_info=True)

    return violations


def _introduced_failures(feature_failed: list[str], baseline_failed: set[str]) -> list[str]:
    """Test ids that failed for the feature but NOT on the baseline.

    Pure + order-preserving over `feature_failed`. A test that fails on both
    the feature branch and origin/main is pre-existing (not this feature's
    fault); one that fails only on the feature branch was introduced by it.
    """
    return [t for t in feature_failed if t not in baseline_failed]


def _excerpt_pytest_tracebacks(
    run_out: str, max_blocks: int = 2, max_per_block: int = 500
) -> str:
    """
    Extract the first N pytest traceback blocks (from FAILURES + ERRORS
    sections) into a compact excerpt that's actually actionable for the
    coder agent.

    Why this exists: the bounce-back comment used to dump the first 800
    chars of raw pytest output, which on a multi-file failing suite is
    100% dot/F/E progress markers (`tests/test_x.py FFF...E....`) — zero
    signal about WHICH test or WHY. The detailed `=== FAILURES ===` /
    `=== ERRORS ===` sections sit AFTER the progress markers in pytest
    output and get truncated off. Agents see the bounce, can't tell what
    to fix, retry blindly, ping-pong to cap-Block.

    Canonical 2026-06-04 incident: MyJira features 1344, 1346, 1249 each
    bounced 3-4x on "First failure: \\`\\`" (empty backticks) with 800
    chars of dot-progress excerpt. Real failure was a single
    `assert hashed_password == password_hash` AssertionError that never
    made it into the bounce body.

    Output shape: per traceback block we keep
      - the test-name header line (`____ test_x.test_y ____`)
      - the TAIL of the block (last `max_per_block` chars) because the
        actual assertion / exception message is at the bottom of the
        traceback, with stack frames at the top.

    Returns "" when the input has no recognisable FAILURES/ERRORS
    sections, so callers can fall back to the legacy raw-excerpt path.
    """
    if not run_out:
        return ""
    # pytest section markers look like:
    #   =================================== FAILURES ===================================
    #   ==================================== ERRORS ====================================
    # Capture from either header until the next "===... <CAPS> ===..." section
    # marker (next is usually "short test summary info" or "warnings summary").
    section_re = re.compile(
        r"^=+\s+(FAILURES|ERRORS)\s+=+\s*$", re.M
    )
    end_re = re.compile(r"^=+\s+\S.*\s+=+\s*$", re.M)
    blocks: list[str] = []
    for m in section_re.finditer(run_out):
        start = m.end()
        nxt = end_re.search(run_out, start + 1)
        end = nxt.start() if nxt else len(run_out)
        section_body = run_out[start:end]
        # Split section into per-test traceback blocks. pytest uses
        # `____ test_name ____` (long underscores) as the per-test
        # divider. The first block before the first divider is usually
        # blank.
        divider_re = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}\s*$", re.M)
        last_pos = 0
        last_name = ""
        for dm in divider_re.finditer(section_body):
            if last_name:
                body = section_body[last_pos:dm.start()].strip()
                if body:
                    blocks.append(f"____ {last_name} ____\n{body[-max_per_block:]}")
                    if len(blocks) >= max_blocks:
                        break
            last_name = dm.group(1).strip()
            last_pos = dm.end()
        if len(blocks) < max_blocks and last_name:
            body = section_body[last_pos:].strip()
            if body:
                blocks.append(f"____ {last_name} ____\n{body[-max_per_block:]}")
        if len(blocks) >= max_blocks:
            break
    if not blocks:
        return ""
    # Also try to capture the short summary line(s) for these blocks if
    # present — they include the one-line "<test> - <exception type>:
    # <message>" form that's the most actionable single line.
    summary_lines: list[str] = []
    summary_re = re.compile(r"^(?:FAILED|ERROR)\s+\S.*", re.M)
    for line in summary_re.findall(run_out):
        summary_lines.append(line.strip())
        if len(summary_lines) >= max_blocks:
            break
    parts: list[str] = []
    if summary_lines:
        parts.append("\n".join(summary_lines))
    parts.extend(blocks)
    return "\n\n".join(parts)


def _baseline_pytest_failures(working_dir: str, test_ids: list[str],
                              timeout: int) -> set[str]:
    """Run `test_ids` against a throwaway git worktree at origin/main and return
    the subset that FAIL there (i.e. pre-existing failures).

    Uses `git worktree add --detach` so the live session branch / working tree
    is never touched. Runs in the orchestrator's existing Python env (deps were
    already pip-installed by the caller). `--continue-on-collection-errors` so a
    test FILE that only exists on the feature branch (new test) doesn't abort
    the baseline run — it simply won't appear in the baseline FAILED set, and is
    therefore correctly classified as introduced.

    Best-effort: ANY error → empty set, which makes the caller treat every
    failure as introduced (fails safe toward the old bounce-on-any behaviour).
    """
    import tempfile as _tf, shutil as _sh, os as _os, re as __re
    if not test_ids:
        return set()
    base_root = _tf.mkdtemp(prefix="pf-baseline-")
    wt = _os.path.join(base_root, "wt")
    try:
        add = _sp.run(["git", "worktree", "add", "--detach", wt, "origin/main"],
                      cwd=working_dir, capture_output=True, text=True, timeout=60)
        if add.returncode != 0:
            log.warning(f"[post-coder] baseline worktree add failed: "
                        f"{(add.stderr or '')[:200]}")
            return set()
        rr = _sp.run(["python", "-m", "pytest", "-q", "--no-header", "-s",
                      "-p", "no:cacheprovider", "--continue-on-collection-errors",
                      *test_ids],
                     cwd=wt, capture_output=True, text=True, timeout=timeout)
        out = (rr.stdout or "") + "\n" + (rr.stderr or "")
        return set(__re.findall(r"^FAILED (\S+)", out, __re.M))
    except Exception as e:
        log.warning(f"[post-coder] baseline failure-check raised: {e}")
        return set()
    finally:
        try:
            _sp.run(["git", "worktree", "remove", "--force", wt],
                    cwd=working_dir, capture_output=True, text=True, timeout=30)
        except Exception:
            pass
        _sh.rmtree(base_root, ignore_errors=True)


def _all_failing_tests(output: str) -> list[str]:
    """Parse the FULL set of currently-failing tests/files from a runner's
    output, runner-agnostically (pytest / vitest / jest / go). Used to WIDEN
    the rework feedback so the coder sees EVERY failing test, not just the
    first — preventing the fix-one-break-another thrash (canonical MyCalc1
    #1370: round 1 the tailwind tests fail; the coder fixes them but breaks
    Calculator.test.tsx → round 2; only the latest single failure was surfaced
    each round). Best-effort: returns [] when nothing parses, and the caller
    falls back to the first-failure + raw excerpt."""
    import re as _re
    if not output:
        return []
    clean = _re.sub(r"\x1b\[[0-9;]*m", "", output)   # strip ANSI colour codes
    found: list[str] = []
    seen: set = set()
    patterns = [
        r"^FAILED\s+(\S+)",                  # pytest
        r"^ERROR\s+(\S+)",                   # pytest collection error
        r"^\s*FAIL\s+(\S+\.\w+)",            # jest / vitest file-level FAIL
        r"❯\s+(\S+\.\w+)[^\n]*\bfailed\b",   # vitest file summary "❯ x.test.ts (.. | N failed)"
        r"❯\s+(\S+\.\w+)\s*\(0 test",        # vitest file collected 0 tests (error)
        r"---\s*FAIL:\s+(\S+)",              # go
        r"^\s*[×✗✕]\s+(.+\S)\s*$",           # vitest / jest individual failing test
    ]
    for pat in patterns:
        for m in _re.finditer(pat, clean, _re.M):
            name = m.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                found.append(name)
    return found[:25]   # cap to keep the comment / prompt bounded


def _agent_container_base(working_dir: str, service_env: dict[str, str] | None = None):
    """Shared `docker run` prefix + stack-install command for executing a shell
    script INSIDE the agent image (full toolchain: node/npm/npx, python/pip, go;
    plus the per-product download cache + workspace mount + sandbox hardening).

    Used by BOTH post-coder gates that EXECUTE product/stack code — the
    test-check and the verify-check. (Static-analysis checks — lint guards,
    drift-scanner, deletion-safety — stay in the orchestrator and do NOT use
    this: they only parse files + use git, which the orchestrator already has.)
    The principle: *run product/stack code in the toolchain container; analyze
    source statically in the control plane.*

    Returns (docker_prefix, install). docker_prefix is the argv up to and
    including `sh -lc`; the caller appends the final script string. install is
    the stack-appropriate dependency install ("" for unknown/no-dep stacks).
    """
    wd = Path(working_dir)
    img = os.environ.get("AGENT_IMAGE", "productfactory-agent")
    if (wd / "package.json").exists():
        install = "npm ci 2>/dev/null || npm install"
    elif (wd / "requirements.txt").exists():
        install = ("pip install --quiet --disable-pip-version-check --no-input "
                   "-r requirements.txt; "
                   "[ -f requirements-dev.txt ] && pip install --quiet "
                   "--disable-pip-version-check --no-input -r requirements-dev.txt; true")
    elif (wd / "go.mod").exists():
        install = "go mod download"
    else:
        install = ""

    # Per-product package-DOWNLOAD cache (pip wheels / npm tarballs / go mod
    # cache), persisted across runs so the clean install is fast WITHOUT
    # undermining the clean-room check — we cache the download cache, NOT the
    # installed deps. Bind-mount (not a named volume — fresh named volumes are
    # root-owned and the gate runs as non-root uid 1001); orchestrator (root)
    # pre-creates it mode-0777. Lives OUTSIDE the repo (sibling .pf-cache/) so
    # `git add -A` never stages it. Best-effort.
    cache_args = []
    try:
        cache_dir = wd.parent / ".pf-cache" / wd.name
        os.makedirs(cache_dir, exist_ok=True)
        os.chmod(cache_dir, 0o777)
        cache_args = [
            "-v", f"{host_path(str(cache_dir))}:/cache",
            "-e", "PIP_CACHE_DIR=/cache/pip",
            "-e", "npm_config_cache=/cache/npm",
            "-e", "GOMODCACHE=/cache/go",
            "-e", "GOCACHE=/cache/gobuild",
        ]
    except Exception:
        cache_args = []

    prefix = [
        "docker", "run", "--rm",
        "--network", "productfactory-net",
        "--add-host", "pm-api:host-gateway",
        "--memory", "4g", "--cpus", "2", "--pids-limit", "512",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        # CI=true → vitest/jest/most runners run once and exit (no watch hang).
        "-e", "CI=true",
        # Sidecar service URLs (REDIS_URL etc.) — same per-session service
        # containers the agent run used; teardown happens after finalize.
        *[a for k, v in (service_env or {}).items() for a in ("-e", f"{k}={v}")],
        *cache_args,                       # persistent per-product download cache
        "-v", f"{host_path(working_dir)}:/workspace",
        "-w", "/workspace",
        img, "sh", "-lc",
    ]
    return prefix, install


def _container_test_run(working_dir: str, service_env: dict[str, str] | None = None):
    """Return a `_run`-compatible callable that runs the stack's TEST command in
    the agent-image container (toolchain present), with a clean install
    prepended. Deterministic (no LLM) — verdict is the container exit code +
    stdout, classified by `_post_coder_test_check` unchanged. Fixed the
    orchestrator-has-no-npm bug behind the MyCalc1 #1370 cascade."""
    prefix, install = _agent_container_base(working_dir, service_env=service_env)

    def _run(cmd, **kw):
        timeout = kw.pop("timeout", 300)
        kw.pop("cwd", None)  # always /workspace inside the container
        inner = _shlex.join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        # Hard `timeout` backstop: a watch-mode runner (bare `vitest`/`jest`)
        # would otherwise hang the container forever (caught live on MyCalc1).
        guarded = f"timeout {timeout}s {inner}"
        full = f"{install} && {guarded}" if install else guarded
        return _sp.run(prefix + [full], capture_output=True, text=True, timeout=timeout + 120)

    return _run


def _detect_missing_tool(output: str) -> str | None:
    """Extract the CLI name from a missing-tool-shaped test failure
    (wave-5). Matches the same output shapes _ENV_BROKEN_PATTERNS already
    classifies (command-not-found, `'ruff' not on PATH` probe assertions) —
    this function only names the tool so the caller can route on the
    capability manifest: obtainable tool missing → env_broken (transient,
    retry); non-obtainable tool → terminal Block-for-redesign (the sandbox
    will NEVER provide it; iterating the coder against it produced the
    Docker-probe grind, DogTinder #1518/19/20). Pure function."""
    if not output:
        return None
    import re as _re2
    m = _re2.search(r"(?:sh|bash):\s+([\w.\-]+):\s+command not found", output)
    if not m:
        m = _re2.search(r"^([\w.\-]+): command not found", output, _re2.M)
    if not m:
        m = _re2.search(
            r"AssertionError:\s*['\"]?([\w.\-]+)['\"]?[^\n]*?(?:on PATH|in PATH|not installed|not available)",
            output)
    if not m:
        m = _re2.search(r"FileNotFoundError:.*No such file or directory: '([\w.\-]+)'", output)
    return m.group(1) if m else None


def _detect_missing_service(output: str) -> str | None:
    """Catalog-scoped missing-service detection (Phase C of service
    provisioning). Returns a SERVICE_CATALOG name when the test output
    shows a connection failure against that service's default port AND
    names the service — e.g. DogTinder #1582's
    `Could not connect to Redis at 127.0.0.1:6379: Connection refused`.

    Deliberately tight: a generic ECONNREFUSED on an arbitrary port stays
    a regular test failure (the app-under-test not starting IS the
    coder's bug); only failures attributable to a known provisionable
    service trigger the triage. Pure function — unit-tested directly.
    """
    if not output:
        return None
    low = output.lower()
    if "connection refused" not in low and "could not connect" not in low:
        return None
    try:
        from orchestrator.services import SERVICE_CATALOG
    except Exception:
        return None
    for svc, entry in SERVICE_CATALOG.items():
        if svc in low and f":{entry['port']}" in output:
            return svc
    return None


def _post_coder_test_check(working_dir: str, _run, product_name: str = "?",
                            timeout: int = 300) -> dict:
    """
    Phase 5 of quality-specs (2026-05-19): run the stack-specific test
    command and classify the outcome.

    Returns a dict:
      {"passed": bool, "env_broken": bool, "collection_errors": int,
       "framework": str, "output": str, "first_failure": str}

    Classification (in order):
      - framework=none: no recognized test config (pytest.ini / package.json /
        go.mod). passed=True (nothing to check). Caller skips the gate.
      - passed=True: exit 0 and no collection ERRORs.
      - env_broken=True: output matches a known infra-failure pattern
        (jest missing, node_modules absent, ENOENT). Caller should alert
        the operator but NOT bump fix_attempts — this isn't the coder's
        fault. Calculator's feature 594 cascade was driven by jest missing,
        which iterations of coder rework can't fix.
      - collection_errors > 0: pytest reported "ERROR" lines during
        collection (test files that fail to import). Calculator has ~100
        such test files referencing functions that don't exist in
        SRC/main.py. Treated as a real test failure but with a different
        bounce message.
      - else: real test failure. Caller bounces features to Implementing.

    Best-effort: any unexpected exception → passed=True (skip the gate).
    """
    from pathlib import Path as _PP
    import re as _re
    wd = _PP(working_dir)
    result = {
        "passed": True, "env_broken": False, "collection_errors": 0,
        "framework": "none", "output": "", "first_failure": "",
        "introduced_failures": [], "pre_existing_failures": [],
        "pre_existing_only": False,
    }
    # ---- detect framework from filesystem markers ----
    if (wd / "pytest.ini").exists() or (wd / "pyproject.toml").exists():
        framework = "pytest"
        # `-s` disables pytest's stdout/stderr capture. Required when the
        # workspace lives on a Windows-bind-mounted Docker volume: pytest's
        # capture cleanup calls `tmpfile.truncate()` on temp files in the
        # mounted dir, which races against the Docker virtiofs layer and
        # raises FileNotFoundError. The resulting partial cleanup makes
        # pytest report `collected 0 items` even when tests exist and run
        # fine. Canonical 2026-05-26 SmokeTest incident: feature #950 looped
        # for ~12 sessions with "zero tests collected" while pytest -s
        # actually collected 5 items and only failed coverage. Don't drop
        # `-s` here — the capture path is unsafe on Windows hosts.
        # Invoke pytest as a MODULE, never the bare `pytest` console-script.
        # The agent image bakes pytest in the pyenv site, but a product whose
        # requirements pin a different pytest version installs it into
        # ~/.local (pip auto-falls-back to --user when the pyenv site isn't
        # writable). That ~/.local pytest shadows the pyenv one on sys.path,
        # but the pyenv bin/pytest *shim* still imports a symbol from its own
        # version (`from _pytest.config import _console_main`) that the
        # ~/.local _pytest doesn't have → ImportError, pytest never starts,
        # and every test "fails". `python -m pytest` loads the package python
        # actually resolves (the coherent ~/.local set, plugins included) and
        # bypasses the version-mismatched shim entirely. Canonical
        # HomeChoreService #1662 (2026-06-19): the gate looped on
        # `cannot import name '_console_main'` until the diagnostician caught
        # it as env_impossible.
        collect_cmd = ["python", "-m", "pytest", "--collect-only", "-q", "-s"]
        run_cmd     = ["python", "-m", "pytest", "-q", "--no-header", "-s"]
    elif (wd / "package.json").exists():
        try:
            import json as _json
            pkg = _json.loads((wd / "package.json").read_text(encoding="utf-8"))
            scripts = (pkg.get("scripts") or {})
            if "test" not in scripts:
                return result
            test_script = scripts["test"] or ""
            # Trivial no-op scripts: don't try to "run" them, just flag as failure.
            if _re.match(r"^\s*(echo|exit\s+0|true|:)\b", test_script):
                result["passed"] = False
                result["first_failure"] = (
                    f"package.json scripts.test is a no-op: {test_script[:60]}"
                )
                return result
            framework = "npm"
            collect_cmd = None   # jest exposes --listTests, but not all node
                                 # projects use jest; skip the collection pass
            run_cmd     = ["npm", "test", "--silent"]
        except Exception:
            return result
    elif (wd / "go.mod").exists():
        framework = "go"
        collect_cmd = None
        run_cmd     = ["go", "test", "./..."]
    else:
        return result
    result["framework"] = framework

    # ---- env-broken patterns: these aren't code bugs the coder can fix ----
    _ENV_BROKEN_PATTERNS = [
        r"Cannot find module 'jest'",
        r"jest: command not found",
        r"sh:\s+\S+:\s+command not found",
        r"npm ERR! missing script",
        r"npm ERR!.*ENOENT",
        r"ENOENT: no such file or directory.*node_modules",
        r"go:.*missing go\.sum entry",
        r"can't load package: package",
        r"pytest: command not found",
        r"ModuleNotFoundError: No module named 'pytest'",
        # pytest env split: a product-pinned pytest in ~/.local shadows the
        # image's pyenv pytest, breaking the bin shim (`_console_main` import)
        # or a plugin load. Infra, not a code bug — the coder can't fix a
        # broken launcher. The `python -m pytest` switch above prevents this in
        # the test-check; these still catch verify-recipe / plugin-conflict
        # variants so they're env_broken (no fix_attempts bump, operator alert)
        # instead of a 4-bounce loop. Canonical HomeChoreService #1662.
        r"cannot import name '_?console_main' from '_pytest",
        r"cannot import name '\w+' from 'pytest' \(/[^)]*\.local/",
        # pytest reports "unrecognized arguments: --cov..." when a plugin in
        # the product's pytest.ini addopts isn't installed in the env running
        # the post-coder check (pytest-cov, pytest-xdist, etc.). That's a
        # missing-tool problem, not a code bug — coder shouldn't be punished.
        r"unrecognized arguments:\s*--(cov|xdist|benchmark|mock|django|sugar)",
        # Cycle DL (2026-06-01): the new cycle-CX designer prompt teaches
        # designers to write Verify recipes / tests that HARD-assert
        # runtime tool presence: `assert shutil.which('ruff'), 'ruff not
        # on PATH'`. That's the right shape (no skip-evasion) but it
        # produces an AssertionError when the tool genuinely isn't in
        # the agent image. Without this pattern, test-check classifies
        # it as a regular test failure and bounces — the coder cycles
        # through cap-route (5 attempts × ~8 min each = ~40 min) before
        # auto-Block.
        #
        # Canonical 2026-06-01 cascade: DocumentSign features 1177
        # (Dev tooling — ruff/pre-commit/git) and 1178 (Ruff lint check)
        # both burned ~40 min each on missing-runtime-tool with no
        # operator alert. Several more queued (1179, 1180, 1181,
        # 1182-1185 locust children) would have done the same.
        #
        # Matching the assertion message shape gives the immediate
        # operator-alert path (env_broken → no bump, rollback to
        # Designed/Approved, alert) on FIRST failure.
        r"AssertionError:\s*['\"]?[\w\-.]+['\"]?\s+(not\s+(on|in|installed))?\s*(on PATH|in PATH)",
        # Generic pytest assertion shape with the message text after the AC.
        r"AssertionError:.*not\s+on\s+PATH",
    ]
    _ENV_BROKEN_RE = _re.compile("|".join(_ENV_BROKEN_PATTERNS))

    try:
        # ---- install product deps so pytest can import them ----
        # Without this, the orchestrator container's site-packages only has
        # what was pre-baked at image-build time. Any product import of
        # flask/django/sqlalchemy/etc. then fails with ModuleNotFoundError
        # during pytest collection → 0 items collected → bounce, even when
        # the agent shipped clean code that passes locally.
        #
        # Canonical 2026-05-27 product-23 (CalcV2) incident: agent shipped a
        # correct Flask calculator (PUBLIC_ROUTE annotation, 5/5 tests pass
        # locally at 97% coverage). Post-coder's pytest ran in
        # pf-orchestrator which doesn't have flask, hit ImportError on
        # `from src.main import create_app`, reported 0 collected, bounced.
        # Same root cause behind every "zero tests collected" bounce we
        # debugged on products 17/18/20/21/22 — system was rejecting
        # correct code because it couldn't run the tests.
        #
        # Trade-off: this pollutes the orchestrator's global Python env
        # across product runs (operator-cleanup nuisance, not data loss).
        # Cleaner long-term: spawn an ephemeral container with the agent
        # image to run tests; deferred to a follow-up.
        if framework == "pytest":
            for req_name in ("requirements.txt", "requirements-dev.txt"):
                if not (wd / req_name).exists():
                    continue
                ri = _run(
                    ["pip", "install", "--quiet", "--disable-pip-version-check",
                     "--no-input", "-r", req_name],
                    timeout=min(180, timeout),
                )
                if ri.returncode != 0:
                    install_out = (ri.stdout or "") + "\n" + (ri.stderr or "")
                    # Benign pyenv-rehash false failure: pip installs the deps
                    # fine, but the pyenv `pip` shim's post-install `pyenv
                    # rehash` hook exits non-zero because /opt/pyenv/shims is
                    # root-owned and the container runs as the non-root `agent`
                    # user. The deps ARE installed (the collection check below
                    # catches anything genuinely missing), so this must NOT
                    # bounce. Without it, EVERY Python product whose deps ship
                    # console scripts (uvicorn/fastapi/alembic/...) false-bounced
                    # env_broken on a clean install — an infinite loop the coder
                    # can't fix (canonical: testingcalc #1423, surfaced 2026-06-08
                    # when the test-check moved into the agent container). The
                    # durable fix is making the shims dir agent-writable in the
                    # Dockerfile; this guard is belt-and-suspenders. Skip only
                    # when NO real pip error accompanies the rehash message.
                    if (("isn't writable" in install_out or "cannot rehash" in install_out)
                            and not _re.search(
                                r"ERROR:|No matching distribution|Could not find a version"
                                r"|ResolutionImpossible|Invalid requirement"
                                r"|conflicting dependencies|Failed building wheel",
                                install_out, _re.I)):
                        log.info(f"[post-coder] {product_name}: ignoring benign "
                                 f"pyenv-rehash exit on `pip install -r {req_name}` "
                                 f"(deps installed; not env_broken)")
                        continue
                    result["passed"] = False
                    result["output"] = (
                        f"pip install -r {req_name} failed "
                        f"(exit={ri.returncode}):\n{install_out[:2000]}"
                    )
                    # Tightened classification (2026-06-08): a pip install
                    # failure is the CODER's fault when requirements.txt declares
                    # a package pip cannot resolve (bogus name, nonexistent
                    # version, unsatisfiable constraint) — that's a real bug they
                    # must fix, so bounce with fix_attempts++ and a pointed
                    # message. Only genuine infra/transient failures (network,
                    # wheel-build toolchain, disk) stay env_broken (no bump,
                    # operator alert). Counting bad-dependency failures as
                    # env_broken let features OSCILLATE instead of climbing to a
                    # clean cap-Block (canonical: testingcalc #1423 — Guard 18
                    # told the coder to declare deps, it declared an
                    # unresolvable package, env_broken kept resetting it without
                    # bumping fix_attempts → flap loop → rapid_flap Block).
                    _bad_dep = _re.search(
                        r"No matching distribution found for"
                        r"|Could not find a version that satisfies the requirement"
                        r"|No matching distribution"
                        r"|Invalid requirement"
                        r"|is not a valid (?:requirement|editable requirement)"
                        r"|ResolutionImpossible"
                        r"|because these package versions have conflicting dependencies"
                        r"|ERROR: Could not find a version",
                        install_out, _re.I,
                    )
                    if _bad_dep:
                        result["env_broken"] = False   # coder's bug → real bounce
                        result["first_failure"] = (
                            f"requirements.txt declares a package pip cannot "
                            f"install — fix the package name/version: "
                            f"{_bad_dep.group(0)[:120]}"
                        )
                    else:
                        result["env_broken"] = True    # infra/transient → no bump
                        result["first_failure"] = (
                            f"pip install -r {req_name} failed (infra/transient)"
                        )
                    return result

        # ---- collection check (pytest only — surfaces import errors fast) ----
        if collect_cmd:
            r = _run(collect_cmd, timeout=min(60, timeout))
            collect_out = (r.stdout or "") + "\n" + (r.stderr or "")
            if _ENV_BROKEN_RE.search(collect_out):
                result["env_broken"] = True
                result["passed"] = False
                result["output"] = collect_out
                return result
            # pytest --collect-only reports collection errors as lines
            # starting with "ERROR " or "!!!"
            errs = [ln for ln in collect_out.splitlines()
                    if ln.startswith("ERROR ") or ln.startswith("!!!")]
            if errs:
                result["collection_errors"] = len(errs)
                result["passed"] = False
                result["first_failure"] = errs[0][:200]
                result["output"] = collect_out
                return result

        # ---- run actual tests ----
        r = _run(run_cmd, timeout=timeout)
        run_out = (r.stdout or "") + "\n" + (r.stderr or "")
        result["output"] = run_out
        if r.returncode == 0:
            result["passed"] = True
            return result
        # Non-zero exit: classify
        if _ENV_BROKEN_RE.search(run_out):
            result["env_broken"] = True
            result["passed"] = False
            return result
        # Real test failure. For pytest, separate failures the FEATURE
        # INTRODUCED from PRE-EXISTING failures on origin/main, so a single
        # broken test can't deadlock every feature. Canonical 2026-05-29 calc3:
        # a DDL-consolidation regression broke ~20 order-dependent tests, and
        # because post-coder runs the FULL suite, every subsequent feature
        # bounced on those unrelated tests → mass-Block. (npm/go keep the
        # bounce-on-any-failure behaviour; baseline-delta is pytest-only.)
        if framework == "pytest":
            failed_ids = _re.findall(r"^FAILED (\S+)", run_out, _re.M)
            result["failed_ids"] = failed_ids
            if failed_ids:
                baseline = _baseline_pytest_failures(working_dir, failed_ids, timeout)
                introduced = _introduced_failures(failed_ids, baseline)
                result["pre_existing_failures"] = sorted(baseline & set(failed_ids))
                result["introduced_failures"] = introduced
                if not introduced:
                    # Every failure pre-exists on origin/main → not this
                    # feature's fault. Let it proceed (don't bounce); the suite
                    # is broken independently and needs an operator/chore fix.
                    result["passed"] = True
                    result["pre_existing_only"] = True
                    result["first_failure"] = ""
                    log.warning(
                        f"[post-coder] {product_name}: {len(failed_ids)} test "
                        f"failure(s), ALL pre-existing on origin/main — suite is "
                        f"broken independent of this feature; not bouncing. "
                        f"Operator/chore must fix: "
                        f"{result['pre_existing_failures'][:5]}"
                    )
                    return result
                result["passed"] = False
                result["first_failure"] = f"FAILED {introduced[0]}"
                return result
        # non-pytest, or pytest with no parseable FAILED ids → bounce on any
        # failure. pytest: "FAILED tests/X::test_Y"; jest: "FAIL tests/X.test.js";
        # go: "--- FAIL: TestFoo".
        # Also match pytest's "ERROR tests/X::test_Y - <Exception>" short-summary
        # lines (setup-error case): the canonical 2026-06-04 MyJira 1344/1346/1249
        # cascade had ALL fixture-setup errors and no `FAILED` lines, so this
        # loop found nothing and `first_failure` rendered as empty backticks in
        # the bounce body — agent got zero signal about WHICH test failed.
        first = ""
        for ln in run_out.splitlines():
            if ln.startswith("FAILED ") or ln.startswith("FAIL ") \
                    or ln.startswith("ERROR ") \
                    or "--- FAIL:" in ln:
                first = ln[:200]
                break
        result["passed"] = False
        result["first_failure"] = first or "see test output below"
        return result
    except _sp.TimeoutExpired as e:
        # Cycle DX (2026-06-02): a pytest TIMEOUT is almost always the
        # coder's test hanging (e.g. spawning a long-running locust
        # process, infinite loop, network call to a missing endpoint),
        # NOT the test runner being missing. The previous catch-all
        # treated it as env_broken (no fix_attempts bump), so each
        # cycle re-ran the same hanging test and the feature looped
        # forever with no circuit-breaker.
        #
        # Canonical 2026-06-02 cycle DX: feature #1080 (Load testing
        # infrastructure) had the agent ship code that imports locust
        # and instantiates a Locust user class at import time. pytest
        # collection hung; subprocess.run timed out; previous code
        # classified env_broken with fix_attempts unchanged. Comment
        # 1879 was the only signal. Without a cap-route bump, the
        # supervisor's rapid_flap (10 status transitions/h) was the
        # only stop-gap — ~80 min wasted before triggering.
        #
        # Treat TimeoutExpired as a real test failure: bump
        # fix_attempts via the normal bounce path (Implementing+
        # changes_requested), cap-route catches at 5.
        result["passed"] = False
        result["env_broken"] = False
        result["first_failure"] = (
            f"pytest timed out after {e.timeout}s — test code hangs "
            "(common: locust/subprocess.Popen without timeout, infinite "
            "loop, network call to missing endpoint). Make the test "
            "complete in under the timeout, or mark the feature Blocked "
            "with a specific reason."
        )
        result["output"] = (
            f"_post_coder_test_check TimeoutExpired after {e.timeout}s: "
            f"{(e.cmd or 'pytest')!r}\n"
            f"stdout (truncated): {(e.stdout or b'')[:600]!r}\n"
            f"stderr (truncated): {(e.stderr or b'')[:600]!r}"
        )
        return result
    except Exception as e:
        # Other tooling failures (subprocess.SubprocessError variants
        # other than timeout, OSError, etc.) → still treat as env_broken.
        # These usually indicate a genuinely missing runner, broken
        # PATH, or similar infra issue the coder can't fix.
        result["env_broken"] = True
        result["passed"] = False
        result["output"] = f"_post_coder_test_check tooling error: {e}"
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Verify-check gate (2026-05-30): empirical AC-recipe runner
# ──────────────────────────────────────────────────────────────────────────────
#
# Reads each assigned feature's docs/story_<id>.md, extracts the per-AC
# `Verify:` bash commands and `Expected:` outputs that the post-2026-05-30
# designer prompt produces, runs each recipe in a subprocess, and compares
# the actual output to Expected. Bounces the feature on real mismatch.
#
# Motivation: the coder's `## AC<N> verification:` blocks in session_summary.md
# are coder-self-reported. Canonical 2026-05-30 #1119 incident: coder pasted
# `## AC1 verification: OK` despite the route being registered at
# `/admin/metrics` (not `/api/admin/metrics` as the Verify command targeted) —
# the curl would have 404'd if actually run. The system relied on the
# reviewer's eyeball reading of the diff to catch this; for subtler bugs
# (`time.strftime("%f")` family, where the routing is fine but the output is
# wrong) eyeball review will miss them.
#
# Server-required commands (curl http://localhost:...) currently get skipped
# with a "server unavailable" classification rather than bounced — the
# post-coder pipeline doesn't currently spin up the app. Pure-Python commands
# (TestClient-based, library-level) ARE enforced and will bounce on mismatch.
# Adding stack-specific server startup (uvicorn for FastAPI, npm for Node) is
# a follow-up. See `_run_verify` for the skip classification heuristic.
#
# Gated by env `POST_CODER_VERIFY_CHECK_ENABLED` (default off) so we can A/B
# this on DocumentSign before turning it on for all products. When the gate
# is off, the function is a no-op.


def _parse_ac_verifies(design_doc_text: str) -> list[tuple[int, str, str]]:
    """Parse (ac_number, verify_command, expected_output) triples from a story doc.

    Story-doc shape from the post-2026-05-30 designer prompt:

        AC1. [behavior]
             Verify: `<bash one-liner>`
             Expected: `<concrete output>`
             [Verify: `<another command>` Expected: `<another output>`]*
             Test: <test name>

    Multiple Verify+Expected pairs per AC are supported (the designer
    prompt allows this for ACs with multiple observable behaviors —
    success path + error path + edge case). Returns one triple per
    Verify recipe.

    Backticks around the command and around the expected output are
    stripped; surrounding whitespace is normalized. The command may span
    multiple lines inside a single pair of backticks (multi-line python
    -c is common for fixture-heavy ACs).
    """
    triples: list[tuple[int, str, str]] = []
    # Split by AC headers — `AC<N>.` at start of line.
    sections = re.split(r"^(AC\d+)\.\s", design_doc_text, flags=re.MULTILINE)
    # sections[0] is the preamble; then alternating ac_id, body, ac_id, body...
    for i in range(1, len(sections), 2):
        ac_id = sections[i]
        m_num = re.match(r"AC(\d+)", ac_id)
        if not m_num:
            continue
        ac_num = int(m_num.group(1))
        body = sections[i + 1] if i + 1 < len(sections) else ""
        # Find all Verify: `...` Expected: <line> pairs.
        # The command can be multi-line (between backticks); the expected
        # is single-line. Use DOTALL so the command capture spans newlines.
        for vm in re.finditer(
            r"Verify:\s*`(?P<cmd>(?:[^`]|`[^`])+?)`\s*\n\s*Expected:\s*(?P<exp>[^\n]+)",
            body, re.DOTALL,
        ):
            cmd = vm.group("cmd").strip()
            raw_exp = vm.group("exp").strip()
            # Designer prompt §AC quality calibration teaches Expected lines
            # in two shapes:
            #   `<concrete output>` (parenthetical narration about WHY).
            #   prints "OK" and exits 0.
            # When a backtick-quoted segment leads, THAT is the strict-match
            # target — the trailing parenthetical is human commentary the
            # downstream substring matcher must NOT include in its compare.
            # Failure mode this guards against: 2026-05-31 DocumentSign
            # #1145, #1146 (actual stdout byte-for-byte matched the quoted
            # target; matcher compared against quoted+parenthetical and
            # rejected), cascading into 18 rapid_flap blocks in ~12h.
            m_quoted = re.match(r"`((?:[^`]|`[^`])+?)`", raw_exp)
            if m_quoted:
                exp = m_quoted.group(1).strip()
            else:
                exp = raw_exp.strip("`").strip("'").strip('"').strip()
            triples.append((ac_num, cmd, exp))
    return triples


# Substring patterns in stderr that indicate the command needs a running
# server (curl couldn't connect). We skip these rather than bounce —
# bouncing would punish the coder for a missing server which is the
# post-coder pipeline's responsibility to provide, not the coder's.
_SERVER_UNAVAILABLE_MARKERS = (
    "Connection refused",
    "Couldn't connect to server",
    "Failed to connect to",
    "Could not resolve host",
)

# curl -s --silent suppresses stderr; curl -w '%{http_code}' on connection
# failure writes 000 to stdout. So stderr markers ALONE miss the
# DocumentSign 2026-05-30 cascade: every `curl -s -w '%{http_code}'
# http://localhost:8000/...` recipe ran, exited 7, emitted "000\n" to
# stdout and empty stderr → verify-check classified them as mismatches
# (output "000" != expected "200") and bounced every cycle. After 3-4
# rework rounds, supervisor.rapid_flap then Blocked the feature.
# Cascade affected 7 features (#1115, 1119, 1121, 1125, ...) before the
# gate was disabled.
#
# Fix: pre-detect server-required commands by parsing the command shape
# instead of relying on runtime stderr. Cover curl + python http libs
# against localhost / 127.0.0.1 / 0.0.0.0; whitelist TestClient (ASGI
# direct, no real server needed).
_SERVER_REQUIRED_CURL_RE = re.compile(
    r"\bcurl\b[^\n|;]*?https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b",
    re.IGNORECASE,
)
_SERVER_REQUIRED_PYHTTP_RE = re.compile(
    r"(requests|httpx|urllib).*https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)",
    re.IGNORECASE | re.DOTALL,
)
_TESTCLIENT_RE = re.compile(
    r"\b(TestClient|starlette\.testclient|fastapi\.testclient)\b",
)


def _is_server_required(cmd: str) -> bool:
    """True when the Verify command needs a running HTTP server.

    Pre-run detection so we don't have to scrape stderr for failure
    markers that curl -s suppresses. TestClient-based commands hit ASGI
    directly with no real server — those return False so they DO run
    and DO get enforced.
    """
    if _TESTCLIENT_RE.search(cmd):
        return False
    if _SERVER_REQUIRED_CURL_RE.search(cmd):
        return True
    if _SERVER_REQUIRED_PYHTTP_RE.search(cmd):
        return True
    return False


def _run_verify(cmd: str, cwd: str, timeout: int,
                service_env: dict[str, str] | None = None) -> dict:
    """Run a single Verify command in bash. Returns a dict with:
      exit_code: int (124 on timeout, subprocess returncode otherwise)
      stdout:    str
      stderr:    str (capped at 500 chars for log noise)
      skipped:   bool — True when the command needs a server (pre-detect)
                        OR connection failed at runtime (stderr marker).
      skip_reason: str ("server-required" / "server unavailable" / "")

    Best-effort: any subprocess exception is captured into a skipped
    result. The verify-check gate NEVER raises out of its caller — only
    logs and continues.
    """
    if _is_server_required(cmd):
        return {"exit_code": 0, "stdout": "", "stderr": "",
                "skipped": True,
                "skip_reason": "server-required (post-coder pipeline does "
                               "not start the app yet)"}
    try:
        # Run the recipe INSIDE the agent container (toolchain present), not
        # in-process in the orchestrator. The designer's Verify recipes invoke
        # stack tools (`npx tailwindcss`, `npx tsc`, `python -c "from app …"`)
        # that don't exist in the orchestrator — running them here was a false-
        # failure machine for every Node product (canonical: MyCalc1 #1370,
        # `bash: npx: command not found`). The container also has /workspace
        # natively, so this retires the verify-check's /workspace symlink
        # band-aid too. Install is silenced (node_modules is usually already
        # present from the test-check earlier in the same pipeline); the recipe
        # runs LAST so the container's exit code + stdout are the recipe's.
        prefix, install = _agent_container_base(cwd, service_env=service_env)
        script = ((f"{install} >/dev/null 2>&1; " if install else "")
                  + f"timeout {timeout}s bash -c {_shlex.quote(cmd)}")
        r = _sp.run(prefix + [script], capture_output=True, text=True,
                    timeout=timeout + 180)
        stderr = (r.stderr or "")[:500]
        if any(mk in stderr for mk in _SERVER_UNAVAILABLE_MARKERS):
            return {"exit_code": r.returncode, "stdout": r.stdout, "stderr": stderr,
                    "skipped": True,
                    "skip_reason": "server unavailable (connection failed at runtime)"}
        return {"exit_code": r.returncode, "stdout": r.stdout, "stderr": stderr,
                "skipped": False, "skip_reason": ""}
    except _sp.TimeoutExpired:
        return {"exit_code": 124, "stdout": "", "stderr": f"timed out after {timeout}s",
                "skipped": False, "skip_reason": ""}
    except FileNotFoundError as e:
        # `docker` not in PATH — skip rather than bounce.
        return {"exit_code": 127, "stdout": "", "stderr": f"container runtime unavailable: {e}",
                "skipped": True, "skip_reason": "container runtime unavailable"}


def _check_expected(actual_stdout: str, actual_exit: int, expected: str) -> bool:
    """Compare actual output to the designer's Expected string. Heuristic:

      - Expected text mentions "exit code 0" / "exits 0" / "returncode 0"
        / "exit 0" → check actual_exit == 0
      - Expected text mentions "exit code N" / "exits N" → check actual_exit == N
      - Otherwise → strip prefix words ("stdout exactly", "prints",
        "returns", "stdout", "output", "matches") and surrounding
        backticks/quotes, then look for the cleaned string as a substring
        of stdout. Substring (not equality) so trailing newlines / curl
        progress noise don't cause spurious failures.

    The coder ships the actual implementation; the heuristic is forgiving
    on the stdout-match path but strict on the exit-code path because the
    exit code is unambiguous.
    """
    exp_lower = expected.lower()
    m = re.search(r"\bexit(?:\s+code)?\s+(\d+)\b|\bexits?\s+(\d+)\b|\breturncode\s+(\d+)\b",
                  exp_lower)
    if m:
        target = int(m.group(1) or m.group(2) or m.group(3))
        return actual_exit == target
    cleaned = re.sub(
        r"^(stdout exactly|prints|returns|stdout|output|matches)\s+",
        "", expected, flags=re.IGNORECASE,
    ).strip().strip("`").strip("'").strip('"').strip()
    if not cleaned:
        # An Expected line with nothing parseable — treat as pass to avoid
        # spurious bounces. Designer is responsible for writing a real one.
        return True
    if cleaned in actual_stdout:
        return True
    # Prose-with-multiple-backticks fallback. Some designer-authored Expected
    # lines drift to "first `wc -l` outputs `1`, second outputs `1`" instead
    # of the canonical single-backtick shape `` `1\n1` (parenthetical) ``. The
    # prose form is unparseable for a substring match against stdout, but if
    # every non-empty stdout line EQUALS some backtick-quoted segment of the
    # Expected text, the work clearly satisfies the AC — pass. Equality
    # (not substring) prevents `"1"` from spuriously passing stdout `"100"`.
    # Stricter than the primary path because the prose form is ambiguous.
    # Designer prompt now forbids this shape; this is a back-compat net for
    # in-flight stories authored before the prompt update. Canonical fire:
    # 2026-06-01 DocumentSign #1147 (audit_log DDL grep recipe).
    if "`" in cleaned and actual_stdout.strip():
        segments = {s.strip() for s in re.findall(
            r"`((?:[^`]|`[^`])+?)`", cleaned)}
        stdout_lines = [ln.strip() for ln in actual_stdout.splitlines()
                        if ln.strip()]
        if segments and stdout_lines and all(
                ln in segments for ln in stdout_lines):
            return True
    return False


def _verify_actionable_hint(command: str, actual_stdout: str, actual_exit) -> str:
    """Turn a verify-check mismatch into ACTIONABLE feedback. Two structural
    aids that apply to every product / AC (added 2026-06-08):

    1. Surface the recipe's internal `assert` statements — that, not the bare
       `Expected: OK` sentinel, is the behavioral contract the coder must
       satisfy. Coders stalled because `expected: OK / actual: <trace>` never
       named what the recipe was actually checking.
    2. When an unhandled exception propagated out of the recipe WHILE it asserts
       an HTTP status code, name the specific fix: handle the error and RETURN
       the status, never re-raise. Canonical: testingcalc #1423 — a log-and-
       re-raise middleware vs AC3's `assert resp.status_code == 500`; the coder
       logged correctly but re-raised, so the exception propagated and the
       status assertion never ran.

    Returns text to append under the AC bullet ("" when nothing to add)."""
    import re as _r
    hints: list[str] = []
    asserts = _r.findall(r"^\s*assert\s+(.+)$", command or "", _r.M)
    if asserts:
        shown = "; ".join(a.strip().rstrip(",")[:100] for a in asserts[:4])
        hints.append(f"  ↳ AC requires (recipe asserts): `{shown}`")
    out = actual_stdout or ""
    status_assert = _r.search(r"status_code\s*==\s*(\d{3})", command or "")
    raised = ("Traceback (most recent call last)" in out
              or bool(_r.search(r"\b\w*(?:Error|Exception)\b:", out)))
    if status_assert and raised:
        code = status_assert.group(1)
        hints.append(
            f"  ↳ Your code RAISED instead of returning {code}. An error "
            f"handler / logging middleware must LOG **and RETURN a response** "
            f"(status {code}) — never `raise`/re-raise or let the exception "
            f"propagate, or the `status_code == {code}` assertion never runs."
        )
    return ("\n" + "\n".join(hints)) if hints else ""


def _post_coder_verify_check(
    working_dir: str,
    product_name: str,
    assigned_features: list[dict],
    timeout_per_command: int = 30,
    service_env: dict[str, str] | None = None,
) -> dict:
    """Empirical AC-recipe gate. Reads each assigned feature's
    docs/story_<id>.md, parses Verify+Expected pairs, runs each command,
    classifies success / mismatch / skip-server. Returns:

      {
        "checked":  bool,    # False = gate disabled or no recipes to check
        "passed":   bool,    # True = no mismatch failures (skips don't fail)
        "failures": list,    # one dict per command that ran AND mismatched
        "skipped":  list,    # commands skipped (server unavailable, etc.)
        "total":    int,     # commands attempted
      }

    The gate is OFF by default and turned on by setting the environment
    variable POST_CODER_VERIFY_CHECK_ENABLED to one of {1, true, yes, on}.
    Off → returns checked=False, passed=True (no-op).
    """
    if os.environ.get("POST_CODER_VERIFY_CHECK_ENABLED", "").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return {"checked": False, "passed": True, "failures": [],
                "skipped": [], "total": 0}
    failures: list[dict] = []
    skipped: list[dict] = []
    total = 0
    for feat in assigned_features:
        fid = feat.get("id")
        if not isinstance(fid, int):
            continue
        # Designer writes docs/story_<id>.md with zero-padding; tolerate
        # both the zero-padded and the legacy un-padded forms.
        doc_path = None
        for candidate in (f"story_{fid:03d}.md", f"story_{fid}.md"):
            p = Path(working_dir) / "docs" / candidate
            if p.is_file():
                doc_path = p
                break
        if doc_path is None:
            continue
        try:
            text = doc_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        triples = _parse_ac_verifies(text)
        if not triples:
            log.info(
                f"[verify-check] {product_name}: feature #{fid} has design doc "
                f"but no Verify recipes (legacy doc?) — skipping."
            )
            continue
        log.info(
            f"[verify-check] {product_name}: feature #{fid} — running "
            f"{len(triples)} Verify recipe(s)"
        )
        for ac_num, cmd, expected in triples:
            total += 1
            r = _run_verify(cmd, cwd=working_dir, timeout=timeout_per_command,
                            service_env=service_env)
            if r["skipped"]:
                skipped.append({
                    "feature_id": fid, "ac": ac_num,
                    "command": cmd[:200], "reason": r["skip_reason"],
                })
                continue
            if not _check_expected(r["stdout"], r["exit_code"], expected):
                failures.append({
                    "feature_id":    fid,
                    "ac":            ac_num,
                    "command":       cmd[:400],
                    "expected":      expected[:200],
                    "actual_stdout": (r["stdout"] or "")[:300],
                    "actual_stderr": (r["stderr"] or "")[:200],
                    "actual_exit":   r["exit_code"],
                })
    return {
        "checked":  True,
        "passed":   not failures,
        "failures": failures,
        "skipped":  skipped,
        "total":    total,
    }


def _run_post_coder_pipeline(product: dict, session_uid: str, working_dir: str,
                              assigned_features: list[dict]) -> list[int]:
    """
    Deterministic git ceremony after the coder LLM exits cleanly.
    The coder ONLY writes code; this function ships it (1-PR model,
    migration 043 — every coder session opens its own session PR):
      1. Detect if there are any changes in the workspace
      2. Rework mode: assigned features share an open session PR →
         force-push fresh commits to its branch (preserves the reviewer's
         comment thread). Otherwise cut a fresh ``coder/<session_uid>``
         branch off the default branch tip.
      3. Run the lint/test/verify gates, then git add + commit + push
         and open the session PR (``coder/<uid>`` → main)
      4. PATCH each assigned feature to Reviewing + pr_number on the PM API
         directly (no session_result.json roundtrip; see step 5 comment for
         why the file-based handoff was removed on 2026-05-06).

    Returns the list of feature IDs successfully PATCHed to Reviewing — used
    by the caller to set the session record's `features_pushed` counter.
    """
    pushed_ids: list[int] = []
    pname = product.get("name", "?")
    if not assigned_features:
        log.info(f"[post-coder] {pname}: no assigned features — skipping commit/PR")
        return pushed_ids

    # Path B: agent left files owned by uid 1001; orchestrator (different uid)
    # needs them writable to append to .git/logs/HEAD during checkout. The only
    # path that works on Windows bind-mounts is an alpine sidecar via the host
    # docker socket. See commit b301048 for why an in-container chmod fails.
    _chmod_workspace_via_alpine(working_dir, pname)

    # Path B: orchestrator owns ALL git ceremony. Agents no longer commit or
    # push (see prompts/{greenfield,brownfield}.md, refactored on this branch).
    # The legacy "agent already pushed" verification block below is dead in
    # normal flow but kept as defense-in-depth: if a stale-cached prompt or a
    # weak model reverts to the old behavior, we still skip the duplicate push.
    # The double check (pr_number > 0 + GitHub existence + [feature-N] commits)
    # catches agents that hallucinate PRs in session_result.json without
    # actually running git push — a failure mode observed with deepseek-v4-flash
    # which wrote pr_number=0 entries that the live-poll later rejected,
    # leaving features stuck Implementing while we'd already discarded the
    # local diff.
    try:
        sr_path = Path(working_dir) / "session_result.json"
        claimed: dict[int, int] = {}  # feature_id -> pr_number from session_result
        if sr_path.exists():
            for ln in sr_path.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    e = _json.loads(ln)
                except Exception:
                    continue
                pr_n = e.get("pr_number") if isinstance(e, dict) else None
                fid_e = e.get("id") if isinstance(e, dict) else None
                if (isinstance(e, dict) and e.get("status") == "Reviewing"
                        and isinstance(pr_n, int) and pr_n > 0
                        and isinstance(fid_e, int)):
                    claimed[fid_e] = pr_n

        # Verify each claimed PR exists on GitHub before trusting the agent.
        already_handled: set[int] = set()
        if claimed:
            gh_token_v = _get_gh_token()
            github_repo_v = product.get("github_repo", "")
            if gh_token_v and github_repo_v:
                repo_slug_v = _parse_repo_slug(github_repo_v)
                gh_headers_v = {
                    "Authorization": f"Bearer {gh_token_v}",
                    "Accept": "application/vnd.github+json",
                }
                # Cache PR-existence checks so duplicate pr_numbers across
                # features don't multiply API calls. A PR counts as "agent
                # actually shipped code" only when (a) the PR exists and is
                # open AND (b) the commit log contains at least one commit
                # whose message tags the feature `[feature-<id>]` — the
                # convention the coder prompt mandates. The PR-exists check
                # alone is too lax: in sprint-PR-mode the PR is opened by
                # provision_sprint_pr at sprint activation with a single
                # `chore(sprint-<id>): scaffold sprint branch` commit, so
                # any agent that hallucinates a Reviewing entry pointing at
                # the existing PR passes the open-PR check without ever
                # calling git push.
                pr_status: dict[int, dict] = {}  # pr_number -> {"open": bool, "feature_commits": set[int]}
                for fid_c, pr_n in claimed.items():
                    if pr_n not in pr_status:
                        info = {"open": False, "feature_commits": set()}
                        try:
                            r = httpx.get(
                                f"https://api.github.com/repos/{repo_slug_v}/pulls/{pr_n}",
                                headers=gh_headers_v, timeout=10,
                            )
                            info["open"] = (
                                r.status_code == 200
                                and isinstance(r.json(), dict)
                                and r.json().get("state") == "open"
                            )
                        except Exception:
                            pass
                        if info["open"]:
                            try:
                                cr = httpx.get(
                                    f"https://api.github.com/repos/{repo_slug_v}/pulls/{pr_n}/commits",
                                    headers=gh_headers_v,
                                    params={"per_page": 100},
                                    timeout=15,
                                )
                                if cr.status_code == 200:
                                    import re as _re
                                    tag_re = _re.compile(r"\[feature-(\d+)\]")
                                    for c in cr.json() or []:
                                        msg = (c.get("commit") or {}).get("message", "")
                                        for m in tag_re.finditer(msg):
                                            try:
                                                info["feature_commits"].add(int(m.group(1)))
                                            except ValueError:
                                                pass
                            except Exception:
                                pass
                        pr_status[pr_n] = info
                    info = pr_status[pr_n]
                    if info["open"] and fid_c in info["feature_commits"]:
                        already_handled.add(fid_c)
                    elif info["open"] and not info["feature_commits"]:
                        log.warning(
                            f"[post-coder] {pname}: agent claimed PR #{pr_n} for feature #{fid_c} "
                            f"but PR has no [feature-N] commits — agent never pushed; running fallback"
                        )
                    elif info["open"]:
                        log.warning(
                            f"[post-coder] {pname}: agent claimed PR #{pr_n} for feature #{fid_c} "
                            f"but PR has no commit tagged [feature-{fid_c}] (found tags: "
                            f"{sorted(info['feature_commits'])}) — running fallback"
                        )
                    else:
                        log.warning(
                            f"[post-coder] {pname}: agent claimed PR #{pr_n} for feature #{fid_c} "
                            f"but PR is not open on GitHub — running fallback for this feature"
                        )

        assigned_ids = {f["id"] for f in assigned_features}
        if assigned_ids and assigned_ids.issubset(already_handled):
            log.info(
                f"[post-coder] {pname}: agent opened verified PRs for all "
                f"{len(assigned_ids)} assigned features — skipping fallback pipeline"
            )
            return pushed_ids
        if already_handled & assigned_ids:
            # Partial coverage. Narrow `assigned_features` to the unhandled
            # subset so the no-diff Blocked-flip and the session_result.json
            # writeback don't demote features the agent already finished
            # (PATCHing them back to Blocked bypasses the rank guard in
            # _apply_session_entry).
            log.info(
                f"[post-coder] {pname}: agent handled {sorted(already_handled & assigned_ids)}; "
                f"running fallback for {sorted(assigned_ids - already_handled)}"
            )
            assigned_features = [f for f in assigned_features if f["id"] not in already_handled]
            if not assigned_features:
                # Filter consumed everything — handled features already
                # taken care of by _apply_session_entry, no fallback work
                # to do. (Defensive: the issubset check above should have
                # caught this, but rely on it here too in case the set
                # math races with a concurrent live-poll application.)
                log.info(f"[post-coder] {pname}: all features handled by agent — skipping fallback pipeline")
                return pushed_ids
    except Exception:
        log.exception(f"[post-coder] {pname}: agent-handled detection failed — running pipeline")

    def _run(cmd: list[str], **kw) -> _sp.CompletedProcess:
        # Pop timeout and cwd from kw so the caller's override doesn't collide
        # with the defaults we pass into _sp.run. Without this, e.g.
        # _run(..., timeout=180) raises TypeError("got multiple values for
        # keyword argument 'timeout'") — which crashes the whole pipeline
        # before our diagnostic checks run. cwd needs the same treatment:
        # canonical 2026-06-03 cycle KU MyJira #1320 — Guard 13 category 13a
        # self-heal called _run(..., cwd=working_dir, timeout=30) and crashed
        # with "subprocess.run() got multiple values for keyword argument
        # 'cwd'"; self-heal aborted, debris file left in the commit, feature
        # bounced unnecessarily.
        timeout = kw.pop("timeout", 120)
        cwd = kw.pop("cwd", working_dir)
        return _sp.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, **kw)

    # 1. Detect changes — anything uncommitted in the tree, OR committed-but-
    # not-pushed (the agent may have committed itself; we still need to push).
    status = _run(["git", "status", "--porcelain"])
    changed = [ln for ln in status.stdout.splitlines() if ln.strip()
               and not ln.endswith("session_result.json")
               and not ln.endswith("session_summary.md")
               and "/Temp/" not in ln and "/Results/" not in ln]
    has_unpushed_commits = False
    for ref in ("@{u}", "origin/main", "origin/master"):
        ahead = _run(["git", "rev-list", "--count", f"{ref}..HEAD"])
        if ahead.returncode == 0:
            try:
                has_unpushed_commits = int((ahead.stdout or "0").strip()) > 0
            except ValueError:
                pass
            break
    if not changed and not has_unpushed_commits:
        log.warning(f"[post-coder] {pname}: agent exited 0 but no code changes or unpushed commits — skipping PR")
        # Mark features Blocked so they don't loop in Implementing forever
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for f in assigned_features:
                    client.patch(f"/api/features/{f['id']}", json={
                        "status": "Blocked",
                        "blocked_reason": f"Coder session {session_uid} exited 0 with no code changes",
                    })
        except Exception:
            pass
        return pushed_ids
    log.info(f"[post-coder] {pname}: {len(changed)} changed file(s) detected — sample: {changed[:3]}")

    def _fmt_err(r) -> str:
        """Render a CompletedProcess for diagnostic logging — git often prints
        useful info on stdout, not stderr (e.g. 'nothing to commit')."""
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return f"rc={r.returncode} stdout={out[:300]!r} stderr={err[:300]!r}"

    # 2. Resolve target branch.
    # Sprint-PR mode: every coder run pushes to the same sprint/<id> branch so
    # there's exactly one PR per sprint (no fan-out, no orphan PRs). The branch
    # and PR were provisioned by the website's _maybe_provision_sprint_pr at
    # sprint activation; we just check it out and push commits.
    # Rework mode (NEW): when all assigned features point at the same existing
    # open PR (set by a prior coder cycle that got changes_requested), push to
    # that PR's head branch instead of cutting a new one. Eliminates the PR
    # fan-out we observed today (PRs 163/164/165 all covering same features).
    # Per-feature mode (default): cut a fresh `coder/<session_uid>` branch and
    # later open a new PR for it.
    feat_ids = [f["id"] for f in assigned_features]
    # 1-PR model (migration 043): every coder session opens its own session
    # PR with base=<default branch>. There is no sprint integration branch
    # and no sprint PR. The legacy `sprint_pr_mode` toggle and its
    # bare-branch-no-PR False branch were retired with this cleanup —
    # behavior is unconditional now.

    # Resolve the repo's true default branch via origin refs — do NOT trust
    # the local HEAD. A prior _reset_workspace that silently failed (stale
    # index.lock, fetch error, checkout failure, etc.) can leave HEAD on a
    # coder/<uid> branch from a previous session. Reading HEAD then would
    # set default_branch to that coder branch, and downstream we'd both
    # (a) cut the new session branch off origin/coder/<prev-uid> and
    # (b) open the GitHub PR with base=coder/<prev-uid>. PR #77 on
    # StockAnalysis (feature 590) was the canonical incident — 2026-05-19.
    default_branch = ""
    for _candidate in ("origin/main", "origin/master"):
        _rv = _run(["git", "rev-parse", "--verify", _candidate])
        if _rv.returncode == 0:
            default_branch = _candidate.split("/", 1)[1]
            break
    if not default_branch:
        log.error(
            f"[post-coder] {pname}: could not resolve repo default branch via "
            f"origin/main or origin/master — bailing without opening a PR. "
            f"Workspace likely has no fetched remote refs; investigate _reset_workspace."
        )
        return pushed_ids

    # Belt-and-braces: if local HEAD is on a coder/* or sprint/* branch, two
    # cases are possible:
    #   (a) INTENTIONAL rework pre-checkout — docker_runner.py's
    #       _find_rework_branch + _checkout_branch (commit 80f42e3) lands
    #       the coder on the prior coder branch when assigned features have
    #       review_outcome=changes_requested + branch_name set, so the agent
    #       inherits the prior implementation. HEAD will equal a
    #       feature.branch_name in this case. The rework_pr_mode block below
    #       will commit + force-push to this same branch — no main-reset
    #       needed.
    #   (b) CORRUPTION — _reset_workspace silently failed, HEAD on an
    #       unrelated coder/sprint branch. Force-restore to main before
    #       continuing, as before.
    # Canonical 2026-06-01 incident: routing-fix c58b0f5 surfaced this
    # interaction — rework pre-checkout sessions were force-resetting to
    # main and the subsequent `git checkout -B coder/<uid>` failed with
    # "you need to resolve your current index first" because the agent's
    # uncommitted edits + prior-branch state conflicted on the reset.
    # Every rework cycle ended with features_pushed=0.
    _hb = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    current_head = (_hb.stdout or "").strip()
    if current_head.startswith(("coder/", "sprint/")):
        intentional_rework = current_head in {
            f.get("branch_name") for f in assigned_features
            if f.get("branch_name")
        }
        if intentional_rework:
            log.info(
                f"[post-coder] {pname}: HEAD on {current_head!r} matches an "
                f"assigned feature's branch_name — intentional rework "
                f"pre-checkout (docker_runner._find_rework_branch). Skipping "
                f"main-reset; rework_pr_mode below will commit + force-push "
                f"to this branch."
            )
        else:
            log.warning(
                f"[post-coder] {pname}: HEAD is on {current_head!r} entering "
                f"post-coder (should have been {default_branch!r} after "
                f"_reset_workspace, and does NOT match any assigned "
                f"feature.branch_name) — forcing checkout. This indicates "
                f"_reset_workspace silently failed; check the prior session's "
                f"reset logs."
            )
            _pre = _run(["git", "stash", "push", "-u", "-m",
                         f"post-coder-pre-reset-{session_uid}"], timeout=300)
            _pre_stashed = (_pre.returncode == 0
                            and "No local changes to save" not in (_pre.stdout or ""))
            _co = _run(["git", "checkout", "-f", default_branch])
            if _co.returncode != 0:
                log.error(
                    f"[post-coder] {pname}: forced checkout to {default_branch} "
                    f"failed — {_fmt_err(_co)}. Bailing rather than opening a PR "
                    f"with the wrong base."
                )
                if _pre_stashed:
                    _run(["git", "stash", "pop"])  # best-effort restore
                return pushed_ids
            _run(["git", "reset", "--hard", f"origin/{default_branch}"])
            if _pre_stashed:
                _run(["git", "stash", "pop"])

    # Rework detection: when every assigned feature shares one open PR, the
    # prior coder cycle produced a session PR that the reviewer rejected and
    # bounced back. Force-push fresh commits to that session PR's head branch
    # so the reviewer sees the new diff on the same PR (preserves the comment
    # thread). The feature's `pr_number` always points at the session PR
    # under the 1-PR model.
    rework_pr_mode = False
    rework_pr_number: int | None = None
    rework_branch_name: str = ""
    rework_pr_url: str = ""
    existing_prs = {f.get("pr_number") for f in assigned_features
                    if isinstance(f.get("pr_number"), int)}
    if len(existing_prs) == 1:
        candidate = next(iter(existing_prs))
        # Safety-net: if `candidate` happens to match a sprint's cached
        # `pr_number` (legacy state from pre-1-PR products — sprint 175 on
        # StockAnalysis is the canonical example, with pr_number=15 still
        # in its DB row pointing at a now-merged PR), refuse rework even
        # if the PR happened to be re-opened. We never want to force-push
        # to a sprint integration branch. Best-effort lookup; on failure
        # we proceed without the guard rather than block all rework.
        sprint_pr_numbers: set[int] = set()
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as _sp_client:
                _sp_resp = _sp_client.get(f"/api/products/{product['id']}/sprints")
                if _sp_resp.status_code == 200 and isinstance(_sp_resp.json(), list):
                    sprint_pr_numbers = {
                        int(s["pr_number"]) for s in _sp_resp.json()
                        if isinstance(s, dict) and s.get("pr_number")
                    }
        except Exception:
            pass
        if candidate in sprint_pr_numbers:
            log.info(
                f"[post-coder] {pname}: features point at PR #{candidate} which "
                f"matches a sprint integration PR (legacy state) — not eligible "
                f"for rework; will open a fresh session PR"
            )
        else:
            gh_token_for_lookup = _get_gh_token()
            github_repo = product.get("github_repo", "")
            if gh_token_for_lookup and github_repo and candidate:
                try:
                    repo_slug = _parse_repo_slug(github_repo)
                    pr_resp = httpx.get(
                        f"https://api.github.com/repos/{repo_slug}/pulls/{candidate}",
                        headers={
                            "Authorization": f"Bearer {gh_token_for_lookup}",
                            "Accept": "application/vnd.github+json",
                        },
                        timeout=10,
                    )
                    if pr_resp.status_code == 200:
                        pr_data = pr_resp.json()
                        if isinstance(pr_data, dict) and pr_data.get("state") == "open":
                            # Under the 1-PR model an open PR shared across the
                            # assigned features is by definition a session PR
                            # from a prior coder cycle — always rework.
                            rework_pr_mode = True
                            rework_pr_number = candidate
                            rework_branch_name = (pr_data.get("head") or {}).get("ref") or ""
                            rework_pr_url = pr_data.get("html_url") or ""
                            log.info(
                                f"[post-coder] {pname}: rework mode — features {feat_ids} "
                                f"all point at open PR #{candidate} (branch={rework_branch_name!r}); "
                                f"force-pushing instead of opening a new PR"
                            )
                            if not rework_branch_name:
                                log.warning(
                                    f"[post-coder] {pname}: rework PR #{candidate} has no head.ref — opening a fresh session PR"
                                )
                                rework_pr_mode = False
                except Exception as e:
                    log.warning(f"[post-coder] {pname}: rework lookup for PR #{candidate} failed: {e} — opening a fresh session PR")

    # Branch resolution. Two modes, evaluated in priority order:
    #   1. rework_pr_mode — features share an open session PR, force-push to
    #      its branch so the reviewer's existing comment thread carries over.
    #   2. fresh session — cut `coder/<session_uid>` from the default branch
    #      tip and open a new session PR (base=default_branch, head=coder/
    #      <uid>). Each coder session ships ONE session PR directly to main
    #      on merge.
    if rework_pr_mode:
        # Stay on whatever branch we're on (HEAD = origin/main +
        # agent's edits) and reset a local branch with the rework branch
        # name pointing at HEAD. Force-push later replaces the rejected
        # prior commits on the remote PR branch with our fresh commits.
        branch = rework_branch_name
        co = _run(["git", "checkout", "-B", branch])
        if co.returncode != 0:
            log.warning(f"[post-coder] {pname}: rework `git checkout -B {branch}` failed — {_fmt_err(co)}")
            return pushed_ids
    else:
        # Cut a fresh session branch from the default branch tip. The
        # agent has been editing files in the working tree (`_reset_workspace`
        # left it on main/master); we stash those edits, switch to a
        # brand-new coder/<session_uid> branched off origin/<default>,
        # then pop the stash to apply the agent's diff onto the new
        # branch. The session PR will target the default branch as its
        # base, so the diff GitHub renders is exactly this session's
        # changes.
        branch = f"coder/{session_uid}"
        from orchestrator.integrations.git_ops import git_fetch_authenticated
        git_fetch_authenticated(["origin"], cwd=working_dir, product_name=pname, timeout=120)
        # Stash with -u so UNTRACKED files survive the branch switch too.
        # The agent's brand-new source files (e.g. SRC/healthz.ts, app/api/
        # .../route.ts) are untracked at this point; without -u, the branch
        # cut would refuse on the first overlap.
        # -u respects .gitignore (node_modules/, .next/, dist/) so the
        # stash stays small even on Node/Python/Go projects.
        stash_r = _run(["git", "stash", "push", "-u", "-m",
                        f"post-coder-{session_uid}"], timeout=300)
        stashed = (stash_r.returncode == 0
                   and "No local changes to save" not in (stash_r.stdout or ""))
        # Cut the new session branch from the default branch's current
        # remote tip. -B creates-or-resets, so a stale local coder/<uid>
        # from a crashed prior session is overwritten.
        co = _run(["git", "checkout", "-B", branch, f"origin/{default_branch}"])
        if co.returncode != 0:
            log.warning(
                f"[post-coder] {pname}: git checkout -B {branch} "
                f"origin/{default_branch} failed — {_fmt_err(co)}"
            )
            if stashed:
                _run(["git", "stash", "pop"])  # best-effort restore
            return pushed_ids
        if stashed:
            pop_r = _run(["git", "stash", "pop"])
            if pop_r.returncode != 0:
                # Conflict between the agent's edits and the default
                # branch's current content (likely because another session
                # merged to main between this session's start and its
                # post-coder run). Under the 1-PR model the session PR's
                # diff IS what gets reviewed, so a force-`--theirs`
                # resolution would produce a misleading PR. Mark every
                # assigned feature Blocked with a clear reason; PM
                # resolves manually (rebase or re-plan) and unblocks.
                # Drop the stash so it doesn't accumulate across sessions.
                conflicts = _run(["git", "diff", "--name-only", "--diff-filter=U"])
                paths = [p for p in conflicts.stdout.splitlines() if p.strip()]
                _run(["git", "stash", "drop"])
                log.warning(
                    f"[post-coder] {pname}: stash-pop conflict on session "
                    f"branch cut from origin/{default_branch} — {len(paths)} "
                    f"conflicted path(s): {paths[:5]}. Blocking assigned "
                    f"features for PM resolution."
                )
                try:
                    with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                        for f in assigned_features:
                            client.patch(f"/api/features/{f['id']}", json={
                                "status": "Blocked",
                                "blocked_reason": (
                                    f"Stash-pop conflict cutting coder/"
                                    f"{session_uid} from origin/{default_branch}; "
                                    f"agent's edits overlap with main's "
                                    f"current content (likely concurrent "
                                    f"session merged). Conflicted paths: "
                                    f"{paths[:5]}. Resolve by rebasing the "
                                    f"feature work onto current main."
                                ),
                            })
                except Exception:
                    pass
                return pushed_ids

    # 3. Add + commit + push.
    # Use the soft denylist stager so the coder's modifications to PM-curated
    # files (ARCHITECTURE.md, CLAUDE.md, .gitignore, etc.) get silently
    # stripped instead of poisoning the entire commit. The legitimate src/
    # / tests/ / alembic/ work still lands. See _coder_stage_with_denylist
    # docstring for the MyDocusign #36 incident that drove this.
    staged_count, stripped = _coder_stage_with_denylist(working_dir, _run, pname)
    # Sanity: was anything actually staged? `diff --cached --quiet` exits 1 if
    # there are staged changes, 0 if none. Catches the "porcelain showed lines
    # but add staged nothing" scenario (e.g. all changes inside a submodule or
    # excluded path, or all changes were stripped by the denylist).
    cached = _run(["git", "diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        ls = _run(["git", "status", "--porcelain"])
        if stripped:
            log.warning(
                f"[post-coder] {pname}: nothing in scope to commit after "
                f"denylist filter (all {len(stripped)} changed path(s) were "
                f"PM-curated files coders may not touch). "
                f"status={ls.stdout.strip()[:400]!r}"
            )
        else:
            log.warning(
                f"[post-coder] {pname}: nothing staged after add "
                f"despite {len(changed)} porcelain entries. "
                f"status={ls.stdout.strip()[:400]!r}"
            )
        return pushed_ids

    # Binary / oversize guard. GitHub rejects pushes with any single file >100 MB
    # (warns at 50 MB). The agent shouldn't be committing build artefacts at all,
    # but if .gitignore is missing or stale on the sprint branch (legacy state)
    # then npm/pip/go-mod outputs end up staged. Refuse to commit + surface the
    # offenders. On size violation we abort the pipeline rather than commit a
    # broken state — the next coder cycle will retry once .gitignore is fixed.
    SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB; GitHub's hard cap is 100 MB
    sized = _run(["git", "diff", "--cached", "--name-only", "-z"])
    paths = [p for p in (sized.stdout or "").split("\x00") if p]
    oversized = []
    for path in paths:
        try:
            sz = (Path(working_dir) / path).stat().st_size
        except OSError:
            continue
        if sz >= SIZE_LIMIT_BYTES:
            oversized.append((path, sz))
    if oversized:
        listing = ", ".join(f"{p} ({sz/1e6:.1f} MB)" for p, sz in oversized[:5])
        log.warning(
            f"[post-coder] {pname}: refusing to commit — {len(oversized)} oversized "
            f"file(s) staged (limit {SIZE_LIMIT_BYTES//1024//1024} MB). Sample: {listing}. "
            f"Likely missing or stale .gitignore on this branch — fix before retry."
        )
        # Reset the index so the bad files don't sit half-committed for the next session.
        _run(["git", "reset"])
        return pushed_ids

    # Include one [feature-<id>] tag per assigned feature so post-coder's
    # verification block (and future reviewer scoping) can scan PR commits
    # by feature. Tags appear as a sequence prefix on the same commit.
    feat_summary = ", ".join(f"#{i}" for i in feat_ids)
    feat_tags    = "".join(f"[feature-{i}]" for i in feat_ids)
    commit_msg = f"{feat_tags} feat: implement features {feat_summary} [coder-{session_uid}]"
    # --no-verify on the orchestrator's commit + push.
    # Pre-commit/pre-push hooks are valuable for HUMAN commits — they catch
    # lint/type/test regressions before code lands. The post-coder pipeline
    # is a deterministic system step that ships agent-generated code; it
    # runs under the orchestrator's UID with the orchestrator's PATH, not
    # the agent's. Agent-installed hooks (husky calling `npx`, lint-staged,
    # pre-commit, etc.) routinely fail in this environment because their
    # required binaries aren't on PATH. Real incident 2026-05-06 18:09:
    # husky's pre-commit script exited 127 (`npx not found`), git commit
    # rc=1, post-coder bailed, feature 183 stranded at Implemented.
    # Hooks would also catch nothing useful here — the agent's code is
    # what we WANT to commit, not what we want to gate. CI on the PR side
    # is the right place for those checks.
    commit_result = _run(["git", "commit", "--no-verify", "-m", commit_msg])
    if commit_result.returncode != 0:
        log.warning(f"[post-coder] {pname}: git commit failed — {_fmt_err(commit_result)}")
        return pushed_ids

    # push_args lists everything AFTER "push" — git_push_authenticated owns
    # the "git push" prefix and adds a one-shot credential helper so the App
    # installation token is never written to .git/config or visible in `ps`.
    if rework_pr_mode:
        # Replace the prior (rejected) commits on the remote PR branch with our
        # fresh commits. --force-with-lease aborts if the remote was touched
        # by anyone else since our last fetch.
        push_args = ["--no-verify", "--force-with-lease", "origin", branch]
    else:
        # Fresh session branch — set upstream so subsequent rework cycles can
        # detect it via the rework path above.
        push_args = ["--no-verify", "-u", "origin", branch]
    from orchestrator.integrations.git_ops import git_push_authenticated
    push_result = git_push_authenticated(push_args, cwd=working_dir, product_name=pname, timeout=180)
    if push_result.returncode != 0:
        log.warning(f"[post-coder] {pname}: git push failed: {push_result.stderr.strip()[:300]}")
        return pushed_ids
    log.info(f"[post-coder] {pname}: pushed branch {branch}")

    # Branch-tracking PATCH: persist `branch_name` on every assigned
    # feature row IMMEDIATELY after push, before any post-coder gate runs.
    # Without this, gate-bounced features (lint/test/verify rejects in
    # the section below) end at status=Implementing with NULL branch_name
    # in the DB — even though the code IS on origin at `branch`. The next
    # rework cycle's pre-checkout (docker_runner._find_rework_branch)
    # then can't locate the prior implementation and falls back to fresh
    # main, defeating the purpose of the rework-persist machinery.
    # Canonical incident: 2026-06-01 04:46 — feature #1062 bounced on
    # test-check before the trailing Reviewing-PATCH at line 3192 ever
    # ran; subsequent rework opened a fresh coder/<new_uid> off main.
    # The PATCH here is best-effort; failure logs at debug because the
    # downstream Reviewing-PATCH will retry the same field if gates pass.
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as _bt_client:
            for _fid in feat_ids:
                try:
                    _bt_client.patch(
                        f"/api/features/{_fid}",
                        json={
                            "branch_name": branch,
                            "changed_by": "post-coder:branch-tracking",
                        },
                    )
                except Exception as _e:
                    log.debug(
                        f"[post-coder] {pname}: branch_name PATCH for "
                        f"#{_fid} failed: {_e}"
                    )
    except Exception as _e:
        log.debug(
            f"[post-coder] {pname}: branch-tracking PATCH session failed: {_e}"
        )

    # 4. PR resolution. Two paths:
    #   - rework_pr_mode: reuse the existing open session PR we just force-
    #     pushed to (preserves the reviewer's comment thread).
    #   - fresh session: open a new session PR with base=<default branch>,
    #     head=coder/<session_uid>. The session PR is the reviewable unit;
    #     on approval auto-merge merges it directly into main.
    if rework_pr_mode:
        pr_number = int(rework_pr_number)  # type: ignore[arg-type]
        pr_url = rework_pr_url
        log.info(f"[post-coder] {pname}: reusing rework PR #{pr_number} — {pr_url}")
    else:
        # Open a new session PR targeting the default branch.
        gh_token_pr = _get_gh_token()
        github_repo_pr = product.get("github_repo", "")
        if not gh_token_pr or not github_repo_pr:
            log.warning(
                f"[post-coder] {pname}: cannot open session PR — missing "
                f"github_repo or auth token. Branch {branch} was pushed but "
                f"no PR will be linked; features stay Implementing for retry."
            )
            return pushed_ids
        repo_slug_pr = _parse_repo_slug(github_repo_pr)
        feat_bullets = "\n".join(
            f"- `[feature-{f['id']}]` {f.get('name','')}" for f in assigned_features
        ) or f"- session {session_uid}"
        pr_body = (
            f"Session `{session_uid}`\n\n"
            f"## Stories in this session\n{feat_bullets}\n\n"
            f"Targets `{default_branch}`; merges on reviewer approval."
        )
        try:
            pr_resp = httpx.post(
                f"https://api.github.com/repos/{repo_slug_pr}/pulls",
                headers={
                    "Authorization": f"Bearer {gh_token_pr}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=20,
                json={
                    "title": f"session {session_uid}: features {feat_summary}",
                    "head":  branch,
                    "base":  default_branch,
                    "body":  pr_body,
                    # Non-draft: the reviewer's approval flips review_outcome,
                    # and auto_merge_reviewer / sweep_product merge on that
                    # signal. Draft state would add an unreliable un-draft
                    # dance.
                    "draft": False,
                },
            )
            if pr_resp.status_code not in (200, 201):
                log.warning(
                    f"[post-coder] {pname}: session PR create returned "
                    f"{pr_resp.status_code}: {pr_resp.text[:200]} — "
                    f"branch {branch} pushed but no PR linked; features "
                    f"stay Implementing for retry"
                )
                return pushed_ids
            pr_data = pr_resp.json()
            pr_number = pr_data["number"]
            pr_url    = pr_data.get("html_url") or ""
            log.info(
                f"[post-coder] {pname}: opened session PR #{pr_number} "
                f"({branch} → {default_branch}) — {pr_url}"
            )
            # PR-tracking PATCH: persist pr_number/pr_url on every assigned
            # feature row IMMEDIATELY after PR creation. Same rationale as
            # the branch-tracking PATCH above — gate-bounced features need
            # the PR linkage in DB so the rework pre-checkout can find
            # the prior implementation. The Reviewing PATCH at the bottom
            # of this function sets these again on the all-gates-pass
            # path; this early PATCH is the gate-bounce path's only
            # opportunity to write them.
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as _pt_client:
                    for _fid in feat_ids:
                        try:
                            _pt_client.patch(
                                f"/api/features/{_fid}",
                                json={
                                    "pr_number": pr_number,
                                    "pr_url": pr_url,
                                    "changed_by": "post-coder:pr-tracking",
                                },
                            )
                        except Exception as _e:
                            log.debug(
                                f"[post-coder] {pname}: pr_number PATCH "
                                f"for #{_fid} failed: {_e}"
                            )
            except Exception as _e:
                log.debug(
                    f"[post-coder] {pname}: pr-tracking PATCH session "
                    f"failed: {_e}"
                )
        except Exception as e:
            log.warning(
                f"[post-coder] {pname}: session PR create raised: {e} — "
                f"branch {branch} pushed but no PR linked"
            )
            return pushed_ids

    # 4.4 Drift-scanner + reconciler chore-controller — runs BEFORE the lint
    # guard so it isn't starved by the lint-guard's early-return-on-violation
    # below. The detectors are pure functions of (working_dir, features); they
    # observe product-wide drift (architect review docs, duplicate DDL,
    # god-files, doc/code contract drift) that is independent of whether
    # THIS session's specific commit lints clean. Pre-2026-05-30 wiring put
    # this block AFTER the lint-guard's `return pushed_ids` — a coder stuck
    # in a lint-bounce loop on a single feature could starve the chore-
    # controller indefinitely, which exactly the canonical DocumentSign 2026-
    # 05-30 case (5 architect-review-pending findings sat unfiled while
    # feature #1102 cycled coder→lint-bounce→coder on a deps-coherence
    # violation). Best-effort — wrapped in try/except, never raises, never
    # bounces the feature.
    #
    # Scope: fetch ALL features for the product, not just session-assigned.
    # Cross-feature drift (e.g. design_doc_path set but the doc file never
    # landed — canonical calcv2 features 995/996/1001/1002) only surfaces
    # if every feature's state is visible to the detectors. Dedupe in
    # post_findings + file_corrective_chores keeps noise bounded across
    # cycles.
    try:
        from orchestrator import drift_detectors as _drift
        _product_id = product.get("id")
        with httpx.Client(base_url=PM_API_URL, timeout=10) as _drift_client:
            try:
                _resp = _drift_client.get(f"/api/products/{_product_id}/features")
                _all_features = _resp.json() if (200 <= _resp.status_code < 300) else assigned_features
            except Exception:
                _all_features = assigned_features
            _drift_findings = _drift.run_all(working_dir, _all_features)
            if _drift_findings:
                log.info(
                    f"[drift-scanner] {pname}: {len(_drift_findings)} finding(s) "
                    f"on session {session_uid} (scope={len(_all_features)} feature(s))"
                )
                _drift.post_findings(_drift_findings, _drift_client, pname)

            # Reconciler-as-controller. Objective code-drift detectors
            # (separate _CHORE_DETECTORS registry, not the comment-path
            # _DETECTORS above) emit high-severity findings filed as Approved
            # chore features. The existing coder→guard→reviewer pipeline is
            # the actuator, so corrections run through the same verification
            # as any feature. file_corrective_chores caps at 3/product/cycle
            # and dedupes open chores, so steady-state filing is bounded.
            #
            # Default ON for ALL products (2026-06-13) — the DogTinder
            # re-review confirmed comment-path findings get read but never
            # repaired; chores give every finding an owner. Resolution:
            #   1. product.config["reconciler_chores"] explicit bool wins
            #      (per-product opt-OUT: set False to disable for a product).
            #   2. else RECONCILER_CHORES_ENABLED env var as a global kill
            #      switch — ON unless explicitly set to a falsy value.
            # Best-effort: failure here never bounces the feature.
            _chores_cfg = (product.get("config") or {}).get("reconciler_chores")
            if isinstance(_chores_cfg, bool):
                _chores_on = _chores_cfg
            else:
                _chores_on = os.environ.get(
                    "RECONCILER_CHORES_ENABLED", "").strip().lower() \
                    not in ("0", "false", "no", "off")
            if _chores_on:
                _chore_findings = _drift.run_chore_detectors(working_dir, _all_features)
                if _chore_findings:
                    _filed = _drift.file_corrective_chores(
                        _chore_findings, _drift_client, _product_id, pname,
                    )
                    log.info(
                        f"[reconciler] {pname}: {len(_chore_findings)} chore-eligible "
                        f"finding(s), filed {_filed} corrective chore(s)"
                    )
    except Exception as _drift_e:
        log.warning(f"[post-coder] {pname}: drift-scanner raised {_drift_e}; continuing")

    # 4.5 Lint guards — auto-reject obviously-broken commits before they hit
    # the LLM reviewer. Per the 2026-05-07 audit, ~80% of reviewer rejections
    # cluster in 3-4 grep-able categories. Catching them here saves a slow
    # reviewer cycle (avg 15-60 min per rejection) and gives the next coder
    # cycle deterministic feedback in {reviewer_feedback}.
    # Cycle IV (2026-06-02): expose category-13a debris files so we can
    # auto-clean and self-heal instead of burning a full rework cycle on a
    # `git rm one.bak && commit` operation the system can do itself.
    # Canonical incidents: 1131 (session 7101, 48 min compute → bounced
    # on tests/test_usage_enforcement.py.bak), 1073 (contributed to
    # cycle GY cap-Block cascade), 1061 (2026-05-31), 1125 (2026-05-30).
    _debris_files_13a: list[str] = []
    lint_violations = _post_coder_lint_check(
        working_dir, _run, pname, debris_files_out=_debris_files_13a,
    )
    # Self-heal path: when the only lint violation is the agent-debris
    # category AND every debris file came from category 13a (filename
    # antipattern — safe to delete), `git rm` them in place, amend the
    # session commit, force-push, and clear the violations list so the
    # rest of the pipeline (test-check / verify-check / Reviewing
    # transition) proceeds. The agent's substantive work survives.
    # `# AGENT_DEBRIS_EXEMPT:` annotation is still honoured upstream in
    # the detector — exempt files never reach _debris_files_13a.
    # Mixed cases (debris + qa_pair_hits, or debris + other guards) still
    # hard-bounce because the non-debris violations require agent attention.
    if (
        lint_violations
        and _debris_files_13a
        and len(lint_violations) == 1
        and lint_violations[0].startswith("agent-debris file(s) in commit:")
    ):
        n_cleaned = len(_debris_files_13a)
        log.info(
            f"[post-coder] {pname}: lint-guard auto-cleaning {n_cleaned} "
            f"agent-debris file(s) (category 13a) and continuing: "
            f"{', '.join(_debris_files_13a[:5])}"
            f"{'...' if n_cleaned > 5 else ''}"
        )
        _self_healed = False
        try:
            _rm_r = _run(
                ["git", "rm", "-f", "--ignore-unmatch", "--", *_debris_files_13a],
                cwd=working_dir, timeout=30,
            )
            if _rm_r.returncode != 0:
                log.warning(
                    f"[post-coder] {pname}: lint-guard auto-clean `git rm` "
                    f"failed (exit {_rm_r.returncode}); falling through to "
                    f"normal bounce. stderr: "
                    f"{(_rm_r.stderr or '')[:200]}"
                )
            else:
                _amend_r = _run(
                    ["git", "commit", "--amend", "--no-edit"],
                    cwd=working_dir, timeout=30,
                )
                if _amend_r.returncode != 0:
                    log.warning(
                        f"[post-coder] {pname}: lint-guard auto-clean "
                        f"`git commit --amend` failed (exit "
                        f"{_amend_r.returncode}); falling through to bounce. "
                        f"stderr: {(_amend_r.stderr or '')[:200]}"
                    )
                else:
                    # Force-push the amended commit to the session branch.
                    # rework_pr_mode below already force-pushes anyway, but
                    # at this point the pipeline hasn't reached the push
                    # step yet. We push here so that test-check / verify-
                    # check / drift-scanner operate on the cleaned tree.
                    # `--force-with-lease` is safer than `--force` against
                    # the race where some other writer touched the branch,
                    # but pre-push happens before any other writer for this
                    # session, so either flag is acceptable.
                    _branch_r = _run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=working_dir, timeout=10,
                    )
                    _branch = (_branch_r.stdout or "").strip()
                    if _branch and _branch != "HEAD":
                        # Note: we deliberately don't push here — the
                        # downstream rework_pr_mode push handles it. The
                        # amend has rewritten the local commit; the next
                        # `git push --force-with-lease` to the session
                        # branch carries the cleaned tree to remote.
                        pass
                    # Post an informational comment so the audit trail
                    # shows what was cleaned and why no fix_attempt was
                    # burned.
                    try:
                        with httpx.Client(base_url=PM_API_URL, timeout=10) as _hc:
                            for _feat in assigned_features:
                                _fid = _feat["id"]
                                _hc.post(
                                    f"/api/features/{_fid}/comments",
                                    json={
                                        "author": "post-coder:lint-guard",
                                        "body": (
                                            f"ℹ post-coder lint-guard auto-cleaned "
                                            f"{n_cleaned} agent-debris file(s) "
                                            f"(category 13a filename antipattern) "
                                            f"and continued the pipeline. No "
                                            f"`fix_attempts` bump.\n\n"
                                            + "\n".join(
                                                f"- removed `{p}`"
                                                for p in _debris_files_13a[:20]
                                            )
                                            + (
                                                f"\n- … and {n_cleaned - 20} more"
                                                if n_cleaned > 20 else ""
                                            )
                                            + "\n\nTo block this auto-clean for a "
                                            "specific file (e.g. an intentional "
                                            "operator backup), put "
                                            "`# AGENT_DEBRIS_EXEMPT: <reason>` "
                                            "on its first non-empty line. Cycle "
                                            "IV (2026-06-02) introduced this "
                                            "self-heal — canonical incidents: "
                                            "#1131, #1073, #1061, #1125."
                                        ),
                                    },
                                )
                    except Exception as _e:
                        log.warning(
                            f"[post-coder] {pname}: lint-guard auto-clean "
                            f"info-comment post failed: {_e}"
                        )
                    lint_violations = []
                    _self_healed = True
        except Exception as _e:
            log.warning(
                f"[post-coder] {pname}: lint-guard auto-clean raised "
                f"{_e}; falling through to normal bounce"
            )
        if _self_healed:
            log.info(
                f"[post-coder] {pname}: lint-guard self-heal complete "
                f"({n_cleaned} debris file(s) removed); proceeding to "
                f"test-check"
            )

    if lint_violations:
        log.warning(
            f"[post-coder] {pname}: lint-guard fired ({len(lint_violations)} "
            f"violation(s)); bouncing back to Implementing+changes_requested"
        )
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for feat in assigned_features:
                    fid = feat["id"]
                    body = (
                        f"❌ lint-guard auto-reject "
                        f"({len(lint_violations)} violation(s) on commit before review):\n"
                        + "\n".join(f"- {v}" for v in lint_violations)
                        + f"\n\nThe orchestrator's deterministic post-coder lint guard "
                          f"caught these before the reviewer ran. Fix and re-push."
                    )
                    try:
                        client.post(
                            f"/api/features/{fid}/comments",
                            json={"author": "lint-guard", "body": body},
                        )
                        # Bounce feature back to Implementing+changes_requested.
                        # Implementing(4) ← Implemented(4) is rank-equal (no guard).
                        # Implementing ← Reviewing is in _ALLOWED_BACKWARD.
                        _bounce_resp = client.patch(
                            f"/api/features/{fid}",
                            json={
                                "status": "Implementing",
                                "review_outcome": "changes_requested",
                                "changed_by": "post-coder:lint-guard",
                            },
                        )
                        # Respect a supervisor / cap Block: if the feature is
                        # Blocked, the website quarantine rejects the un-block
                        # with 422. Don't retry, don't treat as error — the
                        # Block is protecting against exactly this loop. (The
                        # primary circuit breaker is the fix_attempts cap, which
                        # auto-Blocks at 5 lint bounces; this is belt-and-braces.)
                        if _bounce_resp.status_code == 422:
                            log.warning(
                                f"[post-coder] {pname}: feature #{fid} is Blocked "
                                f"— bounce rejected by quarantine (422); respecting "
                                f"the Block, not re-queuing."
                            )
                            continue
                        log.info(
                            f"[post-coder] {pname}: feature #{fid} bounced by "
                            f"lint-guard ({len(lint_violations)} issue(s))"
                        )
                    except Exception as e2:
                        log.warning(
                            f"[post-coder] {pname}: lint-guard PATCH for #{fid} "
                            f"failed: {e2}"
                        )
        except Exception as e:
            log.warning(f"[post-coder] {pname}: lint-guard PM client error: {e}")

        # Drop the agent's stale claims for the bounced features from
        # session_result.json. Without this, the subsequent
        # _reconcile_session_result re-applies the agent's "Implemented"
        # claim and undoes our Implementing+changes_requested PATCH (race
        # observed 2026-05-07: post-coder PATCH at 09:38:04.505, agent
        # PATCH at 09:38:04.521 — same wall-clock millisecond, opposite
        # direction; feature ended up Implemented + changes_requested,
        # which neither coder nor reviewer dispatch will claim → stuck).
        _filter_session_result_by_id(
            working_dir,
            {f["id"] for f in assigned_features},
            pname,
        )
        # Commit was pushed (record of attempt). Feature in rework cycle. Exit.
        return pushed_ids

    # 4b. Post-coder test execution (Phase 5 of quality-specs, 2026-05-19).
    # Run the stack's test command. If tests fail or fail-to-collect, bounce
    # the features to Implementing same way the lint guard does. If the test
    # env itself is broken (jest missing, node_modules absent), alert the
    # operator and roll the features back to their prior ready state without
    # bumping fix_attempts — broken-env is not the coder's fault. Calculator's
    # feature 594 cascade was driven by missing jest/dev-deps, which iterations
    # of coder rework can't fix.
    # /workspace symlink band-aid: test code that follows designer Verify
    # recipes uses absolute /workspace/... paths (e.g. `os.popen("grep
    # /workspace/src/...")`). The post-coder pipeline runs in the
    # pf-orchestrator container where /workspace doesn't exist, so those
    # tests fail even when the work is correct. The CM creates the symlink
    # for the duration of the pytest run and cleans up after. Tracked for
    # removal once the designer/coder prompts forbid absolute /workspace
    # paths and the in-flight features are reauthored.
    # QA/Tester gate: run the stack's tests in a throwaway agent-image
    # container (full toolchain present) rather than in-process. No /workspace
    # symlink needed — the container mounts the workspace at /workspace
    # natively. Deterministic (exit-code based); _post_coder_test_check's
    # detection + classification are unchanged.
    # Sidecar service env (REDIS_URL etc.): the per-session service
    # containers provisioned at launch are still up (teardown happens after
    # finalize); the gate containers connect to the same names.
    from orchestrator import services as _pf_services
    _service_env = _pf_services.session_service_env(product, session_uid)
    test_result = _post_coder_test_check(
        working_dir, _container_test_run(working_dir, service_env=_service_env), pname)
    if test_result.get("framework") != "none" and not test_result.get("passed"):
        env_broken = test_result.get("env_broken", False)
        first_failure = test_result.get("first_failure", "")
        collection_errors = test_result.get("collection_errors", 0)
        output_excerpt = (test_result.get("output") or "")[:1500]

        # service_missing triage (Phase C of service provisioning): a
        # connection-refused failure against a CATALOG service's default
        # port is never coder-fixable — iterating the coder against it
        # produced the DogTinder #1582 vendoring spiral (3 sessions, then
        # the entire Redis source tree in a 982k-line PR).
        #   - service DECLARED for the product but unreachable → the
        #     orchestrator's provisioning hiccuped: env_broken semantics
        #     (no fix_attempts bump, operator alert, retry next session).
        #   - service NOT declared → env-impossible spec: Block the
        #     features with the precise fix (designer files an infra
        #     story, or redesigns with the in-memory fake). No
        #     fix_attempts bump — this was never the coder's fault.
        _missing_svc = _detect_missing_service(test_result.get("output") or "")
        if _missing_svc and not test_result.get("pre_existing_only"):
            from orchestrator import services as _svc_mod
            if _missing_svc in _svc_mod.declared_services(product):
                log.warning(
                    f"[post-coder] {pname}: declared service '{_missing_svc}' "
                    f"unreachable in test container — treating as env_broken "
                    f"(provisioning hiccup, not the coder's bug)"
                )
                env_broken = True
            else:
                _svc_reason = (
                    f"Tests require a live {_missing_svc} service that is not "
                    f"declared for this product (connection refused on the "
                    f"{_missing_svc} default port). This is not coder-fixable: "
                    f"either the designer files a `Provision {_missing_svc} "
                    f"service` story (feature_type=infra; the orchestrator "
                    f"implements it after PM approval), or the story is "
                    f"redesigned to use the in-memory fake (fakeredis / sqlite "
                    f"/ moto / respx). Do not re-approve as-is."
                )
                log.warning(
                    f"[post-coder] {pname}: undeclared service "
                    f"'{_missing_svc}' required by tests — Blocking "
                    f"{len(assigned_features)} feature(s) for redesign "
                    f"(no fix_attempts bump)"
                )
                try:
                    with httpx.Client(base_url=PM_API_URL, timeout=15) as _sm_client:
                        for feat in assigned_features:
                            _sm_client.patch(f"/api/features/{feat['id']}", json={
                                "status": "Blocked",
                                "blocked_reason": _svc_reason,
                            })
                            _sm_client.post(
                                f"/api/features/{feat['id']}/comments",
                                json={"author": "post-coder:service-missing",
                                      "body": "🚫 " + _svc_reason +
                                              f"\n\nFirst failure: `{first_failure[:200]}`"},
                            )
                except Exception:
                    log.exception(f"[post-coder] {pname}: service-missing Block PATCH failed")
                try:
                    from orchestrator.alerts import send_alert
                    send_alert(
                        "warning",
                        f"{pname}: feature(s) Blocked — tests need a live "
                        f"'{_missing_svc}' service that isn't declared (session "
                        f"{session_uid}). Designer should file an infra story "
                        f"or redesign with the in-memory fake.",
                    )
                except Exception:
                    pass
                return pushed_ids

        # tool_missing triage (wave-5): the env_broken patterns already MATCH
        # command-not-found / not-on-PATH failures (cycle DL, 2026-06-01),
        # but env_broken semantics are "transient — operator fixes, retry",
        # which loops forever on a tool the sandbox will NEVER provide.
        # Route on the capability manifest: an obtainable tool reported
        # missing stays env_broken (image hiccup / npm ci not run); a
        # non-obtainable tool is terminal — Block for redesign with the
        # precise fix, no fix_attempts bump. Canonical grind this ends:
        # DogTinder #1518/19/20 (docker CLI — deliberately absent per the
        # security model) and the MyJira Ruff probes that cap-Blocked
        # against a pre-2026-06-01 image.
        if env_broken:
            _missing_tool = _detect_missing_tool(test_result.get("output") or "")
            if _missing_tool:
                from orchestrator.capabilities import is_obtainable_tool
                if not is_obtainable_tool(_missing_tool):
                    _tool_reason = (
                        f"Tests require the '{_missing_tool}' CLI, which is not in "
                        f"the agent image and will not be added (see "
                        f"orchestrator/capabilities.py). This is not coder-fixable. "
                        f"Redesign the story: meta-infrastructure artifacts "
                        f"(Dockerfile, CI workflows, hook configs) verify "
                        f"STATICALLY (hadolint / actionlint / config validation), "
                        f"never by executing the tool. Do not re-approve as-is."
                    )
                    log.warning(
                        f"[post-coder] {pname}: non-obtainable tool "
                        f"'{_missing_tool}' required by tests — Blocking "
                        f"{len(assigned_features)} feature(s) for redesign"
                    )
                    try:
                        with httpx.Client(base_url=PM_API_URL, timeout=15) as _tm_client:
                            for feat in assigned_features:
                                _tm_client.patch(f"/api/features/{feat['id']}", json={
                                    "status": "Blocked",
                                    "blocked_reason": _tool_reason,
                                })
                                _tm_client.post(
                                    f"/api/features/{feat['id']}/comments",
                                    json={"author": "post-coder:tool-missing",
                                          "body": "🚫 " + _tool_reason},
                                )
                    except Exception:
                        log.exception(f"[post-coder] {pname}: tool-missing Block PATCH failed")
                    try:
                        from orchestrator.alerts import send_alert
                        send_alert(
                            "warning",
                            f"{pname}: feature(s) Blocked — tests need the "
                            f"'{_missing_tool}' CLI which the agent image does not "
                            f"provide (session {session_uid}). Story needs a "
                            f"static-verification redesign.",
                        )
                    except Exception:
                        pass
                    return pushed_ids

        # Cycle GK (2026-06-04): cap consecutive env_broken bounces per
        # feature. The env_broken path was added so true infra failures
        # (jest missing, pip-install errors, `<tool> not on PATH`
        # assertions) wouldn't punish the coder — it rolls back to
        # Designed without bumping fix_attempts. But it MISFIRES on
        # fixture-setup AssertionErrors that look infra-shaped but are
        # actually feature-specific (a new dep's autouse fixture probes
        # for a CLI that isn't in the agent image, or the feature
        # rewrote a shared fixture and broke unrelated test files).
        # Without a cap, the coder rewrites → env_broken → rollback →
        # coder rewrites → ... forever, until the supervisor's
        # rapid_flap detector trips at 10 transitions/hour and Blocks
        # the feature. Canonical 2026-06-04 cycle: features 1307 (Auth
        # and DB skeleton, 6 status transitions in 26 min, all
        # Implemented→Designed env_broken bounces) and 1256 (File
        # attachment uploads, 4 transitions in 16 min). Both Blocked by
        # rapid_flap with a misleading "rapid status flap" reason that
        # hid the real env_broken misclassification.
        #
        # Cap rule: count prior `post-coder:test-env` comments per
        # assigned feature. If ANY feature has >= 2 prior env_broken
        # comments in its history, this round downgrades to a regular
        # test-failure bounce — fix_attempts bumps, the coder sees the
        # actual test output (E marks, fixture stack traces) instead
        # of "test env broken, retry later", and after another 2
        # bounces it hits the standard fix_attempts=5 cap-Block path
        # with a real reason for the operator. Symmetric in spirit to
        # the verify-check advisory-on-repeat above.
        env_broken_cap_tripped = False
        if env_broken:
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as cap_client:
                    for feat in assigned_features:
                        fid = feat["id"]
                        try:
                            r = cap_client.get(
                                f"/api/features/{fid}/comments",
                                params={"limit": 50},
                            )
                            if 200 <= r.status_code < 300:
                                recent = r.json() or []
                                prior_env = sum(
                                    1 for c in recent
                                    if isinstance(c, dict)
                                    and c.get("author") == "post-coder:test-env"
                                )
                                if prior_env >= 2:
                                    env_broken_cap_tripped = True
                                    log.warning(
                                        f"[post-coder] {pname}: env_broken cap "
                                        f"tripped on #{fid} ({prior_env} prior "
                                        f"post-coder:test-env comments); "
                                        f"downgrading to test-failure bounce so "
                                        f"fix_attempts climbs and agent sees "
                                        f"actual output"
                                    )
                                    break
                        except Exception:
                            # Cap is a safety net; on client error fall
                            # back to legacy env_broken behaviour (the
                            # conservative move — don't punish the agent
                            # if we can't read the history).
                            pass
            except Exception as e:
                log.warning(f"[post-coder] {pname}: env_broken cap client error: {e}")
            if env_broken_cap_tripped:
                env_broken = False
        if env_broken:
            log.error(
                f"[post-coder] {pname}: test env broken — NOT bumping "
                f"fix_attempts on the coder. Operator must fix the agent "
                f"image / dev-deps / test runner before next session."
            )
            try:
                from orchestrator.alerts import send_alert
                send_alert(
                    "error",
                    f"{pname}: post-coder test env broken (session "
                    f"{session_uid}). Test runner couldn't start. Excerpt:\n"
                    f"{output_excerpt[:400]}",
                )
            except Exception:
                pass
            # Roll features back to a non-agent-claimed state so the next
            # cycle doesn't relaunch a coder on the same feature against
            # a still-broken env. Reset to Approved/Designed; the operator's
            # env fix unblocks them.
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    for feat in assigned_features:
                        fid = feat["id"]
                        reset_to = "Designed" if feat.get("design_doc_path") else "Approved"
                        try:
                            client.post(
                                f"/api/features/{fid}/comments",
                                json={"author": "post-coder:test-env",
                                      "body": f"⚠️ Test env broken (no fix_attempts bump). "
                                              f"Operator alerted. Reset to {reset_to} "
                                              f"pending env repair. Excerpt:\n```\n"
                                              f"{output_excerpt[:600]}\n```"},
                            )
                            client.patch(
                                f"/api/features/{fid}",
                                json={
                                    "status": reset_to,
                                    "changed_by": "post-coder:test-env-broken",
                                },
                            )
                        except Exception as e2:
                            log.warning(
                                f"[post-coder] {pname}: env-broken rollback for "
                                f"#{fid} failed: {e2}"
                            )
            except Exception as e:
                log.warning(f"[post-coder] {pname}: env-broken PM client error: {e}")
            _filter_session_result_by_id(
                working_dir, {f["id"] for f in assigned_features}, pname,
            )
            return pushed_ids
        else:
            # Real test failure (or collection error). Bounce like lint guard.
            # Detect the "coder deleted skipped tests and shipped empty" loop:
            # pytest exit 5 ("no tests ran" / "collected 0 items") gets a more
            # targeted feedback message instead of "fix the failing test(s)",
            # which doesn't actually match the failure (there ARE no tests).
            no_tests_collected = (
                "collected 0 items" in (output_excerpt or "")
                or "no tests ran" in (output_excerpt or "")
            )
            if no_tests_collected:
                reason = "zero tests collected"
            else:
                reason = (f"collection errors ({collection_errors})"
                          if collection_errors else "tests failed")
            log.warning(
                f"[post-coder] {pname}: {reason} — bouncing features to "
                f"Implementing+changes_requested"
            )
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    for feat in assigned_features:
                        fid = feat["id"]
                        if no_tests_collected:
                            body = (
                                f"❌ post-coder test-check auto-reject "
                                f"(zero tests collected):\n\n"
                                f"```\n{output_excerpt[:800]}\n```\n"
                                f"Your push contains NO tests for this story. "
                                f"Empty test files are rejected the same as "
                                f"`pytest.skip()`. You MUST land at least one "
                                f"passing test that exercises an acceptance "
                                f"criterion before this can be reviewed. "
                                f"If a specific AC is impractical to test, "
                                f"rewrite the test to exercise a different "
                                f"part of the code that IS true — don't ship "
                                f"the story with zero coverage."
                            )
                        else:
                            # Cycle GL (2026-06-04): prefer the structured
                            # FAILURES/ERRORS traceback excerpt over the raw
                            # output dot-progress dump. On suites where every
                            # failure is a fixture-setup ERROR, the raw
                            # excerpt is 100% `tests/test_x.py EEEEE` markers
                            # and the agent has no idea WHICH test or WHY.
                            # _excerpt_pytest_tracebacks surfaces the first
                            # two real tracebacks (test name + tail of stack)
                            # so the bounce-back is actually actionable.
                            # Canonical: MyJira 1344/1346/1249 cascade.
                            tb_excerpt = _excerpt_pytest_tracebacks(
                                test_result.get("output") or ""
                            )
                            # Widen to the FULL current failure set, not just the
                            # first failure — so the rework coder fixes every
                            # failing test in one pass instead of trading one for
                            # another across rounds (the divergent fix-A-break-B
                            # thrash that drove #1370 toward cap-Block).
                            all_fail = _all_failing_tests(test_result.get("output") or "")
                            fail_list = ("\n".join(f"- `{t}`" for t in all_fail)
                                         if all_fail
                                         else f"- `{first_failure or 'see output below'}`")
                            body = (
                                f"❌ post-coder test-check auto-reject ({reason}):\n"
                                f"**All currently-failing tests ({len(all_fail) or 1}) — "
                                f"fix EVERY one this round, and do NOT regress the tests "
                                f"that currently pass:**\n{fail_list}\n\n"
                                f"```\n{tb_excerpt or output_excerpt[:800]}\n```\n"
                                f"Run the full suite locally and confirm it is entirely "
                                f"green before pushing — a fix that breaks a previously-"
                                f"passing test bounces again. If a test references symbols "
                                f"that don't exist (collection error), align the test file "
                                f"with the actual code or delete the orphan test."
                            )
                        try:
                            client.post(
                                f"/api/features/{fid}/comments",
                                json={"author": "post-coder:test-check", "body": body},
                            )
                            client.patch(
                                f"/api/features/{fid}",
                                json={
                                    "status": "Implementing",
                                    "review_outcome": "changes_requested",
                                    "changed_by": "post-coder:test-check",
                                },
                            )
                        except Exception as e2:
                            log.warning(
                                f"[post-coder] {pname}: test-check PATCH for "
                                f"#{fid} failed: {e2}"
                            )
            except Exception as e:
                log.warning(f"[post-coder] {pname}: test-check PM client error: {e}")
            _filter_session_result_by_id(
                working_dir, {f["id"] for f in assigned_features}, pname,
            )
            return pushed_ids

    # 4c. Verify-check gate (2026-05-30). Runs the per-AC `Verify:` recipes
    # the designer wrote into docs/story_<id>.md and compares to the
    # `Expected:` outputs. Catches the coder-gaming pattern where
    # `session_summary.md` has cosmetic `## AC<N> verification: OK` blocks
    # that don't reflect actually running the recipe. OFF by default —
    # gated by env POST_CODER_VERIFY_CHECK_ENABLED. When ON, real
    # mismatches bounce the feature like lint/test failures; server-
    # unavailable skips are logged as warnings but DO NOT bounce (the
    # post-coder pipeline doesn't start the app yet; a follow-up will
    # add uvicorn-bg startup for FastAPI products).
    # Same /workspace symlink band-aid as the test-check above — designer
    # Verify recipes are full of `grep /workspace/src/...` and `python -c
    # "with open('/workspace/...')"` patterns that need /workspace to
    # resolve. Without the CM, the recipes consistently report stdout=""
    # and exit 1/2, which the matcher reads as a real mismatch and bounces
    # the feature (canonical 2026-05-31 cascade: features 1147/1148/1149).
    # Recipes now run inside the agent container (toolchain present + /workspace
    # mounted natively), so the orchestrator-side /workspace symlink band-aid is
    # retired here too — and `npx`/node/tsc/python deps actually exist.
    verify_result = _post_coder_verify_check(
        working_dir=working_dir,
        product_name=pname,
        assigned_features=assigned_features,
        service_env=_service_env,
    )
    if verify_result["checked"] and verify_result["skipped"]:
        log.info(
            f"[verify-check] {pname}: {len(verify_result['skipped'])} recipe(s) "
            f"skipped (server unavailable / shell unavailable); "
            f"{verify_result['total']} total recipe(s) attempted"
        )
    if verify_result["checked"] and not verify_result["passed"]:
        n_fail = len(verify_result["failures"])
        # Group failures by feature so each bounced/advised feature gets one
        # actionable comment with all its failing ACs.
        per_feature: dict[int, list[dict]] = {}
        for f in verify_result["failures"]:
            per_feature.setdefault(f["feature_id"], []).append(f)
        # Cycle GJ (2026-06-02): cap verify-check at 1 hard-bounce per feature.
        # A feature that has already received a `post-coder:verify-check`
        # comment in the last 24h gets advisory-mode on this round: the new
        # mismatch comment is still posted (so the reviewer sees it), but the
        # feature is NOT bounced — it proceeds to Reviewing. The reviewer
        # (LLM, smarter than recipe-grep) can judge whether the AC mismatch
        # reflects a real bug or a stale designer recipe and approve/reject
        # accordingly. Test-check + lint-guard bounces are unchanged.
        # Canonical 2026-06-02 cycle GJ DocumentSign: 17 verify-check bounces
        # in 4h on features 1073/1074/1075/1119/1126/1221/1101/1102, with
        # only 1 reviewer engagement total. Designer-authored AC recipes have
        # systematic antipatterns (env-var-after-app-import, brittle
        # exact-string output expectations) that no coder can satisfy and
        # bounce-loop until cap-Block. Net throughput: 0 features merged in
        # 4h despite ~30 coder sessions. Symmetric in spirit to
        # supervisor.detect_repeated_review_feedback which Blocks features
        # with N matching reviewer comments — here we convert the gate
        # itself to advisory after the first fire.
        verify_advisory_fids: set[int] = set()
        # Agentless-style progress tiebreaker (OSS borrowing #6, 2026-06-04):
        # capture the AC-failure count from the most-recent prior verify-check
        # comment for each feature so the new comment can include the delta.
        # If the count is decreasing across iterations, surface "📈 progress"
        # in the new comment so the reviewer and the next coder see "this is
        # iterating constructively." If it's increasing, surface "📉
        # regression" so the reviewer knows the last rework backslid. Spirit
        # of Agentless's repair-tiebreaker (compare patch variants by failing
        # test count, prefer the one that passes more); adapted to our
        # rework-cycle model where there's only one variant at a time but
        # we want trend visibility.
        #
        # Tag format embedded in verify-check comments (machine-parseable;
        # invisible to humans because HTML comment): <!-- ac_fails=N -->
        prior_ac_fails: dict[int, int] = {}
        import re as _vre
        _AC_FAILS_TAG_RE = _vre.compile(r"<!--\s*ac_fails=(\d+)\s*-->")
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as advisory_client:
                for fid in list(per_feature.keys()):
                    try:
                        comments_r = advisory_client.get(
                            f"/api/features/{fid}/comments",
                            params={"limit": 50},
                        )
                        if 200 <= comments_r.status_code < 300:
                            recent = comments_r.json() or []
                            # Find the most-recent verify-check comment to
                            # both flag advisory mode AND extract its
                            # prior failure count (if tagged).
                            verify_comments = [
                                c for c in recent
                                if isinstance(c, dict)
                                and c.get("author") == "post-coder:verify-check"
                            ]
                            if verify_comments:
                                verify_advisory_fids.add(fid)
                                # Comments come newest-first or oldest-first
                                # depending on impl; sort defensively by
                                # created_at to find the actual latest.
                                verify_comments.sort(
                                    key=lambda c: (c.get("created_at") or "")
                                )
                                latest_body = (verify_comments[-1].get("body") or "")
                                m = _AC_FAILS_TAG_RE.search(latest_body)
                                if m:
                                    try:
                                        prior_ac_fails[fid] = int(m.group(1))
                                    except ValueError:
                                        pass
                    except Exception:
                        # On client error, fall back to hard-bounce (safer
                        # default — bouncing is the conservative move).
                        pass
        except Exception:
            pass
        n_advisory = len(verify_advisory_fids)
        n_bounce = len([fid for fid in per_feature if fid not in verify_advisory_fids])
        log.warning(
            f"[post-coder] {pname}: verify-check found {n_fail} AC recipe "
            f"mismatch(es); bouncing {n_bounce} feature(s), advisory-mode "
            f"for {n_advisory} (already had prior verify-check comment)"
        )
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for feat in assigned_features:
                    fid = feat["id"]
                    fails = per_feature.get(fid)
                    if not fails:
                        continue
                    is_advisory = fid in verify_advisory_fids
                    bullets = []
                    for f in fails:
                        # Surface stderr when present: assertion tracebacks /
                        # import errors land there, and a bare "exit 1" with
                        # empty stdout gives the rework coder nothing to act
                        # on (canonical 2026-06-11 DogTinder #1513 AC3/AC4 —
                        # stderr was captured at the _run_verify layer but
                        # dropped by this formatter).
                        _err = (f.get('actual_stderr') or '').strip()
                        bullets.append(
                            f"- AC{f['ac']}: command `{f['command'][:140]}`\n"
                            f"  expected `{f['expected'][:120]}`\n"
                            f"  actual stdout `{(f['actual_stdout'] or '').strip()[:200]}` "
                            f"(exit {f['actual_exit']})"
                            + (f"\n  stderr tail `{_err[:200]}`" if _err else "")
                            + _verify_actionable_hint(
                                f['command'], f['actual_stdout'], f['actual_exit'])
                        )
                    # Agentless tiebreaker: compute progress trend if we have
                    # a prior count. delta < 0 = fewer failures than last
                    # attempt (progress); delta > 0 = more failures
                    # (regression); delta == 0 = stagnant.
                    prior_n = prior_ac_fails.get(fid)
                    current_n = len(fails)
                    trend_line = ""
                    if prior_n is not None:
                        delta = current_n - prior_n
                        if delta < 0:
                            trend_line = (
                                f"📈 **Progress detected**: {current_n} mismatch(es) "
                                f"this attempt vs {prior_n} previously (Δ={delta}). "
                                f"The rework is reducing failures — keep going, "
                                f"reviewer should weigh this when judging.\n\n"
                            )
                        elif delta > 0:
                            trend_line = (
                                f"📉 **Regression detected**: {current_n} mismatch(es) "
                                f"this attempt vs {prior_n} previously (Δ=+{delta}). "
                                f"The latest rework introduced MORE AC failures "
                                f"than it fixed. Reviewer should weigh this — "
                                f"may indicate the coder is editing the wrong "
                                f"surface or misreading the recipe.\n\n"
                            )
                        else:
                            trend_line = (
                                f"➖ **Stagnant**: {current_n} mismatch(es) this "
                                f"attempt, same as last time. The same AC(s) keep "
                                f"failing — coder may need different guidance. "
                                f"Reviewer should consider whether the recipe "
                                f"itself is unsatisfiable.\n\n"
                            )
                    if is_advisory:
                        header = (
                            f"⚠ post-coder verify-check ADVISORY "
                            f"({len(fails)} AC recipe mismatch(es)) — "
                            f"NOT bouncing (already had a prior verify-check "
                            f"comment; gate is advisory on repeats so the "
                            f"reviewer can judge):"
                        )
                        footer = (
                            "\n\nThe verify recipes diverged from Expected "
                            "again. Cycle GJ caps verify-check at 1 hard "
                            "bounce per feature, so this is informational — "
                            "the reviewer will see this comment alongside "
                            "your code and decide whether the AC mismatch "
                            "reflects a real bug or a stale designer recipe."
                        )
                    else:
                        header = (
                            f"❌ post-coder verify-check auto-reject "
                            f"({len(fails)} AC recipe mismatch(es)):"
                        )
                        footer = (
                            "\n\nThese are the per-AC `Verify:` recipes the "
                            "designer wrote into the story doc. The pipeline "
                            "ran each one and the actual output diverged from "
                            "`Expected:`. Either your implementation doesn't "
                            "satisfy the AC, or your `session_summary.md` "
                            "AC verification block was self-reported without "
                            "running the recipe. Re-run each Verify command "
                            "yourself, observe the actual output, and fix the "
                            "code until it matches Expected. "
                            "Do NOT paste fake outputs into session_summary "
                            "to make this go away — the gate runs the recipes "
                            "independently. Next repeat verify-check failure "
                            "on this feature will be advisory-only (gate "
                            "caps at 1 hard bounce per feature)."
                        )
                    # Embed the structured ac_fails tag for the next
                    # verify-check run to parse. HTML comments render as
                    # nothing in Markdown so this is invisible to humans
                    # but machine-parseable by the regex above.
                    machine_tag = f"\n\n<!-- ac_fails={current_n} -->"
                    body = (
                        trend_line
                        + header + "\n\n"
                        + "\n".join(bullets)
                        + footer
                        + machine_tag
                    )
                    try:
                        client.post(
                            f"/api/features/{fid}/comments",
                            json={"author": "post-coder:verify-check", "body": body},
                        )
                        # Cycle GJ (2026-06-02): advisory-mode features do
                        # NOT get bounced. The comment above is informational;
                        # the feature stays at its current status (typically
                        # Implemented, post-coder push already succeeded) and
                        # the downstream Reviewing-PATCH below proceeds. Skip
                        # the rest of this block (status downgrade + fix_attempts
                        # bump) only when this is the first verify-check bounce.
                        if is_advisory:
                            continue
                        # Cycle DG (2026-06-01): explicitly compute and PATCH
                        # the bumped fix_attempts. The website's auto-bump
                        # only fires on a real status transition (Implemented
                        # → Implementing) or review_outcome transition (None
                        # → changes_requested). When verify-check fires
                        # repeatedly on the same Implementing+changes_requested
                        # feature (because the agent re-pushed without
                        # marking Implemented, or the agent re-claims and
                        # post-coder bounces again instantly), the PATCH is
                        # idempotent and the auto-bump skips. Without an
                        # explicit bump here, the cascade is unbounded:
                        # rapid_flap can't fire (no status flap), the cap-
                        # route never triggers, and the feature loops
                        # forever. Canonical 2026-06-01 DocumentSign #1177:
                        # 2 verify-check bounces 10min apart, fix_attempts
                        # stuck at 1, ruff missing from agent image was the
                        # underlying cause but no circuit-breaker fired.
                        # When caller includes `fix_attempts` in the PATCH,
                        # the website's should_bump check at main.py:1891
                        # skips its own auto-bump and uses our value.
                        try:
                            cur = client.get(f"/api/features/{fid}").json()
                            cur_attempts = int(cur.get("fix_attempts") or 0)
                        except Exception:
                            cur_attempts = 0
                        client.patch(
                            f"/api/features/{fid}",
                            json={
                                "status": "Implementing",
                                "review_outcome": "changes_requested",
                                "fix_attempts": cur_attempts + 1,
                                "changed_by": "post-coder:verify-check",
                            },
                        )
                    except Exception as e2:
                        log.warning(
                            f"[post-coder] {pname}: verify-check PATCH for "
                            f"#{fid} failed: {e2}"
                        )
        except Exception as e:
            log.warning(
                f"[post-coder] {pname}: verify-check PM client error: {e}"
            )
        # Cycle GJ (2026-06-02): if every feature was advisory (all already
        # had a prior verify-check comment), don't filter or return — let the
        # downstream Reviewing-PATCH run so these features reach the reviewer.
        # If any feature was hard-bounced, we still drop here (the bounced
        # feature(s) need the reconcile-filter to prevent state churn). For
        # mixed cases (some advisory, some bounced) we still drop — the
        # advisory features will get picked up on the next coder cycle if
        # the bounced one(s) are reworked successfully; this preserves the
        # invariant that a session result reflects a single coherent outcome.
        all_advisory = (
            verify_result["failures"]
            and verify_advisory_fids
            and all(
                f["feature_id"] in verify_advisory_fids
                for f in verify_result["failures"]
            )
        )
        if not all_advisory:
            _filter_session_result_by_id(
                working_dir, {f["id"] for f in assigned_features}, pname,
            )
            return pushed_ids
        log.info(
            f"[post-coder] {pname}: verify-check all-advisory — "
            f"proceeding to Reviewing for {len(verify_advisory_fids)} feature(s)"
        )

    # 5. Mark assigned features as Reviewing + link to the PR via direct PM
    # API PATCH. Until 2026-05-06 this used a session_result.json append +
    # reconcile read, but that file is also the agent's progress channel and
    # cross-session-pollution / Windows-bind-mount-cache races meant the
    # reconciler routinely never saw the post-coder's writes (every session's
    # log: "wrote 1 entries to session_result.json" + "0/1 feature updates
    # applied" — 0 progress events in 9h, the keystone of "0 features
    # released"). The PR is real on GitHub at this point; the DB MUST learn
    # about it or features get stranded and eventually auto-Blocked at
    # fix_attempts cap. changed_by="post-coder:fallback" is in the website's
    # _RANK_GUARD_BYPASS list — defends the rare case where the feature has
    # already been advanced past Reviewing by another path (auto_merge race).
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for feat in assigned_features:
                fid = feat["id"]
                try:
                    # Clear review_outcome on every Implementing→Reviewing
                    # transition. This is a fresh push that needs fresh review;
                    # last cycle's outcome no longer applies. Without this clear,
                    # state_machine's in-line hybrid-combo normalizer would
                    # demote the feature back to Implementing within ~30s of
                    # every push, blocking forward progress and bumping
                    # fix_attempts toward auto-Block. Real incident 2026-05-10
                    # 01:21: feature #379 cycled coder→Reviewing→demote 4 times
                    # in 50min. (The redundant per-cycle sweep that used to
                    # also catch this was deleted in the Tier-2 dead-code pass
                    # — the state_machine normalizer is now the sole defense.)
                    r = client.patch(
                        f"/api/features/{fid}",
                        json={
                            "status": "Reviewing",
                            "pr_number": pr_number,
                            "pr_url": pr_url,
                            # branch_name is the SESSION branch (coder/<uid>),
                            # not the sprint branch. The reviewer dispatch
                            # uses it to check out exactly the branch under
                            # review, so the diff the agent sees matches the
                            # PR diff GitHub renders.
                            "branch_name": branch,
                            "review_outcome": None,
                            "changed_by": "post-coder:fallback",
                        },
                    )
                    r.raise_for_status()
                    pushed_ids.append(int(fid))
                    log.info(
                        f"[post-coder] {pname}: feature #{fid} → Reviewing pr={pr_number}"
                    )
                except Exception as e2:
                    log.warning(
                        f"[post-coder] {pname}: direct PATCH for feature #{fid} failed: {e2}"
                    )
    except Exception as e:
        log.warning(f"[post-coder] {pname}: PM API client error during reviewing PATCH: {e}")
    return pushed_ids
