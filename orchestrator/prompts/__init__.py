"""Prompt builder — selects the right template and fills in product context."""

import os
from pathlib import Path


def build_prompt(product: dict, session_uid: str, persona: str | None = None) -> str:
    """
    Returns the full Claude prompt string for this product session.

    Persona routing:
      designer  → designer.md
      reviewer  → reviewer.md
      coder     → greenfield.md or brownfield.md (existing coder path)
      None      → legacy routing (analysis_run / brownfield / greenfield)
    """
    if persona == "designer":
        template_name = "designer"
    elif persona == "reviewer":
        template_name = "reviewer"
    elif product.get("analysis_status") == "running":
        template_name = "analysis_run"
    elif product.get("type") == "brownfield":
        template_name = "brownfield"
    else:
        template_name = "greenfield"

    template_path = Path(__file__).parent / f"{template_name}.md"
    template = template_path.read_text(encoding="utf-8")

    return template.format(
        product_id=product["id"],
        product_name=product.get("name", product["working_dir"]),
        session_uid=session_uid,
        pm_api_url=os.environ["PM_API_URL"],
        tech_stack=", ".join(product.get("tech_stack") or []),
    )
