"""Record a narrated product walkthrough of the LIVE ProductFactory dashboard.

Drives the running PM website with Playwright (Chromium), records the session to
video, synthesises a voiceover with Edge TTS, and muxes the two into an MP4 in
``output/``.

Unlike ``scripts/build_pf_video.py`` (static slides), this captures the real app:
the dashboard, the Add-Product wizard (greenfield flow), a product's backlog, and
its architecture map.

Prerequisites (all checked at startup):
  - The PM website running and reachable (default http://localhost:8080).
  - PM_USERNAME / PM_PASSWORD (read from .env or the environment) for Basic Auth.
  - playwright + chromium, edge-tts, moviepy, ffmpeg.

Run:
    python scripts/record_walkthrough.py
    PF_BASE_URL=http://localhost:8080 python scripts/record_walkthrough.py

By design it does NOT click the wizard's final "Create" button, so no real repo
is scaffolded during a render. Pass --create to actually submit the greenfield
form (creates a real GitHub repo + product row).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252, which can't encode the arrows/ellipses in
# our progress output. Force UTF-8 so prints never crash the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
VIEWPORT = {"width": 1280, "height": 720}
VOICE = "en-US-AriaNeural"


def _load_env() -> dict:
    env = dict(os.environ)
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    return env


# ── Scene script ──────────────────────────────────────────────────────────────
# Each scene: a narration line + an async action that drives the page for roughly
# `budget` seconds (the narration's length), so audio and video stay aligned.

NARRATIONS = {
    "intro": (
        "Welcome to ProductFactory — a twenty-four-seven autonomous software "
        "development system. From this single dashboard, one project manager oversees "
        "an entire fleet of products. Each one is designed, coded, tested, reviewed, "
        "and shipped entirely by AI agents running in isolated containers — with no "
        "human writing code, and no human opening pull requests."
    ),
    "grid": (
        "The fleet view shows every product the factory is building right now — a "
        "document-signing app, a ride-matching service, a chore marketplace, and more. "
        "The bar across the top counts how many are ready, in flight, or paused, and "
        "each card surfaces live progress: features shipped, features awaiting review, "
        "anything blocked, and the phase currently being built."
    ),
    "create": (
        "Spinning up a brand-new product takes about a minute. The Add-Product wizard "
        "opens a guided flow: we choose greenfield to start from scratch, then describe "
        "the product in plain English. From that single paragraph, ProductFactory "
        "recommends a technology stack, proposes a starter backlog, and — on the final "
        "step — scaffolds a fresh GitHub repository and registers the product, all "
        "without writing a single line of code by hand."
    ),
    "summary": (
        "Opening a product lands on its summary. Across the top are the delivery "
        "metrics — features shipped, the approved backlog still waiting, open pull "
        "requests, and overall health. This is the project manager's at-a-glance view "
        "of how the agents are doing on this one product."
    ),
    "backlog": (
        "The backlog is where the work lives. Every feature moves through a strict "
        "lifecycle — pending, approved, designed, implementing, reviewing, reviewed, "
        "and finally pushed. The project manager approves what the agents are allowed "
        "to pick up; everything after that flows automatically."
    ),
    "feature": (
        "Clicking any feature opens its full story. Here is the description and the "
        "acceptance criteria the designer wrote, the current status and priority, the "
        "linked pull request, and the running history of comments and decisions from "
        "the agents — everything a developer would need to pick the work up."
    ),
    "phases": (
        "Features are grouped into phases — foundational work first, then everything "
        "that builds on it. This is also where the human-in-the-loop phase gate lives. "
        "When a phase finishes, the factory freezes the next one and waits: the project "
        "manager reviews a generated report and clicks approve to unlock the following "
        "phase — keeping a person in control of direction while the agents handle execution."
    ),
    "prs": (
        "Every coder session opens its own pull request, straight to main. The "
        "pull-requests tab tracks them as they're reviewed and squash-merged. One "
        "feature, one PR — no giant integration branches, and no batch merges."
    ),
    "live": (
        "The live-session view streams an agent's work in real time. As a session runs "
        "inside its container, its log lines appear here as they happen — so you can "
        "watch the designer reason about a spec, or the coder implement and test a "
        "feature, live."
    ),
    "architecture": (
        "A dedicated architect agent maintains this living architecture document for "
        "every product — the canonical modules, entry points, and rules. The "
        "orchestrator feeds it back into each coding session, so the agents reuse "
        "existing code instead of drifting into duplicate implementations."
    ),
    "admin": (
        "Behind the scenes, the admin panel is mission control. From here you configure "
        "the GitHub App that powers all git access, tune the orchestration loop — "
        "session timeouts, daily caps, and fix-attempt budgets — and choose the agent "
        "backend, whether that's the Claude models or a local Ollama setup."
    ),
    "closing": (
        "Deterministic quality gates, rule-based supervisors, and a human phase gate "
        "keep the whole pipeline healthy, around the clock, with zero human pull "
        "requests. That is ProductFactory — an autonomous engineering team you manage "
        "from a single screen."
    ),
}


async def _smooth_scroll(page, total_px: int, budget: float):
    """Scroll down by total_px over roughly `budget` seconds, then settle."""
    steps = max(8, int(budget * 8))
    per = total_px / steps
    dt = max(0.04, (budget * 0.8) / steps)
    for _ in range(steps):
        await page.mouse.wheel(0, per)
        await asyncio.sleep(dt)
    await asyncio.sleep(max(0.0, budget * 0.2))


async def _settle(page, budget: float):
    await asyncio.sleep(budget)


async def _click_sidebar(page, title, wait=1.2):
    """Click a product-page sidebar tab by its title attribute."""
    try:
        await page.click(f"a.pf-sidebar-item[title='{title}']", timeout=4000)
    except Exception as e:  # noqa: BLE001
        print(f"  [tab '{title}'] non-fatal: {e}")
    await asyncio.sleep(wait)


async def _to_top(page):
    await page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
    await asyncio.sleep(0.3)


# Real product names → neutral labels, set once in _record. Applied after every
# navigation so no real product name appears anywhere in the recording.
REDACT_PAIRS: list[tuple[str, str]] = []


async def _redact_names(page):
    if not REDACT_PAIRS:
        return
    try:
        await page.evaluate(
            """(pairs) => {
              // Neutralise the 2-letter card avatars (derived from the name).
              document.querySelectorAll('.product-card-mark').forEach((el) => { el.textContent = 'PF'; });
              // Replace every real product name in any text node with its label.
              const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              const nodes = []; while (walk.nextNode()) nodes.push(walk.currentNode);
              for (const n of nodes) {
                let t = n.nodeValue;
                for (const [k, v] of pairs) { if (k && t.includes(k)) t = t.split(k).join(v); }
                if (t !== n.nodeValue) n.nodeValue = t;
              }
              // Title bar too (not on screen, but tidy).
              for (const [k, v] of pairs) { if (k && document.title.includes(k)) document.title = document.title.split(k).join(v); }
            }""",
            REDACT_PAIRS,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [redact] non-fatal: {e}")


async def act_intro(page, base, ctx, budget):
    await page.goto(base + "/", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.5)
    await _smooth_scroll(page, 300, budget - 1.5)


async def act_grid(page, base, ctx, budget):
    await page.goto(base + "/", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.2)
    await _smooth_scroll(page, 1100, budget - 1.6)


async def _scroll_el(page, selector, budget, total=1400):
    """Smoothly scroll an inner scroll-container (e.g. the slide-out panel body)."""
    steps = max(8, int(budget * 6))
    per = total / steps
    dt = max(0.05, (budget * 0.8) / steps)
    for _ in range(steps):
        await page.evaluate(
            "([s, d]) => { const e = document.querySelector(s); if (e) e.scrollTop += d; }",
            [selector, per],
        )
        await asyncio.sleep(dt)
    await asyncio.sleep(max(0.0, budget * 0.2))


async def act_create(page, base, ctx, budget, do_create: bool):
    """Full greenfield wizard: open -> greenfield -> vision -> final Details screen.
    Does NOT submit unless do_create (so no real repo/product is created)."""
    t = time.monotonic()
    await page.goto(base + "/", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.0)
    vision = (
        "A community marketplace where neighbours lend and borrow tools and "
        "household equipment, with listings, reservations, and reviews."
    )
    try:
        await page.evaluate("typeof openWizard==='function' && openWizard()")
        await asyncio.sleep(1.2)
        await page.evaluate("typeof selectType==='function' && selectType('greenfield')")
        await asyncio.sleep(1.0)
        await page.evaluate("typeof wizardNext==='function' && wizardNext()")
        await asyncio.sleep(1.0)
        ta = page.locator("#gf-vision")
        if await ta.count():
            await ta.fill("")
            for ch in vision:
                await ta.type(ch, delay=11)
        await asyncio.sleep(1.0)
        for fn in ("wizardToStack", "wizardToUI", "wizardToBacklog", "wizardToDetailsFromBacklog"):
            try:
                await page.evaluate(f"typeof {fn}==='function' && {fn}()")
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.7)
        # Force the final "Details" screen visible regardless of step guards, with
        # name/repo filled — but DO NOT submit (no real product created).
        await page.evaluate(
            """() => {
              document.querySelectorAll('.wizard-panel').forEach((p) => p.classList.remove('active'));
              const d = document.getElementById('wp-6-details'); if (d) d.classList.add('active');
              document.querySelectorAll('.wizard-step').forEach((s) => s.classList.toggle('active', s.id === 'ws-6'));
              const n = document.getElementById('gf-name'); if (n) n.value = 'NeighbourLend';
              const r = document.getElementById('gf-repo');
              if (r) { r.value = 'neighbour-lend'; r.dispatchEvent(new Event('input', {bubbles: true})); }
            }"""
        )
        await asyncio.sleep(2.0)
        if do_create:  # opt-in only: actually scaffolds a real repo + product row
            await page.evaluate("typeof submitGreenfield==='function' && submitGreenfield()")
            await asyncio.sleep(3.0)
    except Exception as e:  # noqa: BLE001
        print(f"  [create] non-fatal: {e}")
    await _settle(page, max(0.0, budget - (time.monotonic() - t)))


async def act_feature(page, base, ctx, budget, product_id):
    """Open a feature's slide-out panel and reveal its story / AC / details."""
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _click_sidebar(page, "Backlog")
    await _redact_names(page)
    try:
        el = page.locator("#feature-backlog-table [data-feature-id]").first
        if not await el.count():
            el = page.locator("[data-feature-id]").first
        fid = await el.get_attribute("data-feature-id")
        if fid:
            await page.evaluate(
                "(id) => { if (typeof openFeaturePanel === 'function') openFeaturePanel(parseInt(id)); }",
                fid,
            )
        else:
            await el.click()
        await asyncio.sleep(1.8)
    except Exception as e:  # noqa: BLE001
        print(f"  [feature] non-fatal: {e}")
    await _redact_names(page)
    await _scroll_el(page, "#fp-body", budget - 2.5)


