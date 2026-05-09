"""Compare input/output token counts between old and new designer prompt.

Renders both prompt versions through build_prompt() using the eval fixture, then
sends each to a local Ollama model OR an OpenAI-compatible chat completions
endpoint (Zhipu BigModel / Z.ai for GLM, OpenRouter, etc.) and reports the
input/output token counts.

Backend selection (auto):
  - If GLM_API_BASE + GLM_API_KEY are set, uses the cloud OpenAI-compat endpoint.
  - Otherwise falls back to local Ollama at OLLAMA_HOST.

Env vars:
  GLM_API_BASE   e.g. https://api.z.ai/api/paas/v4 or https://open.bigmodel.cn/api/paas/v4
  GLM_API_KEY    bearer token
  GLM_MODEL      e.g. glm-4.6, glm-4-plus, glm-4.7 (default: glm-4.6)
  OLLAMA_HOST    e.g. http://localhost:11434 (used when GLM_* unset)
  DESIGNER_MODEL Ollama tag (default: gemma3:27b)
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

# Make orchestrator importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from evals.fixtures import SAMPLE_PRODUCT, SAMPLE_SESSION_UID
from orchestrator.prompts import build_prompt


_raw_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip()
if "://" not in _raw_host:
    _raw_host = f"http://{_raw_host}"
_host_part = _raw_host.split("://", 1)[1]
# Don't append :11434 if the host already has a path or already a port, or is a public hostname.
if ":" not in _host_part.split("/")[0] and "ollama.com" not in _host_part:
    _raw_host = f"{_raw_host}:11434"
# 0.0.0.0 is a bind address — not connectable on Windows.
_raw_host = _raw_host.replace("://0.0.0.0", "://127.0.0.1")
OLLAMA_HOST = _raw_host.rstrip("/")
OLLAMA_MODEL = os.environ.get("DESIGNER_MODEL", "gemma3:27b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")

GLM_API_BASE = os.environ.get("GLM_API_BASE", "").rstrip("/")
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-4.6")

USE_CLOUD = bool(GLM_API_BASE and GLM_API_KEY)

# Prompt file under test — set via PROMPT env var; defaults to designer.
# Persona is inferred from filename: greenfield/brownfield use type-routing
# (persona=None + product.type=<persona>), everything else uses persona=<stem>.
PROMPT_FILE = os.environ.get("PROMPT", "designer.md")
PROMPT_PATH = ROOT / "orchestrator" / "prompts" / PROMPT_FILE
PERSONA_STEM = PROMPT_PATH.stem


def render_with_template(template_text: str) -> str:
    """Render the prompt-under-test using a given template body."""
    backup = PROMPT_PATH.read_text(encoding="utf-8")
    try:
        PROMPT_PATH.write_text(template_text, encoding="utf-8")
        if PERSONA_STEM in ("greenfield", "brownfield"):
            product = dict(SAMPLE_PRODUCT, type=PERSONA_STEM)
            return build_prompt(product, SAMPLE_SESSION_UID, persona=None)
        return build_prompt(SAMPLE_PRODUCT, SAMPLE_SESSION_UID, persona=PERSONA_STEM)
    finally:
        PROMPT_PATH.write_text(backup, encoding="utf-8")


def ollama_generate(prompt: str, num_predict: int = 512) -> dict:
    """Calls Ollama /api/generate. Works for local Ollama and Ollama Cloud."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": num_predict,
            "temperature": 0.0,
            "seed": 42,
        },
    }
    headers = {"Content-Type": "application/json"}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
    resp = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json=payload, headers=headers, timeout=600,
    )
    resp.raise_for_status()
    r = resp.json()
    return {
        "in_toks": r.get("prompt_eval_count"),
        "out_toks": r.get("eval_count"),
        "response": r.get("response", "") or "",
    }


