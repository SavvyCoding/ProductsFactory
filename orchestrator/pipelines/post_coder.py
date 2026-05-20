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

    if not files:
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
        violations.append(
            f"state-changing route(s) without recognized auth check: "
            f"{', '.join(auth_missing[:3])}{'...' if len(auth_missing) > 3 else ''}. "
            f"Add verifyAuth / verify_auth / equivalent (see ARCHITECTURE.md "
            f"REFERENCE PATTERNS) or annotate first line with `// PUBLIC_ROUTE: <reason>`."
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
        # 13c: empty source file
        try:
            full = _PP(working_dir) / f
            if (any(f.endswith(ext) for ext in _SOURCE_EXTENSIONS)
                    and full.is_file() and full.stat().st_size == 0):
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
        # 13e: test_X_qa.py pair when test_X.py already exists
        m = _re.match(r"^(.*?test_[A-Za-z0-9_]+)_qa(\.[A-Za-z]+)$", fname)
        if m:
            base = m.group(1) + m.group(2)
            try:
                if ((_PP(working_dir) / f).parent / base).exists():
                    qa_pair_hits.append(f"{f} (paired with existing {base})")
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
            f"test_X_qa.py pair(s) when test_X.py already exists: "
            f"{', '.join(qa_pair_hits[:3])}{'...' if len(qa_pair_hits) > 3 else ''}. "
            f"The QA reviewer should EDIT the existing test file, not create a "
            f"parallel _qa.py. Merge the new assertions into the original test "
            f"and delete the _qa file."
        )

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
        has_with = _re.search(r"with\s+\w+(_connection|get_connection|_conn)\s*[(\[]", content) \
                   or "with get_db_connection" in content
        has_raise = "raise" in content
        has_finally_close = _re.search(r"finally\s*:[^}]*?\.close\(\)", content)
        if has_raise and not has_with and not has_finally_close:
            conn_leak_hits.append(f)
    if conn_leak_hits:
        violations.append(
            f"potential DB connection leak (open + raise without `with` or "
            f"`finally: close`): {', '.join(conn_leak_hits[:3])}"
            f"{'...' if len(conn_leak_hits) > 3 else ''}. "
            f"Heuristic — verify; if false positive, refactor to `with "
            f"get_db_connection() as conn:` to make the check unambiguous."
        )

    return violations


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
    # 1-PR model: every coder session opens its own session PR with
    # base=<default branch>. There is no sprint integration branch and no
    # sprint PR. The `sprint_pr_mode` flag retains its old name for
    # backwards compatibility but now means "open a session PR per coder
    # run"; with it off, the bare-branch-no-PR legacy path warns + bails.
    sprint_pr_mode = bool(product.get("_sprint_pr_mode"))

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
    # thread). Applies under sprint_pr_mode AND legacy per-feature mode
    # because under the two-tier model the feature's `pr_number` is the
    # session PR (its base is the sprint branch), not the sprint PR itself.
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

    # Branch resolution. Three modes, evaluated in priority order:
    #   1. rework_pr_mode — features share an open session PR, force-push to
    #      its branch so the reviewer's existing comment thread carries over.
    #   2. sprint_pr_mode — cut a fresh `coder/<session_uid>` from the
    #      default branch tip and open a session PR (base=default_branch,
    #      head=coder/<uid>). Each coder session ships ONE session PR
    #      directly to main on merge. Sprint is a planning bucket only.
    #   3. legacy bare-branch — push coder/<session_uid> with no PR.
    #      Dead path; the PR-resolution step below warns + bails.
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
    elif sprint_pr_mode:
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
    else:
        branch = f"coder/{session_uid}"
        co = _run(["git", "checkout", "-b", branch])
        if co.returncode != 0:
            log.warning(f"[post-coder] {pname}: git checkout -b {branch} failed — {_fmt_err(co)}")
            return pushed_ids

    # 3. Add + commit + push
    add_r = _run(["git", "add", "-A"])
    if add_r.returncode != 0:
        log.warning(f"[post-coder] {pname}: git add -A failed — {_fmt_err(add_r)}")
        return pushed_ids
    # Sanity: was anything actually staged? `diff --cached --quiet` exits 1 if
    # there are staged changes, 0 if none. Catches the "porcelain showed lines
    # but add staged nothing" scenario (e.g. all changes inside a submodule or
    # excluded path) so we surface a clear error instead of an empty stderr.
    cached = _run(["git", "diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        ls = _run(["git", "status", "--porcelain"])
        log.warning(
            f"[post-coder] {pname}: nothing staged after `git add -A` "
            f"despite {len(changed)} porcelain entries. status={ls.stdout.strip()[:400]!r}"
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
    elif sprint_pr_mode:
        # Fresh session branch — set upstream so subsequent rework cycles can
        # detect it via the rework path above.
        push_args = ["--no-verify", "-u", "origin", branch]
    else:
        push_args = ["--no-verify", "-u", "origin", branch]
    from orchestrator.integrations.git_ops import git_push_authenticated
    push_result = git_push_authenticated(push_args, cwd=working_dir, product_name=pname, timeout=180)
    if push_result.returncode != 0:
        log.warning(f"[post-coder] {pname}: git push failed: {push_result.stderr.strip()[:300]}")
        return pushed_ids
    log.info(f"[post-coder] {pname}: pushed branch {branch}")

    # 4. PR resolution. Three paths:
    #   - rework_pr_mode: reuse the existing open session PR we just force-
    #     pushed to (preserves the reviewer's comment thread).
    #   - sprint_pr_mode (fresh session): open a new session PR with
    #     base=<default branch>, head=coder/<session_uid>. The session PR
    #     is the reviewable unit; on approval auto-merge merges it
    #     directly into main. Sprint is a planning bucket only.
    #   - legacy bare-branch: dead path. Warn + bail.
    if rework_pr_mode:
        pr_number = int(rework_pr_number)  # type: ignore[arg-type]
        pr_url = rework_pr_url
        log.info(f"[post-coder] {pname}: reusing rework PR #{pr_number} — {pr_url}")
    elif sprint_pr_mode:
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
        _sprint_ctx = (product.get('_active_sprint') or {}).get('name') or "(no active sprint)"
        pr_body = (
            f"Session `{session_uid}` — sprint: {_sprint_ctx}\n\n"
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
    else:
        log.warning(
            f"[post-coder] {pname}: bare-branch mode (sprint_pr_mode=False) "
            f"no longer supported — branch {branch} was pushed but no PR "
            f"will be opened. Flip sprint_pr_mode=True on the product."
        )
        return pushed_ids

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
                        client.patch(
                            f"/api/features/{fid}",
                            json={
                                "status": "Implementing",
                                "review_outcome": "changes_requested",
                                "changed_by": "post-coder:lint-guard",
                            },
                        )
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
                    # supervisor.detect_invalid_status_combos sees the stale
                    # `Reviewing + changes_requested` combo and demotes the
                    # feature back to Implementing within ~30s of every push,
                    # blocking forward progress and bumping fix_attempts toward
                    # auto-Block. Real incident 2026-05-10 01:21: feature #379
                    # cycled coder→Reviewing→supervisor-demote 4 times in 50min.
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
