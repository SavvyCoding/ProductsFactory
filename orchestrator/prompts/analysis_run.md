You are running a one-time ANALYSIS RUN for **{product_name}** (product_id={product_id}).
This is a brownfield product. Your job is to understand the codebase and produce the documents
that all future sessions will rely on.
Session ID: {session_uid}
PM API base URL: {pm_api_url}
Tech stack: {tech_stack}

Your working directory is /workspace.

---

## What to do (in order)

### Step 1 — Map the codebase
- Read all source files. Build a mental map of modules, patterns, naming conventions, test structure.
- Identify which tests currently pass and which fail.

### Step 2 — Write ARCHITECTURE.md (≤800 tokens, self-trimming)
Must include:
- Directory structure (existing source + test paths)
- Key modules and their responsibilities
- Patterns in use (DI approach, error handling, logging, DB access, etc.)
- Naming conventions
- Pre-existing test failures (excluded from baseline gate)
- "Do not change without PM approval" list

### Step 3 — Write product_config.json (repo root)
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
  "baseline_tests": {{
    "passed": <count>,
    "failed": <pre-existing-fail-count>,
    "recorded": "{session_uid}"
  }},
  "max_batch_size": 3,
  "runtime_version": {{"python": "3.x"}}
}}
```

### Step 4 — Inventory existing features
For each meaningful existing capability, POST to {pm_api_url}/api/features:
```json
{{"product_id": {product_id}, "name": "...", "status": "Pushed", "source": "ai"}}
```
(Status = Pushed because they already exist on main.)

### Step 5 — Commit and push
- Commit ARCHITECTURE.md + product_config.json to `main`
- Message: "chore: Analysis Run — ARCHITECTURE.md + product_config.json [ProductFactory]"
- Push to `main` (this is a one-off scaffolding pass that runs before any sprint exists, so it correctly targets `main`)

### Step 6 — Mark analysis complete
PATCH {pm_api_url}/api/products/{product_id} → analysis_status = "done", status = "ready"

Exit cleanly (exit 0).
