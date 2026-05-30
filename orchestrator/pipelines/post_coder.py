"""
Post-session pipeline for coder runs — the orchestrator-side git ceremony.

Path B (current architecture): the coder agent ONLY writes code to the
workspace. This pipeline takes care of every subsequent git step:
  1. Detect agent-handled vs not-handled features (verifies claimed PRs
     have real ``[feature-N]`` commit tags on GitHub before trusting them)
  2. Detect uncommitted changes / unpushed commits in the workspace
  3. Resolve target branch in three modes:
       - sprint-PR mode  → reuse the already-provisioned sprint branch + PR
       - rework mode     → all features point at one open PR; force-push to it
       - per-feature mode → cut ``coder/<session_uid>`` (legacy fallback)
  4. add + commit (with ``[feature-N]`` tags) + push (force-with-lease in rework)
  5. Append Reviewing entries to session_result.json (with PM-API fallback PATCH
     if the file write fails — keeps a real GitHub PR from being stranded)

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
This is the largest single function in the orchestrator (~500 LOC). Phase 3
will decompose it into 4 helpers (detect_unpushed_work, verify_agent_pr_tags,
commit_and_push, record_reviewing_entries).
"""

import ast as _ast
import json as _json
import logging
import os
import subprocess as _sp
from pathlib import Path

import httpx

from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine
from orchestrator.integrations.github import _get_gh_token, _parse_repo_slug
from orchestrator.session.result_io import _filter_session_result_by_id

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


