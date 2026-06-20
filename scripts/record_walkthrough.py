"""Record a narrated product walkthrough of ProductFactory for a broad audience.

Pipeline: synthesise an Edge-TTS voiceover, drive the real app with Playwright
(Chromium) recording each scene to its own clip, then glue the clips with
crossfades, lay the voiceover on each, and burn in synced captions — output MP4
in ``output/``.

Structure:
  1. Intro — opens the real ``design.html`` (project showcase: hero + D3 system
     map) via file://, narrating what ProductFactory is and the problem it solves.
  2. Fleet dashboard + the Add-Product greenfield wizard (real AI steps).
  3. A product walkthrough with the LEFT SIDEBAR EXPANDED, visiting every
     section: Summary, Backlog, Feature detail, Features/phases, Pull Requests,
     History, Corrections, Videos, Live Session, Settings.
  4. Architecture map + close.

Prerequisites: the PM website running (default http://localhost:8080),
PM_USERNAME/PM_PASSWORD (from .env), playwright+chromium, edge-tts, moviepy, ffmpeg.

Run:  python scripts/record_walkthrough.py
By design it does NOT submit the wizard (no real repo created). --create overrides.
Captions can be disabled with --no-captions.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252, which can't encode arrows/ellipses.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
DESIGN_HTML = ROOT / "design.html"
VIEWPORT = {"width": 1920, "height": 1080}
VOICE = "en-US-AriaNeural"

# Runs in every page BEFORE its own scripts: force dark theme (theme.js reads
# this on load) AND force the product-page left sidebar EXPANDED (it defaults to
# collapsed / icon-only; the walkthrough needs the labels visible).
INIT_SCRIPT = """
try { localStorage.setItem('pf-theme', 'dark'); } catch (e) {}
try { localStorage.setItem('pf-sidebar', 'expanded'); } catch (e) {}
"""

# Caption font — resolved at startup. Filled by _resolve_font().
FONT_PATH: str | None = None
CAPTIONS_ENABLED = True


def _resolve_font() -> str | None:
    for p in (
        "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(p).exists():
            return p
    return None


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


# ── Narration script ────────────────────────────────────────────────────────
# Grounded in the actual system (see CLAUDE.md / INVARIANTS.md) — no invented
# metrics. Each key is one scene's voiceover; scene action duration is tied to
# the voiceover length so there is no dead air.
NARRATIONS = {
    # The opener — the "why", told over the real design.html showcase page.
    "intro": (
        "Building software has always been limited by people. Every feature needs "
        "someone to design it, write it, review it, and ship it — and that human "
        "capacity is the ceiling on how fast a product can move. ProductFactory "
        "removes that ceiling. It is a twenty-four-seven autonomous development "
        "system: you give it a product vision, and a team of specialized AI agents — "
        "a designer, a coder, a reviewer, an architect, and more — builds it inside "
        "isolated containers, around the clock."
    ),
    "intro2": (
        "Every feature flows through a strict lifecycle and ships as its own reviewed "
        "pull request, merged automatically. Deterministic quality gates and "
        "rule-based supervisors keep the agents honest. The result: a single project "
        "manager can run an entire fleet of products — with no human writing code, "
        "and no human opening a pull request. Let's see it."
    ),
    "grid": (
        "This is the fleet. Every card is a real product the factory is building — "
        "the bar across the top counts how many are ready, in flight, or paused, and "
        "each card shows live progress: features shipped, features awaiting review, "
        "and anything blocked."
    ),
    # Create wizard beats (timed to real AI steps).
    "create_intro": (
        "Spinning up a new product starts with the Add-Product wizard. We choose "
        "greenfield to build from scratch, then describe the idea in a sentence."
    ),
    "create_vision": (
        "One click on Articulate Vision sends that to the model, which rewrites it "
        "into a detailed, structured product spec the agents can build from."
    ),
    "create_stack": (
        "From the vision, ProductFactory recommends a technology stack and database, "
        "and pre-selects them, with its reasoning shown."
    ),
    "create_ui": (
        "For web products it also suggests a matching UI template to start the "
        "frontend from, chosen to fit what you described."
    ),
    "create_backlog": (
        "Then it generates a complete starter backlog — real, themed features with "
        "descriptions — straight from the vision."
    ),
    "create_details": (
        "The final step names the product and would scaffold a fresh GitHub "
        "repository. For this walkthrough we stop here, without creating it."
    ),
    # Product walkthrough — one per expanded-sidebar section.
    "summary": (
        "Open a product and the left sidebar — expanded here — is your map through "
        "it. Summary first: the delivery metrics — features shipped, the approved "
        "backlog still waiting, open pull requests, and overall health."
    ),
    "backlog": (
        "The Backlog is where the work lives. Every feature moves through a strict "
        "lifecycle — pending, approved, designed, implementing, reviewing, and "
        "finally pushed. The manager approves what the agents may pick up; the rest "
        "flows automatically."
    ),
    "feature": (
        "Click any feature to open its full story — the description and acceptance "
        "criteria the designer wrote, its status and priority, the linked pull "
        "request, and the running history of agent comments and decisions."
    ),
    "features": (
        "The Features tab groups work into phases — foundational work first. This is "
        "where the human-in-the-loop gate lives: when a phase finishes, the factory "
        "freezes the next one until the manager reviews and approves it."
    ),
    "prs": (
        "Pull Requests: every coder session opens its own PR straight to main, and "
        "the factory tracks each one as it is reviewed and squash-merged. One "
        "feature, one PR — no giant integration branches."
    ),
    "history": (
        "History is the full audit trail — every agent session ever run on this "
        "product, what it touched, and how it ended. Nothing the agents do is hidden."
    ),
    "corrections": (
        "Corrections surfaces the drift detectors — duplicate schema, oversized "
        "files, insecure settings — that the factory files as fix-it chores, so "
        "quality issues become tracked work instead of quietly rotting."
    ),
    "videos": (
        "The Videos tab holds the product's auto-generated showcase reels: the "
        "product-trainer agent narrates what has shipped, on demand."
    ),
    "live": (
        "Live Session streams an agent's work in real time — as a session runs "
        "inside its container, its log lines appear here as they happen."
    ),
    "settings": (
        "Settings is the per-product control panel — quiet hours, daily session "
        "caps, the phase gate, and the other guardrails that tune how aggressively "
        "the factory works this product."
    ),
    "architecture": (
        "A dedicated architect agent maintains this living architecture document for "
        "every product — the canonical modules, entry points, and rules — and feeds "
        "it back into each coding session, so agents reuse existing code instead of "
        "drifting into duplicates."
    ),
    "closing": (
        "Deterministic gates, rule-based supervisors, and a human phase gate keep the "
        "whole pipeline healthy, around the clock. That is ProductFactory — an "
        "autonomous engineering team you run from a single screen."
    ),
}


# ── Browser-driving helpers ──────────────────────────────────────────────────
async def _smooth_scroll(page, total_px: int, budget: float):
    """Scroll down by total_px over `budget` seconds (fills the narration)."""
    budget = max(0.4, budget)
    steps = max(8, int(budget * 8))
    per = total_px / steps
    dt = (budget * 0.85) / steps
    for _ in range(steps):
        await page.mouse.wheel(0, per)
        await asyncio.sleep(dt)
    await asyncio.sleep(budget * 0.15)


async def _scroll_el(page, selector, budget, total=1400):
    budget = max(0.4, budget)
    steps = max(8, int(budget * 6))
    per = total / steps
    dt = (budget * 0.85) / steps
    for _ in range(steps):
        await page.evaluate(
            "([s, d]) => { const e = document.querySelector(s); if (e) e.scrollTop += d; }",
            [selector, per],
        )
        await asyncio.sleep(dt)
    await asyncio.sleep(budget * 0.15)


async def _wait_for(page, js_expr, timeout=22.0):
    waited = 0.0
    while waited < timeout:
        try:
            if await page.evaluate(js_expr):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
        waited += 0.5
    return False


async def _expand_sidebar(page):
    """Belt-and-suspenders: ensure the product-page sidebar is expanded."""
    try:
        await page.evaluate(
            "var s=document.getElementById('pf-sidebar'); if(s) s.classList.remove('collapsed');"
        )
    except Exception:  # noqa: BLE001
        pass


async def _click_sidebar(page, title, wait=1.0):
    try:
        await page.click(f"a.pf-sidebar-item[title='{title}']", timeout=4000)
    except Exception as e:  # noqa: BLE001
        print(f"  [tab '{title}'] non-fatal: {e}")
    await asyncio.sleep(wait)


# Real product names → neutral labels. Applied after every navigation.
REDACT_PAIRS: list[tuple[str, str]] = []


async def _redact_names(page):
    if not REDACT_PAIRS:
        return
    try:
        await page.evaluate(
            """(pairs) => {
              document.querySelectorAll('.product-card-mark').forEach((el) => { el.textContent = 'PF'; });
              const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              const nodes = []; while (walk.nextNode()) nodes.push(walk.currentNode);
              for (const n of nodes) {
                let t = n.nodeValue;
                for (const [k, v] of pairs) { if (k && t.includes(k)) t = t.split(k).join(v); }
                if (t !== n.nodeValue) n.nodeValue = t;
              }
            }""",
            REDACT_PAIRS,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [redact] non-fatal: {e}")


# ── Scenes ───────────────────────────────────────────────────────────────────
# Intro is TWO narration beats over the design.html showcase, in one clip.
INTRO_BEATS = ["intro", "intro2"]


async def act_intro(page, base, audio_dur):
    """Open the real design.html (file://) and narrate the 'why'. Returns
    [(audio_key, offset_seconds), ...] for the two intro beats."""
    offsets = []
    t0 = time.monotonic()
    try:
        await page.goto(DESIGN_HTML.as_uri(), wait_until="domcontentloaded")
    except Exception as e:  # noqa: BLE001
        print(f"  [intro] design.html load: {e}")
        await page.goto(base + "/", wait_until="domcontentloaded")
    await asyncio.sleep(2.0)  # let the hero + D3 graph animate in

    # Beat 1 — hero + slow reveal of the system map.
    offsets.append(("intro", time.monotonic() - t0))
    await _smooth_scroll(page, 900, audio_dur["intro"] - 0.3)
    # Beat 2 — continue through the system graph / lower sections.
    offsets.append(("intro2", time.monotonic() - t0))
    await _smooth_scroll(page, 1400, audio_dur["intro2"] - 0.3)
    return offsets


async def act_grid(page, base, ctx, budget):
    await page.goto(base + "/", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.0)
    await _smooth_scroll(page, 1100, budget - 1.0)


CREATE_BEATS = ["create_intro", "create_vision", "create_stack",
                "create_ui", "create_backlog", "create_details"]

VISION_SEED = (
    "A community marketplace where neighbours lend and borrow tools and household "
    "equipment, with listings, reservations, and reviews."
)


async def act_create(page, base, audio_dur, do_create: bool):
    offsets = []
    t0 = time.monotonic()

    async def beat(key):
        offsets.append((key, time.monotonic() - t0))
        return time.monotonic()

    async def pad(started, key):
        remain = audio_dur[key] + 0.3 - (time.monotonic() - started)
        if remain > 0:
            await asyncio.sleep(remain)

    s = await beat("create_intro")
    await page.goto(base + "/", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.0)
    try:
        await page.evaluate("typeof openWizard==='function' && openWizard()")
        await asyncio.sleep(1.0)
        await page.evaluate("typeof selectType==='function' && selectType('greenfield')")
        await asyncio.sleep(0.8)
        await page.evaluate("typeof wizardNext==='function' && wizardNext()")
        await asyncio.sleep(0.8)
        ta = page.locator("#gf-vision")
        if await ta.count():
            await ta.fill("")
            for ch in VISION_SEED:
                await ta.type(ch, delay=9)
    except Exception as e:  # noqa: BLE001
        print(f"  [create:intro] {e}")
    await pad(s, "create_intro")

    s = await beat("create_vision")
    try:
        await page.click("#articulate-btn", timeout=4000)
        await _wait_for(page, "document.getElementById('articulate-status') && "
                              "document.getElementById('articulate-status').textContent.indexOf('Done') >= 0", 25)
        await asyncio.sleep(1.0)
    except Exception as e:  # noqa: BLE001
        print(f"  [create:vision] {e}")
    await pad(s, "create_vision")

    s = await beat("create_stack")
    try:
        await page.evaluate("typeof wizardToStack==='function' && wizardToStack()")
        await _wait_for(page, "!!document.querySelector('#stack-recommendation .recommendation-title') "
                              "|| !!document.querySelector('.stack-option.recommended-badge')", 22)
        await asyncio.sleep(1.5)
    except Exception as e:  # noqa: BLE001
        print(f"  [create:stack] {e}")
    await pad(s, "create_stack")

    s = await beat("create_ui")
    try:
        await page.evaluate("typeof wizardToUI==='function' && wizardToUI()")
        await _wait_for(page, "!!document.querySelector('#ui-recommendation .recommendation-title') "
                              "|| !!document.querySelector('.ui-template-card.recommended-badge') "
                              "|| !!document.querySelector('#wp-5-backlog.active')", 18)
        await asyncio.sleep(1.2)
    except Exception as e:  # noqa: BLE001
        print(f"  [create:ui] {e}")
    await pad(s, "create_ui")

    s = await beat("create_backlog")
    try:
        await page.evaluate("typeof wizardToBacklog==='function' && wizardToBacklog()")
        await asyncio.sleep(0.6)
        await page.click("#suggest-btn", timeout=4000)
        got = await _wait_for(page, "!document.getElementById('gf-suggestions').classList.contains('hidden') "
                                    "&& document.querySelectorAll('#suggestions-grid .suggestion-card').length > 0", 40)
        if got:
            await asyncio.sleep(1.0)
            await _scroll_el(page, "#wp-5-backlog", 7.0, total=1100)
    except Exception as e:  # noqa: BLE001
        print(f"  [create:backlog] {e}")
    await pad(s, "create_backlog")

    s = await beat("create_details")
    try:
        await page.evaluate("typeof wizardToDetailsFromBacklog==='function' && wizardToDetailsFromBacklog()")
        await asyncio.sleep(0.8)
        await page.evaluate(
            """() => {
              const n = document.getElementById('gf-name'); if (n) n.value = 'NeighbourLend';
              const r = document.getElementById('gf-repo');
              if (r) { r.value = 'neighbour-lend'; r.dispatchEvent(new Event('input', {bubbles: true})); }
            }"""
        )
        if do_create:
            await page.evaluate("typeof submitGreenfield==='function' && submitGreenfield()")
            await asyncio.sleep(3.0)
    except Exception as e:  # noqa: BLE001
        print(f"  [create:details] {e}")
    await pad(s, "create_details")
    await asyncio.sleep(0.6)
    return offsets


async def act_section(page, base, budget, product_id, sidebar_title, scroll_px):
    """Generic: open the product, expand the sidebar, click a section, scroll."""
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _expand_sidebar(page)
    await asyncio.sleep(0.6)
    if sidebar_title:
        await _click_sidebar(page, sidebar_title)
    await _redact_names(page)
    await _smooth_scroll(page, scroll_px, budget - 1.2)


async def act_feature(page, base, budget, product_id):
    """Open a feature's slide-out panel and reveal story / AC / details."""
    await page.goto(f"{base}/product/{product_id}", wait_until="domcontentloaded")
    await _expand_sidebar(page)
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
        await asyncio.sleep(1.6)
    except Exception as e:  # noqa: BLE001
        print(f"  [feature] non-fatal: {e}")
    await _redact_names(page)
    await _scroll_el(page, "#fp-body", budget - 2.2)


async def act_architecture(page, base, budget, product_id):
    await page.goto(f"{base}/product/{product_id}/architecture", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.2)
    await _smooth_scroll(page, 1100, budget - 1.4)


async def act_admin_closing(page, base, budget):
    await page.goto(base + "/", wait_until="domcontentloaded")
    await _redact_names(page)
    await asyncio.sleep(1.0)
    await _smooth_scroll(page, 400, budget - 1.0)


# (scene_key, sidebar_title or None, scroll_px) — the expanded-sidebar tour.
SECTION_SCENES = [
    ("summary",     "Summary",        700),
    ("backlog",     "Backlog",        1200),
    ("feature",     None,             0),     # special — slide-out panel
    ("features",    "Features",       1400),
    ("prs",         "Pull Requests",  900),
    ("history",     "History",        900),
    ("corrections", "Corrections",    700),
    ("videos",      "Videos",         500),
    ("live",        "Live Session",   500),
    ("settings",    "Settings",       900),
]

SCENE_ORDER = (
    ["intro", "grid", "create"]
    + [k for k, _, _ in SECTION_SCENES]
    + ["architecture", "closing"]
)

HEAD_TRIM = 1.5    # drop the page-load flash at the start of each clip
TAIL_PAD = 0.5     # short silent tail after the VO (was 1.2 → cut dead air)
CROSSFADE = 0.5


# ── Recording ────────────────────────────────────────────────────────────────
async def _record_scene(browser, base, env, key, audio_dur, do_create, product_id, video_dir):
    ctx = await browser.new_context(
        viewport=VIEWPORT,
        record_video_dir=str(video_dir),
        record_video_size=VIEWPORT,
        http_credentials={
            "username": env.get("PM_USERNAME", ""),
            "password": env.get("PM_PASSWORD", ""),
        },
    )
    await ctx.add_init_script(INIT_SCRIPT)
    page = await ctx.new_page()
    multi_offsets = None
    budget = HEAD_TRIM + audio_dur.get(key, 9.0) + TAIL_PAD
    try:
        if key == "intro":
            multi_offsets = await act_intro(page, base, audio_dur)
        elif key == "create":
            multi_offsets = await act_create(page, base, audio_dur, do_create)
        elif key == "grid":
            await act_grid(page, base, ctx, budget)
        elif key == "feature":
            await act_feature(page, base, budget, product_id)
        elif key == "architecture":
            await act_architecture(page, base, budget, product_id)
        elif key == "closing":
            await act_admin_closing(page, base, budget)
        else:  # a SECTION_SCENES entry
            title, px = next(((t, p) for k, t, p in SECTION_SCENES if k == key), (None, 800))
            await act_section(page, base, budget, product_id, title, px)
    finally:
        video = page.video
        await ctx.close()
    webm = Path(await video.path()) if video else None
    return webm, multi_offsets


async def _record(base, env, audio_dur, do_create, product_id, video_dir):
    from playwright.async_api import async_playwright

    clips: dict[str, Path] = {}
    multi: dict[str, list] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        for i, key in enumerate(SCENE_ORDER):
            print(f"  scene {i + 1:2}/{len(SCENE_ORDER)}  '{key}'")
            scene_dir = video_dir / key
            scene_dir.mkdir(parents=True, exist_ok=True)
            webm, offs = await _record_scene(
                browser, base, env, key, audio_dur, do_create, product_id, scene_dir
            )
            clips[key] = webm
            if offs:
                multi[key] = offs
        await browser.close()
    return clips, multi


# ── Captions ─────────────────────────────────────────────────────────────────
def _caption_clips(text, start, dur, W, H):
    """Bottom-centre burned-in captions for `text`, chunked across `dur`."""
    if not CAPTIONS_ENABLED or not FONT_PATH:
        return []
    from moviepy import TextClip
    words = text.split()
    if not words:
        return []
    n = 9
    chunks = [" ".join(words[i:i + n]) for i in range(0, len(words), n)]
    per = dur / len(chunks)
    out = []
    for i, ch in enumerate(chunks):
        try:
            tc = (
                TextClip(font=FONT_PATH, text=ch, font_size=38, color="white",
                         stroke_color="black", stroke_width=3, method="caption",
                         size=(int(W * 0.84), None), text_align="center")
                .with_start(start + i * per)
                .with_duration(per + 0.05)
                .with_position(("center", int(H * 0.80)))
            )
            out.append(tc)
        except Exception as e:  # noqa: BLE001
            print(f"  [caption] non-fatal: {e}")
    return out


def _assemble(clips, audio, out_mp4, multi):
    from moviepy import (VideoFileClip, AudioFileClip, CompositeAudioClip,
                         CompositeVideoClip, concatenate_videoclips, vfx)

    W, H = VIEWPORT["width"], VIEWPORT["height"]
    segs = []
    for i, key in enumerate(SCENE_ORDER):
        wp = clips.get(key)
        if not wp or not wp.exists():
            print(f"  [assemble] missing clip for '{key}', skipping")
            continue
        v = VideoFileClip(str(wp))
        caps = []

        if key in multi:  # multi-beat scene (intro, create)
            beat_clips, last_end = [], 0.0
            for akey, off in multi[key]:
                ac = AudioFileClip(str(audio[akey]))
                start = max(0.0, off - HEAD_TRIM)
                beat_clips.append(ac.with_start(start))
                caps += _caption_clips(NARRATIONS[akey], start, ac.duration, W, H)
                last_end = max(last_end, start + ac.duration)
            aud = CompositeAudioClip(beat_clips)
            core = last_end + TAIL_PAD
        else:
            aud = AudioFileClip(str(audio[key]))
            caps += _caption_clips(NARRATIONS[key], 0.0, aud.duration, W, H)
            core = aud.duration + TAIL_PAD

        end = min(v.duration, HEAD_TRIM + core)
        v = v.subclipped(min(HEAD_TRIM, max(0.0, v.duration - 0.1)), end)
        if caps:
            v = CompositeVideoClip([v, *caps])
        v = v.with_audio(aud)
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


# ── Setup helpers ────────────────────────────────────────────────────────────
def _pick_product(env, base) -> int:
    try:
        import httpx
        auth = (env.get("PM_USERNAME", ""), env.get("PM_PASSWORD", ""))
        prods = httpx.get(base + "/api/products", auth=auth, timeout=8).json()
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
    try:
        import httpx
        auth = (env.get("PM_USERNAME", ""), env.get("PM_PASSWORD", ""))
        prods = httpx.get(base + "/api/products", auth=auth, timeout=8).json()
        names = sorted({(p.get("name") or "").strip() for p in prods if (p.get("name") or "").strip()},
                       key=len, reverse=True)
        return [(n, f"Product {i + 1}") for i, n in enumerate(names)]
    except Exception as e:  # noqa: BLE001
        print(f"  [redact] could not fetch product names: {e}")
        return []


def _captions_selftest() -> bool:
    """Confirm moviepy TextClip can render with the resolved font."""
    if not FONT_PATH:
        return False
    try:
        from moviepy import TextClip
        TextClip(font=FONT_PATH, text="test", font_size=30, color="white",
                 method="label")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [captions] disabled — TextClip self-test failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true",
                    help="Actually submit the greenfield wizard (creates a real repo).")
    ap.add_argument("--product-id", type=int, default=None)
    ap.add_argument("--no-captions", action="store_true", help="Skip burned-in captions.")
    args = ap.parse_args()

    env = _load_env()
    base = env.get("PF_BASE_URL", "http://localhost:8080").rstrip("/")
    if not env.get("PM_USERNAME") or not env.get("PM_PASSWORD"):
        print("ERROR: PM_USERNAME / PM_PASSWORD not set (.env or environment).")
        sys.exit(1)
    if not DESIGN_HTML.exists():
        print(f"WARN: {DESIGN_HTML} missing — intro will fall back to the dashboard.")

    try:
        import httpx
        httpx.get(base + "/", auth=(env["PM_USERNAME"], env["PM_PASSWORD"]), timeout=6)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: PM website not reachable at {base} ({e}). Start it with "
              f"`docker compose up -d`.")
        sys.exit(1)

    global FONT_PATH, CAPTIONS_ENABLED, REDACT_PAIRS
    FONT_PATH = _resolve_font()
    CAPTIONS_ENABLED = (not args.no_captions) and _captions_selftest()
    print(f"Captions: {'ON' if CAPTIONS_ENABLED else 'OFF'} (font={FONT_PATH})")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REDACT_PAIRS = _build_redact_pairs(env, base)
    print(f"Redacting {len(REDACT_PAIRS)} product name(s).")
    product_id = args.product_id or _pick_product(env, base)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_mp4 = OUTPUT_DIR / f"productfactory_walkthrough_{ts}.mp4"
    print(f"Recording → {out_mp4}  (showcasing product #{product_id})")

    import tempfile
    # ignore_cleanup_errors: on Windows, Playwright/moviepy may still hold a
    # .webm handle when the tempdir is torn down — without this the (successful)
    # render exits 1 on a cosmetic cleanup PermissionError.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        print("Phase 1: synthesising narration (Edge TTS)…")
        audio = asyncio.run(_synth(NARRATIONS, tmp))
        dur = _durations(audio)

        print("Phase 2: recording scenes (Playwright)…")
        clips, multi = asyncio.run(_record(base, env, dur, args.create, product_id, tmp))
        if not any(p and p.exists() for p in clips.values()):
            print("ERROR: no video captured.")
            sys.exit(1)

        print("Phase 3: gluing scenes + voiceover + captions (moviepy)…")
        _assemble(clips, audio, out_mp4, multi)

    size_mb = out_mp4.stat().st_size // 1024 // 1024
    print(f"Done! → {out_mp4}  ({size_mb} MB)")


if __name__ == "__main__":
    main()
