"""Render the industry-comparison matrix to a PNG.

Usage:  python scripts/render_comparison_matrix.py [output.png]
Default output: output/comparison_matrix.png
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# (capability, ProductFactory, Devin, OpenDevin, Cursor, Claude Code, Aider, Sweep, GH Copilot Workspace)
ROWS: list[tuple[str, ...]] = [
    ("Multi-repo 24/7",            "+ unique", "-", "-", "-", "-", "-", "+ issue",   "-"),
    ("Sprint / DoD gates",         "+ unique", "-", "-", "-", "-", "-", "-",         "-"),
    ("Specialized personas",       "+ 13",     "~3","~3","1", "1", "1", "~2",        "~2"),
    ("PM dashboard",               "+",        "~ Slack", "-", "-", "-", "-", "GH",  "GH"),
    ("Container isolation",        "+ full",   "+ KVM", "+ docker", "N/A","N/A","N/A","+", "+"),
    ("--pids-limit",               "+ 512",    "+", "+", "—", "—", "—", "+",         "+ 512"),
    ("--cap-drop ALL",             "+",        "+", "+", "—", "—", "—", "+",         "+"),
    ("--read-only + tmpfs",        "+",        "+", "+", "—", "—", "—", "+",         "+"),
    ("no-new-privileges",          "+",        "+", "+", "—", "—", "—", "+",         "+"),
    ("Seccomp profile",            "~ default","+ custom","+ custom","—","—","—","+","+ default"),
    ("Secrets via file",           "+",        "+", "+", "N/A","N/A","N/A","+",      "+"),
    ("Hardcoded fallback pw",      "+ removed","+","+", "N/A","N/A","N/A","+",       "+"),
    ("Webhook signature",          "+ 401",    "+", "+", "—", "—", "—", "+",         "+"),
    ("LLM prompt-injection guard", "+",        "?", "~", "?", "+", "—", "?",         "+"),
    ("Basic Auth rate limit",      "+",        "+", "+", "+", "+", "N/A","+",        "+"),
    ("HTTPS / TLS",                "+ Caddy",  "+", "+", "+", "+", "N/A","+",        "+"),
    ("Structured logging",         "+ JSON",   "+", "+", "+", "+", "+", "+",         "+"),
    ("Prometheus / OTel",          "+",        "+", "+", "+", "+", "—", "+",         "+"),
    ("Agent eval harness",         "+ tier-1", "+ SWE-bench","+ SWE-bench","~","+","—","~","+"),
    ("Tool-use loop",              "+ pluggable","proprietary","LangGraph-like","Claude SDK","Claude SDK","custom","LangChain","proprietary"),
    ("Thread-safe counters",       "+",        "+", "+", "—", "—", "—", "+",         "+"),
    ("Git subprocess timeouts",    "+",        "+", "+", "—", "—", "+", "+",         "+"),
    ("FK cascade + indexes",       "+",        "+", "+", "—", "—", "—", "+",         "+"),
    ("Integration tests",          "+ (+20)",  "+", "+", "+", "+", "+", "+",         "+"),
    ("CI/CD",                      "+ GH Actions","+","+","+", "+", "+", "+",        "+"),
]

COLUMNS: list[str] = [
    "Capability",
    "ProductFactory",
    "Devin",
    "OpenDevin",
    "Cursor Agent",
    "Claude Code",
    "Aider",
    "Sweep AI",
    "GH Copilot WS",
]


def _classify(cell: str) -> str:
    """Return a style key for the cell: good, partial, bad, neutral, highlight."""
    s = cell.strip()
    if s.startswith("+"):
        if "unique" in s:
            return "highlight"
        return "good"
    if s.startswith("~"):
        return "partial"
    if s in ("-", ""):
        return "bad"
    if s == "N/A" or s == "—":
        return "neutral"
    if s == "?":
        return "unknown"
    # Freeform text (e.g. "proprietary", "1", "~3")
    if s.startswith("~"):
        return "partial"
    return "neutral"


STYLE = {
    "good":      {"bg": "#1f6f3a", "fg": "#ffffff"},
    "highlight": {"bg": "#0a3d62", "fg": "#ffd166"},
    "partial":   {"bg": "#b7791f", "fg": "#ffffff"},
    "bad":       {"bg": "#7a1f1f", "fg": "#ffffff"},
    "neutral":   {"bg": "#2d2d2d", "fg": "#cccccc"},
    "unknown":   {"bg": "#3d3d3d", "fg": "#aaaaaa"},
}


def render(out_path: Path) -> None:
    n_rows = len(ROWS) + 1   # +1 for header
    n_cols = len(COLUMNS)

    fig_w = 18
    fig_h = 0.48 * n_rows + 1.5
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
    ax.set_xlim(0, n_cols)
    ax.set_ylim(0, n_rows)
    ax.invert_yaxis()
    ax.axis("off")

    # Column widths: first column (capability) wider, ProductFactory second widest.
    widths = [2.4, 1.5] + [1.0] * (n_cols - 2)
    total_w = sum(widths)
    x_edges = [0.0]
    for w in widths:
        x_edges.append(x_edges[-1] + w * (n_cols / total_w))

    # ── Title ────────────────────────────────────────────────────────────────
    fig.suptitle(
        "ProductFactory vs. Industry Leaders — Autonomous Dev Agents",
        fontsize=16, fontweight="bold", color="#111111", y=0.995,
    )

    # ── Header row ───────────────────────────────────────────────────────────
    header_bg = "#111827"
    header_fg = "#ffffff"
    for c, col in enumerate(COLUMNS):
        x0, x1 = x_edges[c], x_edges[c + 1]
        ax.add_patch(Rectangle(
            (x0, 0), x1 - x0, 1,
            facecolor=header_bg, edgecolor="#333333", linewidth=0.6,
        ))
        ax.text(
            (x0 + x1) / 2, 0.5, col,
            ha="center", va="center",
            color=header_fg, fontsize=10, fontweight="bold",
        )

    # ── Data rows ────────────────────────────────────────────────────────────
    for r, row in enumerate(ROWS, start=1):
        # First column: capability label on a dark-neutral strip
        x0, x1 = x_edges[0], x_edges[1]
        ax.add_patch(Rectangle(
            (x0, r), x1 - x0, 1,
            facecolor="#1b1f24", edgecolor="#333333", linewidth=0.5,
        ))
        ax.text(
            x0 + 0.05 * (x1 - x0), r + 0.5, row[0],
            ha="left", va="center",
            color="#e5e7eb", fontsize=9.5, fontweight="semibold",
        )

        for c, cell in enumerate(row[1:], start=1):
            x0, x1 = x_edges[c], x_edges[c + 1]
            style = STYLE[_classify(cell)]
            # ProductFactory column (c==1): slightly brighter outline to draw the eye
            edge = "#f5a623" if c == 1 else "#333333"
            lw = 0.9 if c == 1 else 0.5
            ax.add_patch(Rectangle(
                (x0, r), x1 - x0, 1,
                facecolor=style["bg"], edgecolor=edge, linewidth=lw,
            ))
            # Strip leading "+ " / "~ " marker symbols for readability; show the text that follows.
            label = cell.strip()
            if label.startswith("+ "):
                label = label[2:]
            elif label.startswith("~ "):
                label = label[2:]
            elif label == "+":
                label = "YES"
            elif label == "-":
                label = "NO"
            elif label == "~":
                label = "partial"
            ax.text(
                (x0 + x1) / 2, r + 0.5, label,
                ha="center", va="center",
                color=style["fg"], fontsize=9,
            )

    # ── Legend ───────────────────────────────────────────────────────────────
    legend_y = n_rows + 0.3
    legend_x = 0.05
    legend_specs = [
        ("Covered",   STYLE["good"]),
        ("Unique",    STYLE["highlight"]),
        ("Partial",   STYLE["partial"]),
        ("Missing",   STYLE["bad"]),
        ("N/A",       STYLE["neutral"]),
        ("Unknown",   STYLE["unknown"]),
    ]
    box_w = 0.5
    for label, style in legend_specs:
        ax.add_patch(Rectangle(
            (legend_x, legend_y), box_w, 0.6,
            facecolor=style["bg"], edgecolor="#333333", linewidth=0.6,
        ))
        ax.text(
            legend_x + box_w + 0.08, legend_y + 0.3, label,
            ha="left", va="center", color="#111111", fontsize=9,
        )
        legend_x += box_w + 1.2

    ax.set_ylim(n_rows + 1.3, -0.2)  # extend for legend

    plt.subplots_adjust(left=0.005, right=0.995, top=0.97, bottom=0.02)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="#f9fafb")
    print(f"Wrote {out_path}  ({out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/comparison_matrix.png")
    render(out)