async def act_summary(page, base, ctx, budget, product_id):
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.5)
    await _smooth_scroll(page, 700, budget - 1.5)


async def act_backlog(page, base, ctx, budget, product_id):
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _click_sidebar(page, "Backlog")
    await _redact_names(page)
    await _smooth_scroll(page, 1200, budget - 1.8)


async def act_phases(page, base, ctx, budget, product_id):
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _click_sidebar(page, "Features")
    await _redact_names(page)
    await _smooth_scroll(page, 1400, budget - 1.8)


async def act_prs(page, base, ctx, budget, product_id):
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _click_sidebar(page, "Pull Requests")
    await _redact_names(page)
    await _smooth_scroll(page, 900, budget - 1.8)


async def act_live(page, base, ctx, budget, product_id):
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _click_sidebar(page, "Live Session", wait=2.0)
    await _redact_names(page)
    await _smooth_scroll(page, 500, budget - 2.8)


async def act_architecture(page, base, ctx, budget, product_id):
    await page.goto(f"{base}/product/{product_id}/architecture", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.5)
    await _smooth_scroll(page, 1100, budget - 1.9)
    await asyncio.sleep(0.4)


async def act_admin(page, base, ctx, budget):
    await page.goto(f"{base}/admin", wait_until="domcontentloaded")
    # Redact secrets BEFORE anything is on screen — the admin System tab renders
    # the GitHub App private key (PEM), App/Installation IDs, and webhook/API
    # secrets in plaintext. Never let those land in a shareable recording.
    await page.evaluate(
        """() => {
          const set = (id, val) => {
            const e = document.getElementById(id);
            if (e) {
              try { e.type = 'text'; } catch (_) {}  // number inputs reject bullet strings
              e.value = val; if ('textContent' in e) e.textContent = val;
            }
          };
          set('a-pem', '\\u2022\\u2022\\u2022\\u2022  GitHub App private key (PEM) \\u2014 redacted for this recording  \\u2022\\u2022\\u2022\\u2022');
          set('a-app-id', '\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022');
          set('a-inst-id', '\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022');
          set('a-root', 'C:/\\u2026/Products');
          set('a-slack', 'https://hooks.slack.com/\\u2026 (redacted)');
          ['a-wh-secret', 'p-anthropic-key', 'p-ollama-api-key'].forEach((i) => set(i, ''));
        }"""
    )
    await asyncio.sleep(1.5)
    per = max(1.5, (budget - 1.5) / 4)
    for label in ("System", "Poller", "Agent"):
        try:
            await page.click(f"button.tab-btn:has-text('{label}')", timeout=3000)
        except Exception as e:  # noqa: BLE001
            print(f"  [admin tab '{label}'] non-fatal: {e}")
        await _smooth_scroll(page, 300, per)
        await _to_top(page)
    await asyncio.sleep(max(0.0, budget - 1.5 - per * 3))


