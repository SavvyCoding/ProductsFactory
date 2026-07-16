"""Prompt builder — selects the right template and fills in product context."""

import os
import re
from pathlib import Path


def _load_hard_rules() -> str:
    """
    Shared anti-pattern block injected into every persona prompt via the
    `{hard_rules}` placeholder. Single source of truth for the 9 don'ts
    (apologise, fabricate paths, defensive bloat, bundle commits, fluff
    comments, exploratory shell, claim-without-evidence, re-read after
    write, agent-side git/PR). Centralising the list lets us tune the
    rules in one place; previously these were scattered across designer/
    coder/reviewer/architect prompts with drift.

    Read at module-import time (cheap, ~1 KB), so prompt builds don't pay
    disk cost per call.
    """
    p = Path(__file__).parent / "_hard_rules.md"
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


_HARD_RULES = _load_hard_rules()


def _read_reviewer_patterns(working_dir: str, max_patterns: int = 10) -> str:
    """
    Extract `Pattern:` lines that reviewers wrote into session_summary.md and
    format them as a markdown block for injection into agent prompts.

    The 200-char-per-line cap and 10-pattern total cap keep prompt growth
    bounded (~2 KB max) regardless of how long the file gets.
    """
    if not working_dir:
        return ""
    summary = Path(working_dir) / "session_summary.md"
    if not summary.exists():
        return ""
    try:
        text = summary.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    patterns: list[str] = []
    seen: set[str] = set()
    # Walk reversed so the newest patterns survive when we hit the cap.
    for line in reversed(text.splitlines()):
        m = re.match(r"^\s*Pattern:\s*(.+)$", line.strip())
        if not m:
            continue
        body = m.group(1).strip()[:200]
        if body in seen:
            continue
        seen.add(body)
        patterns.append(body)
        if len(patterns) >= max_patterns:
            break
    if not patterns:
        return ""
    patterns.reverse()  # chronological — oldest first, newest last
    bullets = "\n".join(f"- {p}" for p in patterns)
    return (
        "## Known patterns from prior sessions\n\n"
        "Past reviewers recorded these insights about this codebase. "
        "Read them BEFORE making changes — they prevent recurring failures.\n\n"
        f"{bullets}\n\n---\n"
    )


# Appended to every persona prompt when backend == "ollama".
# Local 30B models (qwen3-coder, gemma3) need stronger reinforcement on:
#   - calling task_done() before exit
#   - sticking to the assigned feature list (not querying for more work)
#   - output format (one JSON line per feature, no wrapping)
# Safe to include for Claude too — just slightly more verbose.
_OLLAMA_ADDENDUM = """

---

## ⚡ Ollama execution contract (local model — read carefully)

You are running on a local model (qwen3-coder, gemma3, etc.) with a hard turn
limit. The base prompt above tells you what to do; this addendum nails down
HOW to do it on this backend specifically:

1. **Tool names on this backend:** `bash`, `read_file`, `write_file`,
   `http_request`, `task_done`. There is no `Write` or `Edit` tool — use
   `write_file` for any code change. There is no `Read` — use `read_file`.

2. **Stick to the assigned feature list.** Do NOT query the PM API for more
   work. If the list is empty, call `task_done(status="success", summary="no work")`
   immediately.

3. **Do NOT run git or gh.** The orchestrator owns all git/PR ceremony on
   this backend. Your only job is to edit files and append entries to
   `session_result.json`. After your session exits, the orchestrator
   stages, commits with the right `[feature-<id>]` tag, pushes, and
   updates the PM API. Running git yourself produces orphan local commits
   that get wiped on the next workspace reset.

4. **Touching `session_result.json` is required.** Append one line per
   assigned feature with `"status": "Implemented"` (coder), `"Designed"`
   (planner / designer), or `"Blocked"` with a `blocked_reason`. Never
   write `"Reviewing"` — that's the orchestrator's downstream state, not
   yours. The reviewer persona writes its own session_result.json with
   `"Reviewed"` entries.

5. **Call `task_done()` BEFORE your turn budget runs out.** Statuses:
   - `success` — all assigned work done
   - `blocked` — cannot proceed (one-line `summary`)
   - `incomplete` — partial progress (note what's done in `summary`)
   Exiting without `task_done` counts as a failure.

6. **One tool call per turn is fine** — don't batch. Use `bash` for `pytest`,
   `ls`, `mkdir`, `mv`, `rm`, `curl`, `head`, `grep`, `find`. NEVER use
   `sed -i` or `awk -i` to edit code — they corrupt indentation. Use
   `write_file` to overwrite the whole file instead.

7. If a tool call fails, read the error and adjust ONE thing. Don't re-run
   the same failing command twice.
"""


