"""Prompt builder — selects the right template and fills in product context."""

from pathlib import Path


def build_prompt(product: dict, session_uid: str) -> str:
    """Returns the full Claude prompt string for this product session."""
    template_name = "analysis_run" if product.get("analysis_status") == "running" else (
        "brownfield" if product.get("type") == "brownfield" else "greenfield"
    )
    template_path = Path(__file__).parent / f"{template_name}.md"
    template = template_path.read_text(encoding="utf-8")

    return template.format(
        product_id=product["id"],
        product_name=product.get("name", product["working_dir"]),
        session_uid=session_uid,
        pm_api_url=__import__("os").environ["PM_API_URL"],
        tech_stack=", ".join(product.get("tech_stack") or []),
    )