async def act_closing(page, base, ctx, budget):
    await page.goto(base + "/", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.2)
    await _smooth_scroll(page, 400, budget - 1.2)


def _pick_product(env, base) -> int:
    """Pick a product to showcase: prefer a 'ready' one, else the first."""
    try:
        import httpx
        auth = (env.get("PM_USERNAME", ""), env.get("PM_PASSWORD", ""))
        r = httpx.get(base + "/api/products", auth=auth, timeout=8)
        r.raise_for_status()
        prods = r.json()
        for p in prods:
            if p.get("status") == "ready":
                return p["id"]
        if prods:
            return prods[0]["id"]
    except Exception as e:  # noqa: BLE001
        print(f"  [pick_product] falling back: {e}")
    return 33


async def _synth(narrations: dict[str, str], tmp: Path) -> dict[str, Path]:
    import edge_tts
    out = {}
    for key, text in narrations.items():
        p = tmp / f"vo_{key}.mp3"
        await edge_tts.Communicate(text, voice=VOICE).save(str(p))
        out[key] = p
    return out


def _durations(audio: dict[str, Path]) -> dict[str, float]:
    from moviepy import AudioFileClip
    d = {}
    for k, p in audio.items():
        c = AudioFileClip(str(p))
        d[k] = c.duration
        c.close()
    return d