# ---------------------------------------------------------------------------
# code_auditor output modes (Increment 1 of the filing promotion, 2026-06-25).
#
# The audit (Steps 0-3) is identical in both modes; only the SINK differs, so
# the prompt body carries a single {code_auditor_output_steps} placeholder and
# the builder injects exactly one of these. Keeping the switch deterministic
# (builder picks the block) rather than agent-branched ("if filing is on...")
# is what stops a local model from doing the wrong half on a bad turn.
#
#   comment-only (default)  -> raise dashboard alerts, file nothing (soak)
#   filing (CODE_AUDITOR_FILING_ENABLED) -> file each finding as a `bug`
#       feature, status routed by severity (2026-07-16): Critical/High land
#       `Approved` (auto-triaged straight into the coder queue for fast repair);
#       Medium/Low land `Pending` (PM triages first — a false positive there
#       costs one rejection, not a session). Auto-approving the top two tiers
#       trades the human gate for speed on the findings that matter most; the
#       Increment-2 verifier session is the eventual compensating control.
# ---------------------------------------------------------------------------
_CODE_AUDITOR_ALERT_STEPS = """## Step 4 — Raise each surviving finding as a dashboard alert

Run mode is **comment-only**. Raise **at most 8** findings, highest-severity
first. For EACH one:

```
POST {pm_api_url}/api/alerts
{"product_id": {product_id},
 "message": "[code-audit][<SEVERITY>][<dimension>] <file:line> — <one-line essence>. Fix: <one-line fix>"}
```

Keep each `message` to a single line. Lead with `[code-audit]` so the operator can
filter the soak's output. Do NOT create or modify features or files — alerts only.

---

## Step 5 — Finish

Post one final summary alert:

```
POST {pm_api_url}/api/alerts
{"product_id": {product_id},
 "message": "[code-audit][summary] {product_name}: raised N finding(s) — <C critical / H high / M medium / L low>."}
```

Then stop. You wrote no files and filed no features — that is correct for this mode."""

_CODE_AUDITOR_FILING_STEPS = """## Step 4 — File each surviving finding as a `bug` feature

Run mode is **filing**. File **at most 8** findings, highest-severity first, as
bug features in the PM backlog — the coder pipeline fixes what you file, you
still write NO files yourself. ONE http_request per finding:

```
POST {pm_api_url}/api/features
{
  "product_id": {product_id},
  "name": "Code-review: <short specific title, max 8 words>",
  "description": "[<dimension>] <what + where: the defect and the file:line>\\n- <AC1: the observable correct behavior the fix must produce>\\n- <AC2: a Verify-able check — an HTTP status, a persisted row, a real assertion>\\n- <AC3 if needed>",
  "feature_type": "bug",
  "source": "ai",
  "priority": <PRIORITY>,
  "status": "<STATUS>"
}
```

Set `priority` by severity (LOWER number = more urgent): **Critical → 1,
High → 5, Medium → 20, Low → 40.**
Set `status` by severity: **Critical and High → `Approved`** — they skip PM
triage and the coder pipeline fixes them immediately; **Medium and Low →
`Pending`** — the PM triages those before any coder session. Every finding MUST
quote the actual code (`file:line`) and carry at least two checkable ACs — no
speculative findings. A Critical/High false positive now costs a real coder
session (no human triage catches it), so do NOT file one you could not refute
in Step 3. If you already saw this issue as an open bug or unread alert in
Step 0, do NOT re-file it.

---

## Step 5 — Finish

Post one final summary alert so the run is visible on the dashboard:

```
POST {pm_api_url}/api/alerts
{"product_id": {product_id},
 "message": "[code-audit][summary] {product_name}: filed N bug(s) — <C critical / H high / M medium / L low>."}
```

Then stop. You wrote no files — you only filed bug features and one summary alert."""


