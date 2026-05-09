"""Behavioral side-by-side: do OLD and NEW versions of a prompt commit to the
same actions?

For each version we render the prompt with the eval fixture, send it to the
model with a planning task that forces structured output, then run a judge
turn that flags any behavioral commitment in OLD that is missing from NEW.

The output isn't a pass/fail — it's a list of differences for human review.
The script prints "no missing commitments detected" when judge says behavior
is preserved.

Usage:
  OLLAMA_HOST=https://ollama.com OLLAMA_API_KEY=... DESIGNER_MODEL=glm-4.7 \\
      python scripts/behavioral_diff.py greenfield.md
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from evals.fixtures import SAMPLE_PRODUCT, SAMPLE_SESSION_UID
from orchestrator.prompts import build_prompt


_raw_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip()
if "://" not in _raw_host:
    _raw_host = f"http://{_raw_host}"
_host_part = _raw_host.split("://", 1)[1]
if ":" not in _host_part.split("/")[0] and "ollama.com" not in _host_part:
    _raw_host = f"{_raw_host}:11434"
_raw_host = _raw_host.replace("://0.0.0.0", "://127.0.0.1")
OLLAMA_HOST = _raw_host.rstrip("/")
OLLAMA_MODEL = os.environ.get("DESIGNER_MODEL", "glm-4.7")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")


def ollama_chat(messages: list[dict], num_predict: int = 2048) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        # Reasoning models (glm-4.7, deepseek-v3, kimi-thinking) burn the entire
        # num_predict budget inside <think>...</think> on long judge prompts and
        # return empty content. Setting think=false disables that block.
        "think": False,
        "options": {"num_predict": num_predict, "temperature": 0.0, "seed": 42},
    }
    headers = {"Content-Type": "application/json"}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
    r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, headers=headers, timeout=600)
    r.raise_for_status()
    j = r.json()
    content = (j.get("message") or {}).get("content", "") or ""
    if os.environ.get("DIFF_DEBUG"):
        print(f"[debug] eval_count={j.get('eval_count')} done_reason={j.get('done_reason')} content_len={len(content)}")
    return content


JUDGE_PROMPT_TEMPLATE = textwrap.dedent("""
    You are auditing two versions (OLD and NEW) of the same agent prompt
    after a token-optimization rewrite. The rewrite was supposed to
    preserve behavior — only remove redundancy.

    Your job: find behavioral commitments in OLD that are MISSING from NEW.
    A "commitment" is a specific directive the agent must follow: a forbidden
    command, a required file path, a status string, a format rule, an exit
    condition, a numeric threshold, etc.

    CRITICAL: Before flagging anything missing, search the NEW prompt
    text for the same fact in different wording. Synonyms, rephrased
    sentences, and consolidated bullets are NOT regressions. ONLY flag
    a commitment as missing if you cannot find equivalent semantics
    anywhere in NEW.

    For each commitment you flag missing, quote the exact line from OLD
    AND state what you searched for in NEW.

    Output ONLY a JSON object, no prose, no code fences. Every field is
    a plain string — do not embed lists or quoted JSON inside string values:
    {
      "missing_in_new": [
        {"old_quote": "<verbatim from OLD, single line>",
         "searched_in_new_for": "<comma-separated keywords as a single string>",
         "severity": "high|medium|low",
         "reason": "<one sentence, no inner quotes>"}
      ],
      "verdict": "behavior_preserved",
      "summary": "<one sentence>"
    }
    Use the empty list [] for missing_in_new when no real losses are found.

    OLD prompt:
    ---BEGIN OLD---
    {old_prompt}
    ---END OLD---

    NEW prompt:
    ---BEGIN NEW---
    {new_prompt}
    ---END NEW---
