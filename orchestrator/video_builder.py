"""
Generates a product showcase video (MP4) with Microsoft Edge Neural TTS narration.
Saves to {working_dir}/output/product_video_{timestamp}.mp4

Slides:
  1. Title + description
  2. One slide per shipped feature (max 8)
  3. Outro (feature count summary)

After generation, commit_and_push_output() adds the output/ folder to git
and pushes to the remote — so the video is always accessible from the repo.

Dependencies (not bundled — install separately):
    pip install Pillow edge-tts moviepy
ffmpeg must be on PATH (used by moviepy under the hood).
"""

import logging
import os
import subprocess
import tempfile
import textwrap

from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("poller.video")

PM_API_URL = os.environ.get("PM_API_URL", "http://localhost:8080")

# ── Slide design ──────────────────────────────────────────────────────────────
W, H    = 1280, 720
BG      = (15,  23,  42)   # slate-900
FG      = (248, 250, 252)  # slate-50
ACCENT  = (99,  102, 241)  # indigo-500
MUTED   = (148, 163, 184)  # slate-400

_FONT_PATH: Optional[str] = None   # resolved once, cached


def _resolve_font(size: int):
    """Return an ImageFont for the given size, falling back to PIL default."""
    from PIL import ImageFont

    global _FONT_PATH
    if _FONT_PATH is None:
        for candidate in [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "arial.ttf",
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


def _draw_text_centered(draw, y: int, text: str, font, color, max_width: int = W - 120) -> int:
    """
    Draw text centered horizontally, word-wrapping to max_width.
    Returns the y position below the last drawn line.
    """
    try:
        avg_w = font.getbbox("M")[2]
    except Exception:
        avg_w = 10
    chars_per_line = max(10, max_width // max(avg_w, 1))

    for line in textwrap.wrap(text, width=chars_per_line) or [text]:
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            lh = bbox[3] - bbox[1]
        except Exception:
            lw, lh = len(line) * avg_w, 20
        x = max(0, (W - lw) // 2)
        draw.text((x, y), line, fill=color, font=font)
        y += lh + 14
    return y


def _make_title_slide(name: str, description: str, path: Path) -> None:
    from PIL import Image, ImageDraw
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Accent bar across the vertical centre
    draw.rectangle([(0, H // 2 - 4), (W, H // 2 + 4)], fill=ACCENT)

    y = H // 5
    y = _draw_text_centered(draw, y, name,        _resolve_font(72), FG)
    y += 28
    _draw_text_centered(draw, y, description[:140], _resolve_font(30), MUTED)

    draw.text((W - 280, H - 48), "Built by ProductFactory", fill=MUTED, font=_resolve_font(20))
    img.save(str(path))


def _make_feature_slide(index: int, name: str, description: str, path: Path) -> None:
    from PIL import Image, ImageDraw
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    draw.text((60, 54), f"Feature {index}", fill=ACCENT, font=_resolve_font(26))

    y = H // 3 - 30
    y = _draw_text_centered(draw, y, name, _resolve_font(58), FG)
    if description:
        y += 22
        _draw_text_centered(draw, y, description[:180], _resolve_font(30), MUTED)

    img.save(str(path))


def _make_outro_slide(name: str, feature_count: int, path: Path) -> None:
    from PIL import Image, ImageDraw
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    y = H // 5
    y = _draw_text_centered(draw, y, name, _resolve_font(64), FG)
    y += 32
    y = _draw_text_centered(draw, y, f"{feature_count} feature{'s' if feature_count != 1 else ''} shipped autonomously", _resolve_font(38), ACCENT)
    y += 32
    _draw_text_centered(draw, y, "Powered by ProductFactory + Claude Code", _resolve_font(26), MUTED)

    img.save(str(path))


# ── API helpers ───────────────────────────────────────────────────────────────

def _fetch_features(product_id: int) -> list[dict]:
    try:
        r = httpx.get(f"{PM_API_URL}/api/products/{product_id}/features", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Could not fetch features for video: {e}")
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

def build_product_video(product: dict, working_dir: Path) -> Optional[Path]:
    """
    Generate a product showcase MP4 with TTS narration.
    Returns the path to the video file, or None if generation failed or
    dependencies are missing.
    """
    try:
        from PIL import Image                           # noqa — early ImportError check
        import edge_tts                                 # noqa
        from moviepy import ImageClip as _IC            # noqa
    except ImportError as e:
        log.warning(
            f"Video generation skipped — missing dependency: {e}. "
            "Install with: pip install Pillow edge-tts moviepy"
        )
        return None

    import asyncio
    import edge_tts
    from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

    product_id   = product["id"]
    product_name = product.get("name") or "Product"
    description  = (product.get("description") or f"An AI-built {product_name} application.")

    features = _fetch_features(product_id)
    shipped  = [f for f in features if f["status"] in ("Pushed", "Reviewed", "Reviewing")]

    output_dir = working_dir / "output"
    output_dir.mkdir(exist_ok=True)

    video_path = output_dir / "product_video.mp4"

    log.info(f"Building product video for '{product_name}' ({len(shipped)} shipped features) -> {video_path}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clips: list = []

        # Use a dedicated event loop so we don't conflict with any outer loop
        tts_loop = asyncio.new_event_loop()

        def _tts(narration: str, audio_path: Path) -> None:
            """Synthesise speech with Microsoft Edge Neural TTS (en-US-AriaNeural)."""
            async def _run():
                communicate = edge_tts.Communicate(narration, voice="en-US-AriaNeural")
                await communicate.save(str(audio_path))
            tts_loop.run_until_complete(_run())

        def _make_clip(slide_path: Path, narration: str, pause: float = 0.6):
            audio_path = tmp_path / (slide_path.stem + ".mp3")
            _tts(narration, audio_path)
            audio_clip = AudioFileClip(str(audio_path))
            slide_clip = (
                ImageClip(str(slide_path))
                   .with_duration(audio_clip.duration + pause)
                   .with_audio(audio_clip)
            )
            return slide_clip

        # Title
        title_path = tmp_path / "slide_00.png"
        _make_title_slide(product_name, description, title_path)
        clips.append(_make_clip(
            title_path,
            f"Welcome to {product_name}. {description}",
        ))

        # Features (max 8)
        for i, feat in enumerate(shipped[:8], start=1):
            slide_path = tmp_path / f"slide_{i:02d}.png"
            _make_feature_slide(i, feat["name"], feat.get("description") or "", slide_path)
            narration = f"Feature {i}: {feat['name']}."
            if feat.get("description"):
                narration += f" {feat['description']}"
            clips.append(_make_clip(slide_path, narration))

        # Outro
        outro_path = tmp_path / "slide_outro.png"
        _make_outro_slide(product_name, len(shipped), outro_path)
        clips.append(_make_clip(
            outro_path,
            f"{product_name} — {len(shipped)} feature{'s' if len(shipped) != 1 else ''} shipped autonomously by ProductFactory.",
            pause=1.5,
        ))

        tts_loop.close()

        # Assemble
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
            log.info(f"Product video saved: {video_path}")
            return video_path
        except Exception as e:
            log.error(f"Video assembly failed: {e}")
            return None
        finally:
            if final is not None:
                try:
                    final.close()
                except Exception:
                    pass
            for c in clips:
                try:
                    c.close()
                except Exception:
                    pass


# ── Git commit + push ─────────────────────────────────────────────────────────

def commit_and_push_output(working_dir: Path, deploy_key: Optional[Path] = None) -> bool:
    """
    Stage output/, commit, and push to origin.
    Returns True on success or when there is nothing new to commit.
    """
    git_env = os.environ.copy()
    if deploy_key and deploy_key.exists():
        git_env["GIT_SSH_COMMAND"] = (
            f"ssh -i {deploy_key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        )

    def _git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=str(working_dir),
            env=git_env,
            capture_output=True,
            text=True,
        )

    try:
        _git("add", "output/")

        # Check if anything is staged
        if _git("diff", "--cached", "--quiet").returncode == 0:
            log.info("output/ — nothing new to commit")
            return True

        commit = _git("commit", "-m", "chore: add product video to output/")
        if commit.returncode != 0:
            log.warning(f"git commit failed: {commit.stderr.strip()}")
            return False

        push = _git("push")
        if push.returncode != 0:
            log.warning(f"git push of output/ failed: {push.stderr.strip()}")
            return False

        log.info(f"output/ committed and pushed for {working_dir.name}")
        return True

    except Exception as e:
        log.exception(f"commit_and_push_output failed: {e}")
        return False
