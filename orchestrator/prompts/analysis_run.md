You are running a one-time **ANALYSIS RUN** for **{product_name}** (product_id={product_id}).
This is a brownfield product. Understand the codebase and produce the documents all future sessions rely on.

Session ID: {session_uid}
PM API: {pm_api_url}
Tech stack: {tech_stack}
Working dir: `/workspace`.

---

## What to do (in order)

### 1. Map the codebase
Read all source files. Build a mental map of modules, patterns, naming conventions, test structure. Identify which tests currently pass and which fail.

### 2. Write `ARCHITECTURE.md` (≤800 tokens, self-trimming)
Must include: directory structure (existing source + test paths), key modules and responsibilities, patterns in use (DI approach, error handling, logging, DB access, etc.), naming conventions, pre-existing test failures (excluded from baseline gate), "Do not change without PM approval" list.

### 3. Write `product_config.json` at repo root
```json
{{
  "type": "brownfield",
  "existing_source_paths": ["..."],
  "existing_test_paths": ["..."],
  "new_feature_source": "...",
  "new_feature_tests": "...",
  "results_path": "Results/",
  "test_command": "...",
  "audit_command": "pip-audit | npm audit --audit-level=high",
  "baseline_tests": {{"passed": <count>, "failed": <pre-existing-fail-count>, "recorded": "{session_uid}"}},
  "max_batch_size": 3,
  "runtime_version": {{"python": "3.x"}}
}}
```

### 4. Inventory existing features
For each meaningful existing capability, POST to `{pm_api_url}/api/features` with `status="Pushed"` (they already exist on main):
```json
{{"product_id": {product_id}, "name": "...", "status": "Pushed", "source": "ai"}}
```

### 5. Leave files in the working tree — do NOT run git
The orchestrator commits and pushes `ARCHITECTURE.md` + `product_config.json` to `main` after you exit. Never run `git add`/`commit`/`push` yourself — agent-side git is rejected by the harness and contradicts the factory-wide git-ownership contract.

### 6. Mark analysis complete
PATCH `{pm_api_url}/api/products/{product_id}` → `analysis_status="done"`, `status="ready"`.

Exit 0.