def _post_coder_lint_check(working_dir: str, _run, product_name: str = "?") -> list[str]:
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
    try:
        files_r = _run(["git", "show", "HEAD", "--name-only", "--pretty=",
                        "--diff-filter=AM"], timeout=20)
        if files_r.returncode != 0:
            return []
        files = [
            f.strip() for f in (files_r.stdout or "").splitlines()
            if f.strip()
            and not f.endswith("session_result.json")
            and not f.endswith("session_summary.md")
            and not f.startswith(".sprint-79")  # scaffold marker
        ]
    except Exception:
        return []

    # Guard 17 (AST-diff deletion safety) also needs to run on deletion-only
    # commits, where `files` (AM-only) is empty but a `D` entry exists. Probe
    # for any MD file up front; if both are empty, nothing to check.
    try:
        _has_md_r = _run(["git", "show", "HEAD", "--name-only", "--pretty=",
                          "--diff-filter=MD"], timeout=20)
        _has_md = bool(_has_md_r.returncode == 0 and (_has_md_r.stdout or "").strip())
    except Exception:
        _has_md = False
    if not files and not _has_md:
        return []

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
    if src_files:
        impl_files = [f for f in src_files if "test" not in f.lower()]
        if impl_files:
            try:
                r = _run([
                    "grep", "-n", "-iE",
                    r"\b(TODO|FIXME|XXX|HACK)\b"
                    r"|throw new Error\([\"'][^\"']*((not |un)?implemented|todo|placeholder|stub|coming soon)[^\"']*[\"']\)"
                    r"|raise NotImplementedError"
                    r"|NotImplementedError\(\)"
                    r"|//\s*(placeholder|not implemented|stub)"
                    r"|#\s*(placeholder|not implemented|stub)",
                ] + impl_files, timeout=15)
                if r.returncode == 0:
                    raw_hits = [h for h in (r.stdout or "").splitlines() if h.strip()]
                    # Skip JSX/HTML `placeholder="..."` attributes (common UX
                    # text, not a stub marker).
                    _ATTR_SKIPS = (
                        'placeholder="', "placeholder='",
                        "placeholder={",
                    )
                    bad_files = set()
                    for h in raw_hits:
                        parts = h.split(":", 2)
                        if len(parts) < 3:
                            continue
                        fname, _lineno, line = parts
                        if any(a in line for a in _ATTR_SKIPS):
                            continue
                        bad_files.add(fname)
                    if bad_files:
                        files_sample = sorted(bad_files)
                        sample = ", ".join(files_sample[:3])
                        more = "..." if len(files_sample) > 3 else ""
                        violations.append(
                            f"placeholder / TODO / NotImplementedError in "
                            f"implementation files: {sample}{more}. Every "
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

    def _read(rel_path: str) -> str:
        try:
            from pathlib import Path as _P
            return (_P(working_dir) / rel_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

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
        _re.compile(r"_v[0-9]+[._]"),
        _re.compile(r"_complete[._]"),
        _re.compile(r"^temp_fixed"),
        _re.compile(r"^src_(head|tail)_"),
    ]
    _SCRATCH_DIRS = ("Temp/", "temp/", "temp_storage/")
    _SOURCE_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb")
    debris_hits = []
    qa_pair_hits = []
    from pathlib import Path as _PP
    for f in files:
        fname = _PP(f).name
        # 13a: name patterns
        debris_match = next((p.pattern for p in _DEBRIS_PATTERNS if p.search(fname)), None)
        if debris_match:
            # Honour the exemption marker on the first non-empty line
            first = next((ln for ln in _read(f).splitlines() if ln.strip()), "")
            if "AGENT_DEBRIS_EXEMPT" in first:
                continue
            debris_hits.append(f"{f} (matches {debris_match})")
            continue
        # 13b: tracked files in scratch directories the template marks
        # as never-committed
        if any(f.startswith(d) for d in _SCRATCH_DIRS):
            first = next((ln for ln in _read(f).splitlines() if ln.strip()), "")
            if "AGENT_DEBRIS_EXEMPT" in first:
                continue
            debris_hits.append(f"{f} (in scratch dir — template forbids commits here)")
            continue
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
        # through unchanged.
        _SIBLING_SUFFIX_RE = _re.compile(
            r"^(.*?test_[A-Za-z0-9_]+)"
            r"_(qa|manager|class|cases|extra|additional|more|new|v2)"
            r"(\.[A-Za-z]+)$"
        )
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
    try:
        md_r = _run(["git", "show", "HEAD", "--name-only", "--pretty=",
                     "--diff-filter=MD"], timeout=20)
        md_files = (
            {f.strip() for f in (md_r.stdout or "").splitlines()
             if f.strip().endswith(".py")}
            if md_r.returncode == 0 else set()
        )
    except Exception:
        md_files = set()

    if md_files:
        try:
            all_r = _run(["git", "show", "HEAD", "--name-only", "--pretty=",
                          "--diff-filter=AMD"], timeout=20)
            all_changed = (
                {f.strip() for f in (all_r.stdout or "").splitlines() if f.strip()}
                if all_r.returncode == 0 else set(md_files)
            )
        except Exception:
            all_changed = set(md_files)

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
                removed_symbols.append((path, name))

        if removed_symbols:
            dangling: list[str] = []
            for path, name in removed_symbols:
                try:
                    g = _run(["git", "grep", "-l", "-w", name, "--", "*.py"],
                             timeout=15)
                except Exception:
                    continue
                if g.returncode != 0:
                    continue
                hits = [h.strip() for h in (g.stdout or "").splitlines()
                        if h.strip()]
                surviving = [h for h in hits if h not in all_changed]
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
        "yaml":       "pyyaml",
        "jwt":        "PyJWT",
        "cv2":        "opencv-python",
        "PIL":        "Pillow",
        "sklearn":    "scikit-learn",
        "bs4":        "beautifulsoup4",
        "dateutil":   "python-dateutil",
        "dotenv":     "python-dotenv",
        "magic":      "python-magic",
        "MySQLdb":    "mysqlclient",
        "google":     "google-cloud-storage",  # best-effort; google.* is huge
        "OpenSSL":    "pyOpenSSL",
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
            _skip_dirs = {".git", ".venv", "venv", "env", "__pycache__",
                          "node_modules", "Temp", "Results", "dist", "build",
                          ".pytest_cache", ".mypy_cache"}
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
                try:
                    src = (_wd_path / f).read_text(
                        encoding="utf-8", errors="replace")
                    tree = _ast.parse(src)
                except (OSError, SyntaxError, ValueError):
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

            _missing = sorted(set(_imported) - _declared)
            if _missing:
                sample = ", ".join(
                    f"`{pkg}` (used in {_imported[pkg]})" for pkg in _missing[:5]
                )
                more = f" ... ({len(_missing) - 5} more)" if len(_missing) > 5 else ""
                violations.append(
                    "imports use packages not declared in "
                    + " / ".join(_req_present) + " (deps-coherence): "
                    + sample + more
                    + ". The agent image masks this because it pre-installs "
                    "common Python libs; the reviewer and any fresh `pip "
                    "install -r requirements.txt && pytest` will fail.\n"
                    "\n"
                    "CORRECT FIX: append the missing package(s) to "
                    "requirements.txt (or requirements-dev.txt for "
                    "test-only deps). Pin a version range, e.g. "
                    "`pytest>=8.0,<10`.\n"
                    "\n"
                    "WRONG FIX (do NOT do this): removing the import "
                    "from the source file, or deleting tests that "
                    "trigger the check. The check is purely static — "
                    "AST-walk imports vs requirements.txt. A passing "
                    "`pip install && pytest` locally does not satisfy "
                    "it; the violating import must remain AND the "
                    "package must appear in a requirements file."
                )
    except Exception:
        log.debug("Guard 18 deps-coherence raised", exc_info=True)

    return violations


def _introduced_failures(feature_failed: list[str], baseline_failed: set[str]) -> list[str]:
    """Test ids that failed for the feature but NOT on the baseline.

    Pure + order-preserving over `feature_failed`. A test that fails on both
    the feature branch and origin/main is pre-existing (not this feature's
    fault); one that fails only on the feature branch was introduced by it.
    """
    return [t for t in feature_failed if t not in baseline_failed]


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
        rr = _sp.run(["pytest", "-q", "--no-header", "-s",
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
        collect_cmd = ["pytest", "--collect-only", "-q", "-s"]
        run_cmd     = ["pytest", "-q", "--no-header", "-s"]
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
        # pytest reports "unrecognized arguments: --cov..." when a plugin in
        # the product's pytest.ini addopts isn't installed in the env running
        # the post-coder check (pytest-cov, pytest-xdist, etc.). That's a
        # missing-tool problem, not a code bug — coder shouldn't be punished.
        r"unrecognized arguments:\s*--(cov|xdist|benchmark|mock|django|sugar)",
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
                    # All pip install failures classified env_broken:
                    # operator alert fires, fix_attempts not bumped. If the
                    # agent put a bogus package in requirements.txt, the
                    # operator sees it. Better than bouncing forever on a
                    # transient network issue or wheel-unavailable case.
                    result["env_broken"] = True
                    result["passed"] = False
                    result["output"] = (
                        f"pip install -r {req_name} failed "
                        f"(exit={ri.returncode}):\n{install_out[:2000]}"
                    )
                    result["first_failure"] = f"pip install -r {req_name} failed"
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
        # go: "--- FAIL: TestFoo"
        first = ""
        for ln in run_out.splitlines():
            if ln.startswith("FAILED ") or ln.startswith("FAIL ") \
                    or "--- FAIL:" in ln:
                first = ln[:200]
                break
        result["passed"] = False
        result["first_failure"] = first or "see test output below"
        return result
    except Exception as e:
        # Tooling failure (timeout, subprocess.SubprocessError, etc.) →
        # treat as env_broken (don't blame the coder). Caller alerts.
        result["env_broken"] = True
        result["passed"] = False
        result["output"] = f"_post_coder_test_check tooling error: {e}"
        return result


def _run_post_coder_pipeline(product: dict, session_uid: str, working_dir: str,
                              assigned_features: list[dict]) -> list[int]:
    """
    Deterministic git fallback after the coder LLM exits cleanly.
    The coder ONLY writes code; this function pushes to the sprint branch:
      1. Detect if there are any changes in the workspace
      2. Check out the sprint branch (provisioned at sprint activation)
      3. git add + commit + push
      4. PATCH each assigned feature to Reviewing + pr_number on the PM API
         directly (no session_result.json roundtrip; see step 5 comment for
         why the file-based handoff was removed on 2026-05-06).

    Returns the list of feature IDs successfully PATCHed to Reviewing — used
    by the caller to set the session record's `features_pushed` counter.

    Sprint-PR mode is the only supported flow: PR creation happens once at
    sprint activation (`orchestrator.sprint_pr.provision_sprint_pr`); coder
    sessions just stack commits onto the same branch. If the active sprint
    has no provisioned branch + PR, the pipeline marks the assigned features
    Blocked with a clear reason — never opens a fresh PR.
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
        # Pop timeout from kw so the caller's override doesn't collide with the
        # default we pass into _sp.run. Without this, e.g. _run(..., timeout=180)
        # raises TypeError("got multiple values for keyword argument 'timeout'")
        # — which crashes the whole pipeline before our diagnostic checks run.
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout, **kw)

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

    # Belt-and-braces: if local HEAD is on a coder/* or sprint/* branch, that
    # is evidence _reset_workspace didn't land cleanly. Force checkout to the
    # true default branch before continuing — agent's working-tree edits are
    # restashed-and-popped so they survive the switch. Loud warning so the
    # operator can chase the _reset_workspace failure separately.
    _hb = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    current_head = (_hb.stdout or "").strip()
    if current_head.startswith(("coder/", "sprint/")):
        log.warning(
            f"[post-coder] {pname}: HEAD is on {current_head!r} entering "
            f"post-coder (should have been {default_branch!r} after "
            f"_reset_workspace) — forcing checkout. This indicates "
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

            # Reconciler-as-controller (opt-in via RECONCILER_CHORES_ENABLED).
            # Objective code-drift detectors (separate _CHORE_DETECTORS registry,
            # not the comment-path _DETECTORS above) emit high-severity findings
            # that get filed as Approved chore features. The existing
            # coder→guard→reviewer pipeline is the actuator, so corrections run
            # through the same verification as any feature. Default OFF — flip
            # the flag per-environment to A/B on a product. Best-effort: failure
            # here never bounces the feature.
            if os.environ.get("RECONCILER_CHORES_ENABLED", "").strip().lower() \
                    in ("1", "true", "yes", "on"):
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
    lint_violations = _post_coder_lint_check(working_dir, _run, pname)
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
    test_result = _post_coder_test_check(working_dir, _run, pname)
    if test_result.get("framework") != "none" and not test_result.get("passed"):
        env_broken = test_result.get("env_broken", False)
        first_failure = test_result.get("first_failure", "")
        collection_errors = test_result.get("collection_errors", 0)
        output_excerpt = (test_result.get("output") or "")[:1500]
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
                            body = (
                                f"❌ post-coder test-check auto-reject ({reason}):\n"
                                f"First failure: `{first_failure}`\n\n"
                                f"```\n{output_excerpt[:800]}\n```\n"
                                f"Fix the failing test(s) and re-push. If tests reference "
                                f"symbols that don't exist (collection error), align the "
                                f"test file with the actual code or delete the orphan test."
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