def _build_redact_pairs(env, base):
    """Map every real product name → a neutral label ('Product 1', ...).

    Longer names first so substrings can't be partially replaced.
    """
    try:
        import httpx
        auth = (env.get("PM_USERNAME", ""), env.get("PM_PASSWORD", ""))
        prods = httpx.get(base + "/api/products", auth=auth, timeout=8).json()
        names = []
        for p in prods:
            n = (p.get("name") or "").strip()
            if n:
                names.append(n)
        names = sorted(set(names), key=len, reverse=True)
        return [(n, f"Product {i + 1}") for i, n in enumerate(names)]
    except Exception as e:  # noqa: BLE001
        print(f"  [redact] could not fetch product names: {e}")
        return []


SCENE_ORDER = [
    "intro", "grid", "create",
    "summary", "backlog", "feature", "phases", "prs", "live",
    "architecture", "admin", "closing",
]

# How long the visible action lasts per scene = narration + this much tail. The
# extra head/tail is trimmed/used by the crossfade so the cut never clips the VO.
HEAD_TRIM = 1.5   # drop the white page-load flash at the start of each clip
TAIL_PAD = 1.2    # silent tail after the VO, must exceed the crossfade duration
CROSSFADE = 0.6   # dissolve between scenes


async def _record_scene(browser, base, env, key, budget, do_create, product_id, video_dir):
    """Record ONE scene into its own webm so scenes can be glued with transitions."""
    ctx = await browser.new_context(
        viewport=VIEWPORT,
        record_video_dir=str(video_dir),
        record_video_size=VIEWPORT,
        http_credentials={
            "username": env.get("PM_USERNAME", ""),
            "password": env.get("PM_PASSWORD", ""),
        },
    )
    page = await ctx.new_page()
    try:
        if key == "intro":
            await act_intro(page, base, ctx, budget)
        elif key == "grid":
            await act_grid(page, base, ctx, budget)
        elif key == "create":
            await act_create(page, base, ctx, budget, do_create)
        elif key == "summary":
            await act_summary(page, base, ctx, budget, product_id)
        elif key == "backlog":
            await act_backlog(page, base, ctx, budget, product_id)
        elif key == "feature":
            await act_feature(page, base, ctx, budget, product_id)
        elif key == "phases":
            await act_phases(page, base, ctx, budget, product_id)
        elif key == "prs":
            await act_prs(page, base, ctx, budget, product_id)
        elif key == "live":
            await act_live(page, base, ctx, budget, product_id)
        elif key == "architecture":
            await act_architecture(page, base, ctx, budget, product_id)
        elif key == "admin":
            await act_admin(page, base, ctx, budget)
        elif key == "closing":
            await act_closing(page, base, ctx, budget)
    finally:
        video = page.video
        await ctx.close()  # flushes the webm
    return Path(await video.path()) if video else None


