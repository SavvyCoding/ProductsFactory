"""Prompt builder — selects the right template and fills in product context."""

import os
from pathlib import Path



def build_prompt(product: dict, session_uid: str, persona: str | None = None, max_features: int | None = None) -> str:
    """
    Returns the full Claude prompt string for this product session.

    Persona routing:
      designer  → designer.md
      reviewer  → reviewer.md
      coder     → greenfield.md or brownfield.md (existing coder path)
      None      → legacy routing (analysis_run / brownfield / greenfield)
    """
    if persona == "retrospective":
        template_name = "retrospective"
    elif persona == "product_planner":
        template_name = "product_planner"
    elif persona == "designer":
        template_name = "designer"
    elif persona == "reviewer":
        template_name = "reviewer"
    elif persona == "recommender":
        template_name = "recommender"
    elif persona == "planner":
        template_name = "planner"
    elif persona == "qa_tester":
        template_name = "qa_tester"
    elif persona == "security_auditor":
        template_name = "security_auditor"
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
    elif product.get("analysis_status") == "running":
        template_name = "analysis_run"
    elif product.get("type") == "brownfield":
        template_name = "brownfield"
    else:
        template_name = "greenfield"

    template_path = Path(__file__).parent / f"{template_name}.md"
    template = template_path.read_text(encoding="utf-8")

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
        "{assigned_features}": product.get("_assigned_features_md", ""),
        "{assigned_feature_count}": str(len(product.get("_assigned_features", []))),
        "{prev_session_summary}": (
            f"## Previous session context\n\n{prev}\n\n---\n" if prev else ""
        ),
        "{product_memory}": _memory_content,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template