""").strip()


def render_with_template(prompt_path: Path, template_text: str) -> str:
    backup = prompt_path.read_text(encoding="utf-8")
    persona = prompt_path.stem
    try:
        prompt_path.write_text(template_text, encoding="utf-8")
        if persona in ("greenfield", "brownfield"):
            product = dict(SAMPLE_PRODUCT, type=persona)
            return build_prompt(product, SAMPLE_SESSION_UID, persona=None)
        return build_prompt(SAMPLE_PRODUCT, SAMPLE_SESSION_UID, persona=persona)
    finally:
        prompt_path.write_text(backup, encoding="utf-8")


def fetch_old(prompt_file: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"HEAD:orchestrator/prompts/{prompt_file}"],
        cwd=ROOT, text=True, encoding="utf-8",
    )


def judge(old_prompt: str, new_prompt: str) -> dict:
    msg = JUDGE_PROMPT_TEMPLATE.replace(
        "{old_prompt}", old_prompt
    ).replace(
        "{new_prompt}", new_prompt
    )
    raw = ollama_chat([{"role": "user", "content": msg}], num_predict=4000).strip()
    # Strip <think>...</think> blocks some models emit before the JSON.
    if "</think>" in raw:
        raw = raw.split("</think>", 1)[1].strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        print("\n[!] Judge returned no JSON object. Raw response:\n")
        print(raw or "(empty)")
        return {"verdict": "judge_unavailable", "raw": raw}
    try:
        return json.loads(raw[s : e + 1])
    except json.JSONDecodeError as err:
        # Fall back: extract verdict + summary via regex so the user still
        # sees the answer even when the model produced slightly malformed JSON.
        import re
        body = raw[s : e + 1]
        verdict = re.search(r'"verdict"\s*:\s*"([^"]+)"', body)
        summary = re.search(r'"summary"\s*:\s*"([^"]+)"', body)
        print(f"\n[!] Judge JSON parse failed ({err}); falling back to regex extraction.")
        print("\nRaw response:\n")
        print(body)
        return {
            "verdict": verdict.group(1) if verdict else "judge_unavailable",
            "summary": summary.group(1) if summary else "",
            "raw": raw,
        }


BASELINE_DIR = ROOT / "evals" / "prompt_baselines"


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    refresh = "--refresh-baseline" in args
    args = [a for a in args if a != "--refresh-baseline"]
    if not args:
        print("usage: behavioral_diff.py <prompt_file> [--refresh-baseline]")
        return 2
    prompt_file = args[0]
    prompt_path = ROOT / "orchestrator" / "prompts" / prompt_file
    baseline_path = BASELINE_DIR / f"{prompt_file}.baseline.txt"

    print(f"Prompt: {prompt_file}")
    print(f"Model:  {OLLAMA_MODEL} @ {OLLAMA_HOST}")
    print()

    # Load (or build) the OLD baseline. By default we use the prompt at
    # git HEAD as the behavioral baseline; once it's cached we never
    # re-render it. Pass --refresh-baseline after committing a new "OLD"
    # version to update the cache.
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    if baseline_path.exists() and not refresh:
        print(f"-> using cached baseline: {baseline_path.relative_to(ROOT)}")
        old_rendered = baseline_path.read_text(encoding="utf-8")
    else:
        print("-> rendering OLD from git HEAD and caching...")
        old_template = fetch_old(prompt_file)
        old_rendered = render_with_template(prompt_path, old_template)
        baseline_path.write_text(old_rendered, encoding="utf-8")
        print(f"   cached at {baseline_path.relative_to(ROOT)}")

    print("-> rendering NEW (current working copy)...")
    new_template = prompt_path.read_text(encoding="utf-8")
    new_rendered = render_with_template(prompt_path, new_template)

    print("-> running judge (direct prompt comparison)...")
    verdict = judge(old_rendered, new_rendered)
    print()
    print("=== Judge verdict ===")
    print(json.dumps(verdict, indent=2))

    # Persist a structured report next to the baseline so the user can
    # compare runs side-by-side. Each run overwrites the previous; older
    # runs are appended to <file>.diff.history.jsonl for historical view.
    report = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "prompt_file": prompt_file,
        "model": OLLAMA_MODEL,
        "host": OLLAMA_HOST,
        "old_chars": len(old_rendered),
        "new_chars": len(new_rendered),
        "old_words": len(old_rendered.split()),
        "new_words": len(new_rendered.split()),
        "char_delta_pct": round(100.0 * (len(new_rendered) - len(old_rendered)) / len(old_rendered), 1),
        "verdict": verdict,
    }
    report_path = BASELINE_DIR / f"{prompt_file}.diff.json"
    history_path = BASELINE_DIR / f"{prompt_file}.diff.history.jsonl"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report) + "\n")
    print()
    print(f"-> report written: {report_path.relative_to(ROOT)}")
    print(f"-> appended history: {history_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