async def _record(base, env, audio_dur, do_create, product_id, video_dir):
    from playwright.async_api import async_playwright

    clips: dict[str, Path] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        for i, key in enumerate(SCENE_ORDER):
            budget = HEAD_TRIM + audio_dur[key] + TAIL_PAD
            print(f"  scene {i + 1:2}/{len(SCENE_ORDER)}  '{key}'  (~{budget:.1f}s)")
            scene_dir = video_dir / key
            scene_dir.mkdir(parents=True, exist_ok=True)
            clips[key] = await _record_scene(
                browser, base, env, key, budget, do_create, product_id, scene_dir
            )
        await browser.close()
    return clips


def _assemble(clips: dict[str, Path], audio: dict[str, Path], out_mp4: Path):
    """Glue the per-scene clips with crossfade dissolves, then lay the VO on each."""
    from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips, vfx

    segs = []
    for i, key in enumerate(SCENE_ORDER):
        wp = clips.get(key)
        if not wp or not wp.exists():
            print(f"  [assemble] missing clip for '{key}', skipping")
            continue
        v = VideoFileClip(str(wp))
        a = AudioFileClip(str(audio[key]))
        core = a.duration + TAIL_PAD                      # VO + silent tail
        end = min(v.duration, HEAD_TRIM + core)
        v = v.subclipped(min(HEAD_TRIM, max(0.0, v.duration - 0.1)), end)
        v = v.with_audio(a)                               # VO starts at clip start
        if i > 0:
            v = v.with_effects([vfx.CrossFadeIn(CROSSFADE)])
        segs.append(v)

    final = concatenate_videoclips(segs, method="compose", padding=-CROSSFADE)
    final.write_videofile(
        str(out_mp4), fps=24, codec="libx264", audio_codec="aac", logger=None
    )
    final.close()
    for s in segs:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true",
                    help="Actually submit the greenfield wizard (creates a real repo).")
    ap.add_argument("--product-id", type=int, default=None,
                    help="Product to showcase on the product/architecture scenes.")
    args = ap.parse_args()

    env = _load_env()
    base = env.get("PF_BASE_URL", "http://localhost:8080").rstrip("/")
    if not env.get("PM_USERNAME") or not env.get("PM_PASSWORD"):
        print("ERROR: PM_USERNAME / PM_PASSWORD not set (.env or environment).")
        sys.exit(1)

    # Fail fast if the app is unreachable.
    try:
        import httpx
        httpx.get(base + "/", auth=(env["PM_USERNAME"], env["PM_PASSWORD"]), timeout=6)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: PM website not reachable at {base} ({e}). Start it with "
              f"`docker compose up -d`.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    global REDACT_PAIRS
    REDACT_PAIRS = _build_redact_pairs(env, base)
    print(f"Redacting {len(REDACT_PAIRS)} product name(s) from the recording.")
    product_id = args.product_id or _pick_product(env, base)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_mp4 = OUTPUT_DIR / f"productfactory_walkthrough_{ts}.mp4"
    print(f"Recording walkthrough → {out_mp4}  (showcasing product #{product_id})")

    import tempfile
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        print("Phase 1: synthesising narration (Edge TTS)…")
        audio = asyncio.run(_synth(NARRATIONS, tmp))
        dur = _durations(audio)

        print("Phase 2: recording each scene as its own clip (Playwright)…")
        clips = asyncio.run(_record(base, env, dur, args.create, product_id, tmp))
        if not any(p and p.exists() for p in clips.values()):
            print("ERROR: no video captured.")
            sys.exit(1)

        print("Phase 3: gluing scenes with crossfades + voiceover (moviepy)…")
        _assemble(clips, audio, out_mp4)

    size_mb = out_mp4.stat().st_size // 1024 // 1024
    print(f"Done! → {out_mp4}  ({size_mb} MB)")


if __name__ == "__main__":
    main()
