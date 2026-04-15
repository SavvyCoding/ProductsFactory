"""
ProductFactory showcase video — compelling story arc with TTS narration.

Slides:
  1. Title — "ProductFactory"
  2. The Problem — slow, manual software development
  3. The Vision — autonomous agents shipping code 24/7
  4. The Architecture — three subsystems working together
  5. 11 AI Personas — the full agent team
  6. The Pipeline — feature to shipped code
  7. Auto-Merge & Review — quality gates with zero human bottleneck
  8. Real Results — what it ships autonomously
  9. Built on Claude — the AI backbone
 10. Outro — "Code While You Sleep"

Run:
    python scripts/build_pf_video.py
Output saved to: output/productfactory_story_<timestamp>.mp4
"""

import asyncio
import logging
import sys
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("pf_video")

# ── Design tokens ─────────────────────────────────────────────────────────────
W, H   = 1280, 720
BG     = (10,  14,  26)    # deep navy
FG     = (248, 250, 252)   # white
ACCENT = (99,  102, 241)   # indigo-500
GREEN  = (34,  197, 94)    # emerald-500
MUTED  = (148, 163, 184)   # slate-400
CARD   = (22,  31,  54)    # slightly lighter navy for cards

_FONT_PATH = None


def _resolve_font(size: int):
    from PIL import ImageFont
    global _FONT_PATH
    if _FONT_PATH is None:
        for candidate in [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]:
            try:
                ImageFont.truetype(candidate, 12)
                _FONT_PATH = candidate
                break
            except Exception:
                continue
    if _FONT_PATH:
        try:
            return ImageFont.truetype(_FONT_PATH, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _bold_font(size: int):
    from PIL import ImageFont
    for candidate in [
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return _resolve_font(size)


def _text_w(draw, text: str, font) -> int:
    try:
        return draw.textbbox((0, 0), text, font=font)[2]
    except Exception:
        return len(text) * 10


def _draw_centered(draw, y: int, text: str, font, color, max_width: int = W - 120) -> int:
    """Word-wrap and center text. Returns y below the last line."""
    try:
        avg_w = max(font.getbbox("M")[2], 1)
    except Exception:
        avg_w = 10
    chars = max(10, max_width // avg_w)
    for line in textwrap.wrap(text, width=chars) or [text]:
        try:
            bb = draw.textbbox((0, 0), line, font=font)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            lw, lh = len(line) * avg_w, 20
        draw.text(((W - lw) // 2, y), line, fill=color, font=font)
        y += lh + 12
    return y


def _draw_left(draw, x: int, y: int, text: str, font, color, max_width: int = W - 120) -> int:
    try:
        avg_w = max(font.getbbox("M")[2], 1)
    except Exception:
        avg_w = 10
    chars = max(10, max_width // avg_w)
    for line in textwrap.wrap(text, width=chars) or [text]:
        draw.text((x, y), line, fill=color, font=font)
        try:
            bb = draw.textbbox((0, 0), line, font=font)
            y += bb[3] - bb[1] + 10
        except Exception:
            y += 24
    return y


def _accent_bar(draw, y_center: int, width: int = W):
    draw.rectangle([(0, y_center - 3), (width, y_center + 3)], fill=ACCENT)


def _pill(draw, x: int, y: int, text: str, font, bg=ACCENT, fg=FG):
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
    except Exception:
        tw, th = len(text) * 10, 20
    pad_x, pad_y = 16, 8
    draw.rounded_rectangle(
        [x, y, x + tw + pad_x * 2, y + th + pad_y * 2],
        radius=8, fill=bg,
    )
    draw.text((x + pad_x, y + pad_y), text, fill=fg, font=font)
    return x + tw + pad_x * 2 + 12, y + th + pad_y * 2


# ── Slide builders ─────────────────────────────────────────────────────────────

def _slide_title(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Gradient-ish diagonal accent lines
    for i in range(0, W + H, 60):
        d.line([(i, 0), (0, i)], fill=(20, 28, 54), width=1)

    # Large glowing dot behind title
    d.ellipse([(W//2 - 220, H//2 - 160), (W//2 + 220, H//2 + 100)],
              fill=(18, 22, 50))

    y = H // 5
    y = _draw_centered(d, y, "ProductFactory", _bold_font(88), FG)
    y += 16
    y = _draw_centered(d, y, "The 24/7 Autonomous Development System", _resolve_font(32), ACCENT)
    y += 40
    _draw_centered(d, y, "AI agents that design, code, review and ship — while you sleep",
                   _resolve_font(24), MUTED)

    d.text((W - 320, H - 44), "Powered by Claude Code · Anthropic", fill=MUTED, font=_resolve_font(18))
    img.save(str(path))


def _slide_problem(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _accent_bar(d, 90)

    y = 30
    _draw_centered(d, y, "The Problem", _bold_font(52), FG)
    y = 120

    problems = [
        ("⏱", "Features take days or weeks to ship"),
        ("🔁", "Developers context-switch between reviewing, coding, testing"),
        ("😴", "Development stops when the team sleeps"),
        ("💸", "Engineering time is the scarcest and most expensive resource"),
    ]
    for icon, text in problems:
        d.rounded_rectangle([100, y, W - 100, y + 72], radius=10, fill=CARD)
        d.text((130, y + 18), icon, fill=ACCENT, font=_resolve_font(30))
        _draw_left(d, 200, y + 20, text, _resolve_font(28), FG, max_width=W - 280)
        y += 96

    img.save(str(path))


def _slide_vision(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Big glowing circle
    d.ellipse([(W//2 - 280, H//2 - 240), (W//2 + 280, H//2 + 180)], fill=(14, 20, 44))
    _accent_bar(d, H // 2)

    y = H // 8
    y = _draw_centered(d, y, "What if software built itself?", _bold_font(54), FG)
    y += 24
    y = _draw_centered(d, y,
        "ProductFactory turns a product vision and a feature backlog "
        "into shipped, reviewed, merged code — automatically.",
        _resolve_font(28), MUTED)
    y += 36
    _draw_centered(d, y, "No developer required. No bottlenecks. No downtime.",
                   _resolve_font(30), ACCENT)
    img.save(str(path))


def _slide_architecture(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _accent_bar(d, 90)

    _draw_centered(d, 20, "Three Subsystems, One Machine", _bold_font(48), FG)

    boxes = [
        (60,  160, "Orchestrator", "Windows poller — picks products,\nlaunches Docker sessions,\nmanages the agent lifecycle"),
        (460, 160, "PM Dashboard", "FastAPI website — manage products,\napprove features, watch live\nsession logs in real-time"),
        (860, 160, "Agent Image", "Docker container — Claude Code\nruns inside, reads prompts,\nwrites code, opens PRs"),
    ]
    for bx, by, title, body in boxes:
        d.rounded_rectangle([bx, by, bx + 340, by + 340], radius=14, fill=CARD)
        d.rounded_rectangle([bx, by, bx + 340, by + 6], radius=4, fill=ACCENT)
        _draw_centered(d, by + 22, title, _bold_font(26), FG, max_width=300)
        _draw_left(d, bx + 20, by + 80, body, _resolve_font(22), MUTED, max_width=300)

    # Arrows
    for ax in [400, 800]:
        d.line([(ax, 330), (ax + 60, 330)], fill=ACCENT, width=3)
        d.polygon([(ax + 60, 320), (ax + 80, 330), (ax + 60, 340)], fill=ACCENT)

    img.save(str(path))


def _slide_personas(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _accent_bar(d, 90)

    _draw_centered(d, 20, "11 AI Personas — A Full Engineering Team", _bold_font(44), FG)

    personas = [
        ("Designer",   GREEN),
        ("Coder",      ACCENT),
        ("Reviewer",   ACCENT),
        ("QA Tester",  GREEN),
        ("Security",   (239, 68,  68)),
        ("Recommender",(245,158, 11)),
        ("Documenter", MUTED),
        ("Analytics",  MUTED),
        ("Refactorer", MUTED),
        ("DevOps",     MUTED),
        ("Planner",    (167,139,250)),
    ]

    x, y = 80, 130
    row_h = 82
    col_w = (W - 160) // 4
    for i, (name, color) in enumerate(personas):
        col = i % 4
        row = i // 4
        bx = x + col * col_w
        by = y + row * row_h
        d.rounded_rectangle([bx, by, bx + col_w - 16, by + row_h - 12], radius=8, fill=CARD)
        d.rounded_rectangle([bx, by, bx + 6, by + row_h - 12], radius=4, fill=color)
        _draw_left(d, bx + 18, by + 18, name, _bold_font(24), FG, max_width=col_w - 40)

    img.save(str(path))


def _slide_pipeline(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _accent_bar(d, 90)

    _draw_centered(d, 20, "Feature → Shipped: The Pipeline", _bold_font(48), FG)

    steps = [
        ("Pending",      "PM approves\nthe idea",            MUTED),
        ("Approved",     "Designer writes\nthe design doc",  GREEN),
        ("Designed",     "Coder implements\n& opens PR",     ACCENT),
        ("Reviewing",    "Reviewer checks\nquality & tests", (245,158,11)),
        ("Pushed",       "Auto-merged to\nmain branch",      GREEN),
    ]
    bw = 196
    gap = (W - len(steps) * bw) // (len(steps) + 1)
    by = 220
    bh = 200
    for i, (label, desc, color) in enumerate(steps):
        bx = gap + i * (bw + gap)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=12, fill=CARD)
        d.rounded_rectangle([bx, by, bx + bw, by + 6], radius=4, fill=color)
        _draw_centered(d, by + 20, label, _bold_font(22), color, max_width=bw - 10)
        _draw_centered(d, by + 60, desc, _resolve_font(19), MUTED, max_width=bw - 20)
        # Arrow
        if i < len(steps) - 1:
            ax = bx + bw + gap // 2
            ay = by + bh // 2
            d.line([(bx + bw + 4, ay), (ax + 8, ay)], fill=ACCENT, width=2)
            d.polygon([(ax + 8, ay - 7), (ax + 18, ay), (ax + 8, ay + 7)], fill=ACCENT)

    _draw_centered(d, by + bh + 28,
                   "Each step is autonomous — agents hand off to each other without human intervention",
                   _resolve_font(24), MUTED)
    img.save(str(path))


def _slide_automerge(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _accent_bar(d, 90)

    _draw_centered(d, 20, "Quality Without the Bottleneck", _bold_font(48), FG)

    points = [
        (GREEN,  "Reviewer checks every PR diff against the design doc and acceptance criteria"),
        (ACCENT, "Security checklist: SQL injection, XSS, hardcoded secrets, missing auth — every time"),
        (GREEN,  "High-confidence approvals auto-merge to main; low-confidence waits for human review"),
        (ACCENT, "QA Tester adds automated tests to every PR branch before review even starts"),
        ((239,68,68), "Security Auditor files bug features for any OWASP issue it finds"),
    ]
    y = 130
    for color, text in points:
        d.rounded_rectangle([80, y, W - 80, y + 68], radius=10, fill=CARD)
        d.ellipse([100, y + 22, 124, y + 46], fill=color)
        _draw_left(d, 144, y + 18, text, _resolve_font(24), FG, max_width=W - 220)
        y += 82

    img.save(str(path))


def _slide_results(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Big numbers layout
    _draw_centered(d, 30, "What ProductFactory Ships", _bold_font(52), FG)

    stats = [
        ("24 / 7", "Development never stops"),
        ("11",     "Specialised AI agents"),
        ("0",      "Lines of code written by humans"),
        ("∞",      "Features in the backlog"),
    ]
    col_w = W // 2
    positions = [(0, 160), (col_w, 160), (0, 400), (col_w, 400)]
    for (bx, by), (number, label) in zip(positions, stats):
        d.rounded_rectangle([bx + 40, by, bx + col_w - 40, by + 190], radius=16, fill=CARD)
        _draw_centered(d, by + 20, number, _bold_font(72), ACCENT,
                       max_width=col_w - 100)
        _draw_centered(d, by + 120, label, _resolve_font(26), MUTED,
                       max_width=col_w - 100)

    img.save(str(path))


def _slide_claude(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.ellipse([(W//2 - 200, H//2 - 200), (W//2 + 200, H//2 + 200)], fill=(14, 20, 44))
    _accent_bar(d, H // 2)

    y = H // 7
    y = _draw_centered(d, y, "Built on Claude Code", _bold_font(60), FG)
    y += 20
    y = _draw_centered(d, y,
        "Every agent — Designer, Coder, Reviewer, QA, Security and more — "
        "runs as a Claude Code session inside an isolated Docker container.",
        _resolve_font(28), MUTED)
    y += 30
    y = _draw_centered(d, y,
        "Claude reads prompts, browses the codebase, writes code, opens pull requests "
        "and reports results back through a live-polled session file.",
        _resolve_font(26), MUTED)
    y += 30
    _draw_centered(d, y, "Anthropic's most capable model — doing real engineering work.",
                   _resolve_font(28), ACCENT)
    img.save(str(path))


def _slide_outro(path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    for i in range(0, W + H, 60):
        d.line([(i, 0), (0, i)], fill=(18, 24, 48), width=1)

    d.ellipse([(W//2 - 300, H//2 - 220), (W//2 + 300, H//2 + 180)], fill=(12, 18, 40))

    y = H // 6
    y = _draw_centered(d, y, "ProductFactory", _bold_font(82), FG)
    y += 10
    y = _draw_centered(d, y, "Code While You Sleep", _bold_font(44), ACCENT)
    y += 40
    y = _draw_centered(d, y,
        "A 24/7 autonomous engineering team — from idea to merged PR, "
        "with zero human intervention required.",
        _resolve_font(26), MUTED)
    y += 36
    _draw_centered(d, y, "github.com/SavvyCoding/ProductsFactory", _resolve_font(22), MUTED)

    d.text((W - 340, H - 44), "Built with ProductFactory + Claude Code", fill=MUTED,
           font=_resolve_font(18))
    img.save(str(path))


# ── Narration script ──────────────────────────────────────────────────────────

SLIDES = [
    (_slide_title, "Introducing ProductFactory — the 24/7 autonomous development system that turns product ideas into shipped code, while you sleep."),
    (_slide_problem, "Building software is slow and expensive. Features take days or weeks. Developers lose time to context-switching between writing code, reviewing pull requests, writing tests, and keeping documentation up to date. And when the team logs off, development stops completely."),
    (_slide_vision, "What if software could build itself? ProductFactory answers that question. Give it a product vision and a feature backlog, and it designs, implements, reviews, and merges code — automatically, continuously, with no developer required."),
    (_slide_architecture, "ProductFactory has three subsystems working in concert. The Orchestrator — a Windows poller — selects products, launches isolated Docker containers, and manages the full agent lifecycle. The PM Dashboard is a FastAPI website where you approve features and watch live session logs. And inside each Docker container, Claude Code runs as the agent — reading its prompt, browsing the codebase, writing code, and opening pull requests."),
    (_slide_personas, "ProductFactory fields eleven specialised AI personas — a complete engineering team. The Designer writes the technical spec. The Coder implements it and opens a pull request. The Reviewer checks every diff against acceptance criteria. The QA Tester adds automated tests. The Security Auditor runs the OWASP checklist. And supporting roles — Recommender, Documenter, Analytics, Refactorer, DevOps, and Planner — keep the product healthy and growing."),
    (_slide_pipeline, "Every feature follows the same pipeline: a PM approves the idea, the Designer writes the spec, the Coder implements and opens a pull request, the Reviewer approves the quality, and the change is merged to main. Each hand-off is automatic. No human stands in the critical path."),
    (_slide_automerge, "Quality is never sacrificed for speed. The Reviewer checks every pull request against the design document and acceptance criteria. A dedicated security checklist catches SQL injection, cross-site scripting, hardcoded secrets, and missing auth on every run. High-confidence approvals auto-merge instantly. Lower confidence flags wait for a human second opinion. The QA Tester commits automated tests before review even begins."),
    (_slide_results, "The result: development runs 24 hours a day, 7 days a week, powered by 11 specialised agents, requiring zero lines of code written by a human engineer. The backlog is the only limit."),
    (_slide_claude, "Under the hood, every agent is a Claude Code session — Anthropic's most capable model — running inside an isolated Docker container. Claude reads its persona prompt, browses the repository, writes real production code, opens pull requests, and writes results back through a live-polled session file that the orchestrator applies to the database in real time."),
    (_slide_outro, "ProductFactory. An autonomous engineering team that never clocks out. From idea to merged pull request — with zero human intervention required. Code while you sleep."),
]


def build(output_dir: Path) -> Path:
    try:
        from PIL import Image          # noqa
        import edge_tts                # noqa
        from moviepy import ImageClip  # noqa
    except ImportError as e:
        log.error(f"Missing dependency: {e}\nInstall: pip install Pillow edge-tts moviepy")
        sys.exit(1)

    from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = output_dir / f"productfactory_story_{timestamp}.mp4"

    log.info(f"Building ProductFactory story video → {video_path}")

    tts_loop = asyncio.new_event_loop()

    def _tts(text: str, audio_path: Path):
        async def _run():
            comm = edge_tts.Communicate(text, voice="en-US-AriaNeural")
            await comm.save(str(audio_path))
        tts_loop.run_until_complete(_run())

    clips = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        for i, (slide_fn, narration) in enumerate(SLIDES):
            log.info(f"  Slide {i+1}/{len(SLIDES)}: {slide_fn.__name__}")
            slide_path = tmp_path / f"slide_{i:02d}.png"
            audio_path = tmp_path / f"slide_{i:02d}.mp3"

            slide_fn(slide_path)
            _tts(narration, audio_path)

            audio = AudioFileClip(str(audio_path))
            pause = 1.5 if i == len(SLIDES) - 1 else 0.5
            clip = (
                ImageClip(str(slide_path))
                .with_duration(audio.duration + pause)
                .with_audio(audio)
            )
            clips.append(clip)

        tts_loop.close()
        log.info("Assembling final video…")

        final = None
        try:
            final = concatenate_videoclips(clips, method="compose")
            final.write_videofile(
                str(video_path),
                fps=24,
                codec="libx264",
                audio_codec="aac",
                logger=None,
            )
            log.info(f"Done! → {video_path}")
            return video_path
        finally:
            if final:
                try:
                    final.close()
                except Exception:
                    pass
            for c in clips:
                try:
                    c.close()
                except Exception:
                    pass


if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent
    out = repo_root / "output"
    build(out)