def _code_auditor_filing_on(product: dict) -> bool:
    """Increment 1: resolve the code_auditor filing flag.

    Precedence (mirrors deploy/orchestrator/tools._code_auditor_enabled):
      1. product.config["code_auditor_filing"] explicit bool wins (per-product).
      2. else CODE_AUDITOR_FILING_ENABLED env — ON only if truthy.
    Default OFF — comment-only soak until an operator flips it.
    """
    cfg = (product.get("config") or {}).get("code_auditor_filing")
    if isinstance(cfg, bool):
        return cfg
    return os.environ.get("CODE_AUDITOR_FILING_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def build_prompt(product: dict, session_uid: str, persona: str | None = None, max_features: int | None = None, backend: str = "claude") -> str:
    """
    Returns the full Claude prompt string for this product session.

    Persona routing:
      designer  → designer.md
      reviewer  → reviewer.md
      coder     → greenfield.md or brownfield.md (existing coder path)
      None      → legacy routing (analysis_run / brownfield / greenfield)
    """
    # "retrospective" persona is gone — defensive alias for any legacy
    # caller that still asks for it; returns the planner template.
    if persona == "retrospective":
        template_name = "planner"
    elif persona == "designer":
        template_name = "designer"
    # "product_planner" was merged into "designer" on 2026-05-06 (Phase 1
    # of futureplan.md — they shared the same filter and wrote
    # near-identical per-feature docs). Route any lingering callers to the
    # designer prompt rather than 404 the build.
    elif persona == "product_planner":
        template_name = "designer"
    elif persona == "reviewer":
        template_name = "reviewer"
    elif persona == "recommender":
        template_name = "recommender"
    elif persona == "planner":
        template_name = "planner"
    # Phase 2 (2026-05-06): qa_tester and security_auditor were merged
    # into reviewer. The merged reviewer's prompt covers tri-section
    # review (functional + tests + security). Defensive aliases — if
    # anything still routes one of those persona names here, fall back
    # to the reviewer prompt rather than 404 the build.
    elif persona == "qa_tester":
        template_name = "reviewer"
    elif persona == "security_auditor":
        # Wave-6 (2026-06-13): revived as a STANDING, read-only, product-wide
        # security audit that files findings as `bug` features — distinct
        # from the per-PR security review that was merged into `reviewer`
        # on 2026-05-06 (that merge is unchanged). PM-triggered on-demand
        # only; never auto-scheduled.
        template_name = "security_auditor"
    elif persona == "code_auditor":
        # Phase-boundary whole-product review (2026-06-24). Read-only,
        # product-wide, triggered when a phase settles (tools._run_phase_review_gate).
        # Comment-only soak mode: raises findings as dashboard alerts, files no
        # bugs. Distinct from per-PR reviewer (one diff) and drift detectors
        # (pattern matchers) — this is the cross-cutting semantic pass.
        template_name = "code_auditor"
    elif persona == "audit_verifier":
        # Fix 2b (2026-07-27): independent, read-only verifier that re-checks
        # newly-filed audit findings against the code on a DIFFERENT model and
        # Rejects the false positives before they reach a coder. Queued by
        # tools._run_audit_verifier_gate when unverified audit bugs exist.
        template_name = "audit_verifier"
    elif persona == "documenter":
        template_name = "documenter"
    elif persona == "refactorer":
        template_name = "refactorer"
    elif persona == "devops":
        template_name = "devops"
    elif persona == "analytics":
        template_name = "analytics"
    elif persona == "product_trainer":
        template_name = "product_trainer"
    elif persona == "architect":
        # Phase 8 of quality-specs (2026-05-19): quantitative drift detection
        # against ARCHITECTURE.md's MODULES / ENTRY POINTS / CONFIG GATES
        # sections. Maintenance persona — read-only on source, may write
        # /workspace/docs/architecture_review_*.md and product_memory.md.
        # Schedule: every ~3 features pushed (default N, _check_architect_due
        # in tools.py), or on-demand via run_persona_now="architect".
        template_name = "architect"
    elif persona == "coder":
        # Coder always uses brownfield.md regardless of product.type. The
        # greenfield/brownfield distinction was originally meant to give a
        # truly-empty scaffolding-time repo a simpler prompt, but in practice
        # every product accumulates real code within hours of discovery and
        # the brownfield prompt's "preserve existing code" guidance applies
        # vacuously to empty repos (nothing to preserve). Meanwhile, ALL the
        # coder-quality structural fixes have been shipped to brownfield.md
        # over the 2026-05-30..06-01 watch loop: HARD STOP no-skips,
        # self-rolled mock class ban, runtime-tool availability check, full-
        # suite before task_done, rework branch persistence, last-feedback-
        # only block, anti-doc-only-commit guidance. The greenfield template
        # accumulated none of these. Canonical 2026-06-01 incident:
        # DocumentSign carried product.type='greenfield' from initial
        # scaffolding despite having 200+ files; its coders read greenfield.md
        # and silently ignored every structural improvement to brownfield.md
        # — 6 hours of 0 Pushed traced directly to this routing bug.
        # Note: analysis_run uses a different code path (template_name set
        # above for `analysis_status == running`) and is not affected.
        template_name = "brownfield"
    elif product.get("analysis_status") == "running":
        template_name = "analysis_run"
    else:
        # 2026-06-11: greenfield.md retired entirely. The persona="coder"
        # branch above already routed all production coder sessions to
        # brownfield; this legacy persona=None fall-through (scripts /
        # evals / local Ollama runs) was the one remaining path that could
        # serve the drifted zombie template — missing {hard_rules}, the
        # HARD STOP block, rework-branch logic, and every 2026-05/06 coder
        # fix, with a rework header docker_runner no longer emits. One
        # accidental re-route away from mass regression; now structurally
        # impossible. greenfield.md was deleted outright (dead-code sweep).
        template_name = "brownfield"

    # Per-product prompt overrides (Symphony-style WORKFLOW.md pattern):
    # if a product wants its own prompt for any persona, it can drop a file at
    # `<working_dir>/.productfactory/prompts/<persona>.md`. This file is
    # version-controlled with the product code, hot-reloaded on every call
    # (no orchestrator restart required), and falls back to the baked-in
    # default when absent. Lets product teams iterate on their own agent
    # contracts without rebuilding the orchestrator image.
    _baked_path = Path(__file__).parent / f"{template_name}.md"
    template_path = _baked_path
    _override_source = "baked-in"
    _wd = product.get("working_dir", "")
    if _wd:
        try:
            from orchestrator.paths import container_path as _container_path
            _wd_resolved = _container_path(_wd)
        except Exception:
            _wd_resolved = _wd
        _override_path = Path(_wd_resolved) / ".productfactory" / "prompts" / f"{template_name}.md"
        if _override_path.is_file():
            template_path = _override_path
            _override_source = f"override:{_override_path}"
    template = template_path.read_text(encoding="utf-8")
    # Note: log line is at INFO so the override is visible in `docker logs
    # pf-orchestrator` — useful when debugging "why is my custom prompt not
    # being picked up". Falls back silently when no override exists (the
    # common case).
    if _override_source != "baked-in":
        import logging as _logging
        _logging.getLogger("orchestrator.prompts").info(
            "[prompt] persona=%s template=%s source=%s",
            persona or "<default>", template_name, _override_source,
        )

    # Use explicit replacement instead of str.format() so that JSON examples
    # like {"status": "..."} in the templates are not misinterpreted as placeholders.
    prev = product.get("_prev_session_summary", "")

    # Load product_memory.md if it exists (limit to last 3000 chars)
    _memory_content = ""
    _working_dir = product.get("working_dir", "")
    if _working_dir:
        _mem_file = Path(_working_dir) / "product_memory.md"
        if _mem_file.exists():
            try:
                _mem_text = _mem_file.read_text(encoding="utf-8").strip()
                if _mem_text:
                    if len(_mem_text) > 3000:
                        _mem_text = "...[earlier entries truncated]\n\n" + _mem_text[-3000:]
                    _memory_content = f"## Product memory (cross-session knowledge)\n\n{_mem_text}\n\n---\n"
            except Exception:
                pass

    # Declared sidecar services (service provisioning, 2026-06-12): the
    # fakes-first rule in designer.md tells agents to "check the declared
    # services list" — but that list lives in product.config (DB), invisible
    # from inside the workspace. Render it into the prompt explicitly, with
    # the env var carrying each connection URL. Root cause: DogTinder #1582
    # redesign still hardcoded localhost:6379 because the designer had no
    # way to know redis was declared or that REDIS_URL exists.
    _svc_block = ""
    try:
        from orchestrator.services import SERVICE_CATALOG
        _declared = [s for s in ((product.get("config") or {}).get("services") or [])
                     if s in SERVICE_CATALOG]
        if _declared:
            _svc_lines = "\n".join(
                f"- **{s}** — a live per-session instance is provisioned for every "
                f"agent/test/verify container; connect ONLY via the "
                f"`{SERVICE_CATALOG[s]['env_var']}` environment variable. NEVER "
                f"hardcode `localhost:{SERVICE_CATALOG[s]['port']}` or any host:port — "
                f"the service is NOT on localhost."
                for s in _declared
            )
            _svc_block = (
                "## Declared live services (USE THESE — do not fake, do not vendor)\n\n"
                f"{_svc_lines}\n\n---\n"
            )
    except Exception:
        _svc_block = ""

    replacements = {
        "{product_id}": str(product["id"]),
        "{product_name}": str(product.get("name", product["working_dir"])),
        "{session_uid}": str(session_uid),
        "{pm_api_url}": str(os.environ.get("PM_API_URL_CONTAINER", os.environ["PM_API_URL"])),
        "{tech_stack}": ", ".join(product.get("tech_stack") or []),
        "{max_features_per_run}": str(
            max_features if max_features is not None else int(os.environ.get("MAX_FEATURES_PER_SPRINT", "5"))
        ),
        "{auto_merge_enabled}": str(product.get("_auto_merge_enabled", False)),
        # Session-PR context (1-PR model). Populated for the reviewer
        # only — coder/designer don't use them. Empty string falls through
        # cleanly when the substitution isn't relevant (e.g. designer).
        "{session_branch}": str(product.get("_session_branch", "") or ""),
        "{session_pr_number}": str(product.get("_session_pr_number", "") or ""),
        "{session_pr_url}": str(product.get("_session_pr_url", "") or ""),
        "{assigned_features}": product.get("_assigned_features_md", ""),
        "{assigned_feature_count}": str(len(product.get("_assigned_features", []))),
        # Reviewer/security_auditor comments for features in a rework cycle.
        # Empty for fresh first-pass assignments. Wired only for coder
        # (docker_runner._format_reviewer_feedback returns "" for other
        # personas). Closes the reviewer→coder feedback gap surfaced on
        # 2026-05-06: reviewer 1975 left specific line-numbered comments
        # that coder 1976 never saw because the prompt didn't render them.
        "{reviewer_feedback}": product.get("_reviewer_feedback_md", ""),
        # Phase 7 of quality-specs (2026-05-19): pre-coder context augmentation.
        # See orchestrator/session/context_builder.py. Empty for non-coder
        # personas; empty for coder when ARCHITECTURE.md MODULES has no rows
        # that match the feature's keywords. When populated, the block lists
        # canonical modules + AST-discovered exports for the area, with
        # explicit "do not parallel these" framing — closes the multi-agent
        # drift pattern that produced StockAnalysis's two user-stores and
        # Calculator's five main.py variants.
        "{related_existing_code}": product.get("_related_existing_code_md", ""),
        "{prev_session_summary}": (
            f"## Previous session context\n\n{prev}\n\n---\n" if prev else ""
        ),
        "{product_memory}": _memory_content,
        "{reviewer_patterns}": _read_reviewer_patterns(_working_dir),
        "{hard_rules}": _HARD_RULES,
        "{declared_services}": _svc_block,
    }
    # code_auditor sink (filing vs comment-only) is pre-substituted BEFORE the
    # main loop because the injected block itself contains {pm_api_url} /
    # {product_id} / {product_name}; the loop below then resolves those. No-op
    # for other personas (their templates lack this placeholder).
    template = template.replace(
        "{code_auditor_output_steps}",
        _CODE_AUDITOR_FILING_STEPS if _code_auditor_filing_on(product)
        else _CODE_AUDITOR_ALERT_STEPS,
    )
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)

    # Append Ollama-specific reinforcement when running on a local model.
    # Harmless but slightly verbose for Claude; portable.
    if backend == "ollama":
        template = template + _OLLAMA_ADDENDUM

    return template