def cloud_generate(prompt: str, max_tokens: int = 2048) -> dict:
    """OpenAI-compatible chat completions — works with Zhipu BigModel and Z.ai."""
    payload = {
        "model": GLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GLM_API_KEY}",
    }
    resp = requests.post(
        f"{GLM_API_BASE}/chat/completions",
        json=payload, headers=headers, timeout=600,
    )
    resp.raise_for_status()
    r = resp.json()
    usage = r.get("usage") or {}
    choice = (r.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {
        "in_toks": usage.get("prompt_tokens"),
        "out_toks": usage.get("completion_tokens"),
        "response": msg.get("content", "") or "",
    }


def fetch_old_prompt() -> str:
    """Get the prior prompt-under-test from git HEAD."""
    out = subprocess.check_output(
        ["git", "show", f"HEAD:orchestrator/prompts/{PROMPT_FILE}"],
        cwd=ROOT, text=True, encoding="utf-8",
    )
    return out


def main() -> int:
    old_template = fetch_old_prompt()
    new_template = PROMPT_PATH.read_text(encoding="utf-8")

    print(f"Prompt: {PROMPT_FILE}  (persona={PERSONA_STEM})")
    if USE_CLOUD:
        print(f"Backend: cloud (OpenAI-compatible)")
        print(f"Endpoint: {GLM_API_BASE}")
        print(f"Model: {GLM_MODEL}")
        gen = lambda p: cloud_generate(p, max_tokens=2048)
    else:
        print(f"Backend: Ollama @ {OLLAMA_HOST}")
        print(f"Model: {OLLAMA_MODEL}")
        gen = lambda p: ollama_generate(p, num_predict=2048)
    print()

    rendered = {
        "OLD (HEAD)": render_with_template(old_template),
        "NEW (current)": render_with_template(new_template),
    }

    print(f"{'Variant':<16} {'chars':>7} {'words':>7}")
    for label, prompt in rendered.items():
        print(f"{label:<16} {len(prompt):>7} {len(prompt.split()):>7}")
    print()

    results = {}
    for label, prompt in rendered.items():
        print(f"-> sending {label} ({len(prompt)} chars)...")
        t0 = time.time()
        r = gen(prompt)
        elapsed = time.time() - t0
        results[label] = {
            "in_toks": r["in_toks"],
            "out_toks": r["out_toks"],
            "wall_s": round(elapsed, 2),
            "response_first_120": r["response"][:120].replace("\n", " "),
        }

    print()
    print(f"{'Variant':<16} {'in_toks':>9} {'out_toks':>9} {'wall_s':>8}")
    for label, m in results.items():
        print(
            f"{label:<16} {str(m['in_toks']):>9} "
            f"{str(m['out_toks']):>9} {str(m['wall_s']):>8}"
        )

    print()
    print("Deltas (negative = savings):")
    deltas: dict = {}
    for field in ("in_toks", "out_toks"):
        old, new = results["OLD (HEAD)"][field], results["NEW (current)"][field]
        if old and new:
            d = old - new
            pct = 100.0 * d / old
            print(f"  {field:<10} {old} -> {new}  ({-d:+d}, {-pct:+.1f}%)")
            deltas[field] = {"old": old, "new": new, "delta": -d, "pct": round(-pct, 1)}

    # Persist alongside the behavioral baselines so token counts and the
    # judge verdict live in the same folder. Each run overwrites the
    # latest report; history is appended to a JSONL log.
    out_dir = ROOT / "evals" / "prompt_baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "prompt_file": PROMPT_FILE,
        "backend": "cloud" if USE_CLOUD else "ollama",
        "model": GLM_MODEL if USE_CLOUD else OLLAMA_MODEL,
        "results": results,
        "deltas": deltas,
    }
    report_path = out_dir / f"{PROMPT_FILE}.tokens.json"
    history_path = out_dir / f"{PROMPT_FILE}.tokens.history.jsonl"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report) + "\n")
    print()
    print(f"-> report written: {report_path.relative_to(ROOT)}")
    print(f"-> appended history: {history_path.relative_to(ROOT)}")

    print()
    print("Sample outputs (first 120 chars):")
    for label, m in results.items():
        print(f"  {label}: {m['response_first_120']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
