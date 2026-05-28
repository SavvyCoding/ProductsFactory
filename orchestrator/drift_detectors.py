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
# Orchestration
# ────────────────────────────────────────────────────────────────────────────


_DETECTORS = (
    detect_shell_artifact_files,
    detect_design_doc_mismatch,
    detect_placeholder_template_content,
)


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


def post_findings(
    findings: Iterable[Finding],
    pm_client,
    product_name: str = "?",
) -> int:
    """POST each finding as a feature_comment with author="drift-scanner".

    Returns the count of successfully posted findings. Best-effort —
    individual POST failures are logged and don't abort the loop.

    Dedupe is deferred to a follow-up: re-running the same detectors
    will post the same findings again on the next cycle. For the spike,
    rely on the comment being a duplicate the reader can ignore;
    real dedupe (by category + target_id within a 24h window) wants a
    PM API query we don't have yet.
    """
    posted = 0
    for f in findings:
        try:
            resp = pm_client.post(
                f"/api/features/{f.feature_id}/comments",
                json={"author": "drift-scanner", "body": f.as_comment_body()},
            )
            if 200 <= resp.status_code < 300:
                posted += 1
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
    return posted
