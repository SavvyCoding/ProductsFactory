## Hard rules — agent anti-patterns (read first; they apply to EVERY persona)

These are the recurring failure modes the gate/supervisor layer catches AFTER the fact. Avoid them inline and you save 5–10 turns per session.

**A. Don't apologize, hedge, or restate the user's request.**
Phrases like "Let me think...", "I'll try to...", "As mentioned above..." burn turns and tokens and signal uncertainty when the assignment asked for action. State and act.

**B. Don't fabricate file paths, function names, or line numbers.**
Every path/symbol you mention in a tool call, commit message, or comment MUST have been observed via a prior tool call in THIS session. Hallucinated paths cascade into Guard 17 deletion-safety false positives and into reviewer "module not found" rejections. If you haven't `read_file`'d it or `bash ls`'d it in this session, you don't know it exists.

**C. Don't add error handling, fallbacks, or validation for cases that can't happen.**
Trust internal code and framework guarantees. Only validate at system boundaries (user input, external APIs). Defensive `try/except`/`if x is not None` bloat is a top-3 cause of coverage rot — pushes line count up, drives `--cov-fail-under` down on the gate.

**D. Don't bundle unrelated changes into one commit.**
If you find something else broken while doing your assigned feature, note it under `## Out-of-scope observations:` in `session_summary.md` and leave it. The `[feature-N]` commit-tag system breaks when one commit touches files belonging to two different features.

**E. Don't write comments explaining WHAT the code does.**
Names already say what. Comments are for WHY: a non-obvious constraint, a hidden invariant, a workaround citation. One short line max. Multi-paragraph docstrings on internal helpers get flagged as fluff.

**F. Don't run shell commands "to check" without a hypothesis.**
Every Bash call must complete the sentence: "I expect to see X because Y; I'll run Z to confirm." Random `ls` / `grep` / `find` exploration loops eat the turn budget before `task_done`. Read the relevant ARCHITECTURE.md sections + your prior tool output instead.

**G. Don't claim verification without evidence in tool output.**
`## AC<N> verification: OK` MUST be backed by an actual `pytest` / `curl` / `python -c` result quoted directly above. Cosmetic OK blocks without recipe output are the verify-check gate's primary fire trigger — they bounce the feature and waste a full cycle.

**H. Don't re-read a file you just wrote.**
`write_file` / `Edit` would have errored if the change failed. Re-reading to "verify" wastes a turn. Trust the tool's error contract.

**I. Don't deploy / push / merge / open PR from inside the session.**
The orchestrator owns all git, PR, and deploy ceremony. Your job ends at `task_done`. Running `git push` / `gh pr create` yourself produces orphan commits that get wiped on the next workspace reset.

**J. Don't make a check pass by weakening the check.** (The meta-rule — most gate bounces are a special case of this.)
Every gate is a PROXY for working software, never the target: a test, an auth guard, a bar in `quality_gates.json`, an AC `Verify:` recipe, a `PUBLIC_ROUTE:` annotation. The moment you find yourself editing the *check* rather than the *code under test* to go green — softening an assertion, adding `.skip`/`xit`, deleting a failing test, lowering a coverage/lint bar, annotating a route public to silence the auth guard, or returning a hardcoded constant the test happens to assert — STOP. That is the single failure mode every gate in this system exists to catch, and the lint-guard/reviewer catch it every time, costing you a full rework round. Two honest options only: change the real code until the check passes for the right reason, or mark the feature `Blocked` with the specific reason. An honest `Blocked` routes the work correctly; a gamed green is the only true failure. (The persona-specific sections below list concrete instances — they're all this one rule.)

---
