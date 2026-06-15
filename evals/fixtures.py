"""Canonical fixtures used by tier-1 (contract) and tier-2 (live) evals.

Keep these fixtures small and stable — every time they change, all historical
eval results become incomparable.
"""
from __future__ import annotations


# Minimal product dict shaped like what poller._normalize_product returns.
SAMPLE_PRODUCT: dict = {
    "id": 101,
    "name": "Calculator",
    "working_dir": "/workspace",
    "github_repo": "https://github.com/example/calculator",
    "type": "greenfield",
    "tech_stack": ["python", "fastapi"],
    "analysis_status": "done",
    "_assigned_features": [
        {"id": 42, "name": "Add divide endpoint", "status": "Designed"},
    ],
    "_assigned_features_md": "- #42 Add divide endpoint (Designed)",
    "_auto_merge_enabled": False,
}


SAMPLE_SESSION_UID = "eval-session-0001"


# Mapping persona → (tier-1 required substrings in the built prompt).
# These are non-negotiable invariants the prompt MUST satisfy, otherwise the
# session behaviour changes dangerously.
PROMPT_INVARIANTS: dict[str, list[str]] = {
    # Coder personas: behavioral guarantees that must survive any prompt
    # rewrite. Each phrase locks in a specific directive — losing one
    # changes session behaviour.
    # ("greenfield" entry removed 2026-06-11: the template is retired to a
    # pointer stub; ALL coder routing — including the legacy persona=None
    # type-based fall-through — lands on brownfield.md.)
    "brownfield": [
        "session_result.json",
        "Implemented",
        "Blocked",
        "Reviewing",
        "task_done",
        "sed -i",
        "git",
        "gh ",
        "session_summary.md",
        # Rework-gap rules added 2026-06-15 from the cross-product feedback
        # audit (~1k reviewer/gate rejections). Each locks in a directive with
        # zero prior coverage; losing one re-opens its rejection class.
        "is real, not a stub",            # R1: implementation-is-a-stub reject
        "wired end-to-end and fails closed",  # R2: dead-at-runtime / swallow-and-proceed
        "request-controlled identifiers",  # R3: SQL injection via dynamic column names
        "never deleting/`.skip`-ing the test",  # R5: delete-the-failing-test shortcut
    ],

    # Designer must produce a design doc in docs/ so the coder can pick it up,
    # and must use session_result.json (not the PM API) for status updates.
    "designer": [
        "docs/",                       # canonical design-doc location
        "design",                      # role of the persona
        "Designed",                    # success status string
        "Blocked",                     # failure status string
        "session_result.json",         # status reporting channel
        "PATCH",                       # must remind not to call PATCH /api/features
        "design_doc_path",             # JSON key the orchestrator reads
        "AGENT_WORKFLOW",              # must warn against following the coder workflow
        "session_summary.md",          # progress logging path
        "product_memory.md",           # cross-session findings path
        "git",                         # the no-git prohibition must mention git
        # Shared-entrypoint cohesion: the sizing gate must NOT split a
        # foundation whose children co-create the same new file (the 2026-
        # 06-13 IndianFoodTruck NextAuth cascade). Losing this re-opens the
        # colliding-PR failure class.
        "COHESION",
        "[...nextauth].ts",
    ],

    # Reviewer behavioral guarantees. Each phrase locks in a specific
    # directive — losing one changes session behaviour and risks the
    # known failure modes (infinite reviewing loop, hybrid Reviewed+
    # changes_requested state, write-restricted personas writing files).
    "reviewer": [
        "Reviewed",                 # success status
        "Implementing",             # changes-requested status (NOT "Reviewing")
        "Reviewing",                # must remind model NOT to write this
        "approved",                 # review_outcome value
        "changes_requested",        # review_outcome value
        "session_result.json",      # write channel
        "PATCH",                    # must forbid PATCH /api/features
        "write_file",               # must call out the forbidden tool
        "sed -i",                   # must forbid in-place edit
        "git commit",               # must forbid git mutation
        "/api/features",            # must reference the PM API
        "comments",                 # must reference the comments endpoint
        "[feature-",                # must reference the per-feature commit tag
        "functional",               # tri-section review
        "security",                 # tri-section review
        "QA/Tester gate",           # tests run in the deterministic QA gate, not the reviewer
        # "Decision before task_done" rule — added 2026-05-09 after the
        # silent-task_done loop on feature #395. The reviewer model hit
        # Ollama 500s and exited via task_done WITHOUT writing a decision,
        # leaving the feature in Reviewing forever — infinite redispatch.
        # The prompt must require a fallback decision so the queue moves.
        "MANDATORY",
        "review_notes",
        "Review incomplete",
    ],

    # Planner: under the flat phases→features model (migration 043, 2026-05-26)
    # the planner creates Stories grouped into a Phase. It must POST features
    # in Pending status (PM gate) and reference the phases + features
    # endpoints. Pre-2026-05-26 invariants ("sprint", "/api/sprints") are
    # retired with the sprints layer.
    "planner": [
        "Story",                      # vocabulary the prompt teaches
        "phase",                      # phases→features flat model
        "/api/phases",                # phase create endpoint
        "/api/features",              # story POST endpoint
        "Pending",                    # stories must be created in Pending state
        "ONE new Feature",            # the one-feature-per-session rule
    ],

    # Recommender — generates Pending feature suggestions. Must check
    # backlog cap to avoid spamming, must POST features (not sprints),
    # must limit web searches.
    "recommender": [
        "/api/features/count",        # backlog-cap check
        "Pending",                    # backlog-cap is on Pending status
        "/api/features",              # POSTs features
        "duckduckgo",                 # uses DDG search
        "task_done",                  # explicit exit signal
        "Maximum 5 features",         # cap on suggestions
    ],

    # Refactorer — files chore features only. Must NOT write code, must
    # mark feature_type=chore at low priority.
    "refactorer": [
        "/api/features",
        "chore",                      # feature_type
        "priority",                   # priority is set
        "last_refactorer_at",         # config update
        "Do NOT write application code",
    ],

    # DevOps — mirrors refactorer but for infra. Same shape: chore
    # features, no code modification.
    "devops": [
        "/api/features",
        "chore",
        "Dockerfile",                 # infra audit category
        "CI/CD",                      # infra audit category
        "last_devops_at",             # config update
    ],

    # Documenter — must update README/CHANGELOG/ARCHITECTURE only.
    # Must commit/push these files (writes are expected for this persona).
    "documenter": [
        "README.md",
        "CHANGELOG.md",
        "ARCHITECTURE.md",
        "git commit",                 # this persona DOES commit
        "last_documenter_at",         # config update
    ],

    # Analytics — files high-value features and writes a report.
    "analytics": [
        "/api/features",
        "docs/analytics_",            # report path stem (the {session_uid} suffix gets rendered)
        "last_analytics_at",          # config update
        "git commit",                 # commits the report
    ],

    # Product Trainer — generates a video. Must write narration + run
    # the generator, then update product config.
    "product_trainer": [
        "narration.md",
        "generate_video.py",
        "last_product_trainer_at",
        "PM_API_URL",                 # the script reads this env
        "moviepy",                    # the dep that drives the bundle
    ],

    # NOTE: analysis_run.md is template-routed via product.analysis_status
    # ("running"), not via persona name. It can't be exercised by this
    # contract test's persona-based dispatch, so we don't add invariants
    # for it here. (If we ever want to cover it, the test harness needs
    # a special product fixture with analysis_status="running".)

    # Security auditor must not modify code — it only files bugs.
    "security_auditor": ["bug", "security"],

    # QA tester operates on the coder's open PR — it must push tests to that branch.
    "qa_tester": ["test"],
}
