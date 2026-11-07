"""
Format 9 assembler -- "Dilemme du Choix" (gamified two-choice split screen).

Sequential full-frame phases:
  0-4s    hook      : provocative "would you choose wrong?" framing line.
  4-10s   choice    : split screen, option A (left) vs option B (right).
  10-13s  teaser    : "The answer is in the description" over the still-visible split screen
                      (was a 3-2-1 countdown -- replaced per user feedback so the video pushes
                      description clicks BEFORE asking for the comment, not at the same time).
  13-16s  cta       : "Put your answer in the comments" -- the comment ask, on its own beat.

Background: the iconic "red pill / blue pill" choice photo (real stock/meme
imagery, not AI-generated -- this exact visual is a recurring TikTok trope
for dilemma-style content per user request), Ken Burns slow zoom for motion,
under the same charcoal-gradient scrim as format8's flat bg so captions stay
legible over unpredictable photo content and the channel still reads as one
visual system. Falls back to the old flat gradient if no photo is cached/
fetchable. Segoe UI type, soft shadow instead of hard stroke, muted
champagne/slate accents, ffmpeg cross-fades between stages. No voice, no
B-roll video -- same cheap production class as format8.
"""

import logging
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, OUTPUT_DIR, RESETWORTH_ASSETS_DIR
from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _slugify, _FONT_REGULAR, _FONT_LIGHT, _FONT_SEMIBOLD,
    _TEXT_COLOR, _soft_text, _FADE_DUR,
)

logger = logging.getLogger(__name__)

_HOOK_DUR      = 4.0     # was 3.0 -- the hook line didn't leave enough time to read before
                         # cutting to the split screen
_CHOICE_DUR    = 6.0
_TEASER_DUR    = 3.0     # was a 3x1s 3-2-1 countdown -- now "the answer is in the description"
_CTA_DUR       = 3.0

# Split-screen panel colors -- matches the pill photo's own blue/red so the choice stage
# reads as a continuation of the hook, not a different visual language. opt_a/left = the
# safer/certain choice = blue; opt_b/right = the riskier/higher-reward choice = red.
_PILL_BLUE      = (28, 92, 196)
_PILL_BLUE_DARK = (8, 22, 54)
_PILL_RED       = (196, 40, 40)
_PILL_RED_DARK  = (54, 8, 10)

# ── Red pill / blue pill background photo pool ──────────────────────────
_PILL_QUERIES = [
    "red pill blue pill hand choice",
    "matrix red pill blue pill decision",
]
_PILL_POOL_TARGET = 6
_PILL_MIN_SIZE = 500
_SCRIM_ALPHA = 175   # 0-255 -- charcoal gradient opacity over the photo, keeps text legible
                     # and the channel's dark look without hiding the photo entirely


def assemble_format9(script, visuals_result, voice_result, timestamps_result, music_path):
    """Pure motion-graphics assembler -- ignores visuals_result/voice_result/timestamps_result."""
    hook = script.get("hook", "").strip()
    body = [b.strip() for b in script.get("body", []) if b.strip()]
    cta  = (script.get("cta") or "Put your answer in the comments.").strip()

    opt_a = body[0] if len(body) > 0 else "Option A: the safe, small reward."
    opt_b = body[1] if len(body) > 1 else "Option B: the risky, big reward."

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f9"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    pill_photo = _fetch_pill_image()
    scrim_png = tmp_dir / "scrim.png"
    _build_scrim(scrim_png)
    if pill_photo is None:
        # No photo cached/fetchable -- fall back to the old flat gradient, still
        # scrimmed the same way so the CTA/text stages look identical either way.
        bg_png = tmp_dir / "bg.png"
        _build_bg(bg_png)
        pill_photo = bg_png

    overlay_specs: list[tuple[Path, float, float]] = []
    t = 0.0

    hook_png = tmp_dir / "hook.png"
    _render_hook(hook, hook_png)
    overlay_specs.append((hook_png, t, t + _HOOK_DUR))
    t += _HOOK_DUR

    choice_png = tmp_dir / "choice.png"
    _render_split(opt_a, opt_b, choice_png)
    overlay_specs.append((choice_png, t, t + _CHOICE_DUR))
    t += _CHOICE_DUR

    teaser_png = tmp_dir / "teaser.png"
    _render_teaser(teaser_png)
    overlay_specs.append((teaser_png, t, t + _TEASER_DUR))
    t += _TEASER_DUR

    cta_png = tmp_dir / "cta.png"
    _render_cta(cta, cta_png)
    total = t + _CTA_DUR
    overlay_specs.append((cta_png, t, total))

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format9_{slug}.mp4"

    cmd = _dilemma_cmd(pill_photo, scrim_png, overlay_specs, total, music_path, str(out))
    logger.info(f"Assembling format9 (dilemme du choix) | total={total:.1f}s "
                f"| music={'yes' if music_path else 'no'}")
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


def _build_bg(out_path: Path) -> None:
    """Same soft charcoal vertical gradient as format8. No divider baked in here --
    it only belongs to the split-screen stages, drawn per-stage in _render_split
    instead (a version baked into the shared bg leaked into the hook/CTA frames,
    which aren't a split screen)."""
    top, bottom = (58, 59, 63), (16, 16, 18)
    img  = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), bottom)
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        f = y / (VIDEO_HEIGHT - 1)
        row = tuple(int(top[c] + (bottom[c] - top[c]) * f) for c in range(3))
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=row)
    img.save(str(out_path), quality=92)


def _build_scrim(out_path: Path) -> None:
    """Same charcoal gradient as _build_bg but semi-transparent (RGBA) -- laid
    over the pill photo (or the flat fallback bg) so captions stay legible
    over unpredictable photo content, without hiding the photo entirely."""
    top, bottom = (58, 59, 63), (16, 16, 18)
    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        f = y / (VIDEO_HEIGHT - 1)
        row = tuple(int(top[c] + (bottom[c] - top[c]) * f) for c in range(3)) + (_SCRIM_ALPHA,)
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=row)
    img.save(str(out_path), format="PNG")


def _pill_pool_dir() -> Path:
    d = RESETWORTH_ASSETS_DIR / "dilemma_pills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch_pill_image() -> Path | None:
    """
    Real stock/meme photo of the "red pill / blue pill" choice -- the
    recurring TikTok dilemma-format visual the channel wants behind format9
    instead of a flat gradient. assets/dilemma_pills/pill_pinned.jpg -- the
    user's own hand-picked reference (the Matrix silhouette, blue pill left /
    red pill right) -- is used every time when present, no random rotation:
    the user approved this exact image specifically, so it isn't diluted by
    a random pick from the auto-searched pool. That pool (downloaded once,
    cached, same curated-pool idea as clip_broll's Pinterest pool) is only a
    fallback for when the pinned image is missing. Returns None (caller falls
    back to the flat gradient) if neither is available -- never blocks a render.
    """
    import random
    pool_dir = _pill_pool_dir()
    pinned = pool_dir / "pill_pinned.jpg"
    if pinned.exists():
        return pinned

    cached = [p for p in pool_dir.glob("*.jpg") if p.name != "pill_pinned.jpg"]
    if len(cached) < 3:
        cached.extend(_download_pill_candidates(pool_dir, needed=_PILL_POOL_TARGET - len(cached)))
    if not cached:
        return None
    return random.choice(cached)


# Watermarked/licensed stock-agency previews -- a query this on-the-nose ("red pill blue
# pill") returns almost exclusively these (Alamy/Shutterstock/iStock/Getty/etc previews carry
# a visible watermark or diagonal stamp), unlike the more generic B-roll footage clip_broll.py
# searches for. Filtered on BOTH the image CDN host and the source page host -- some agencies
# serve the actual pixels from a separate CDN domain (istockphoto -> media.istockphoto.com).
_STOCK_AGENCY_DOMAINS = (
    "alamy.com", "shutterstock.com", "istockphoto.com", "gettyimages.com",
    "dreamstime.com", "adobe.com", "123rf.com", "depositphotos.com",
    "freepik.com", "vectorstock.com", "canstockphoto.com", "bigstockphoto.com",
    "stocksy.com", "pond5.com", "envato.com", "elements.envato.com",
)


def _is_stock_agency_url(url: str) -> bool:
    return any(domain in url.lower() for domain in _STOCK_AGENCY_DOMAINS)


def _has_visible_watermark(image_path: Path) -> bool:
    """One quick vision check via the user's own Claude subscription -- domain blocklisting
    catches most watermarked stock previews, but reposted copies (wallpaper aggregators etc.)
    can still carry a watermark baked into the pixels from wherever THEY sourced it. Runs only
    once per candidate while topping up the small cached pool, not per render. Fails safe:
    treats an unreadable verification response as "has a watermark" (reject) since accepting a
    watermarked frame into a permanent, reused pool is worse than discarding one good candidate."""
    from config import CLAUDE_MODEL
    from modules.claude_cli import call_claude, ClaudeCliError
    prompt = (
        f"@{image_path}\nDoes this image have a visible watermark, logo, or stock-agency "
        "text/stamp overlaid on it (e.g. a company name, URL, or diagonal repeated pattern)? "
        "Answer with exactly one word: YES or NO."
    )
    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
    except ClaudeCliError as e:
        logger.debug(f"format9: watermark check failed ({e}); rejecting candidate to be safe")
        return True
    return not raw.strip().upper().startswith("NO")


def _is_relevant_image(image_path: Path, query: str) -> bool:
    """Vision check via the user's own Claude subscription -- the watermark/stock-agency
    filters say nothing about whether the image is actually ON TOPIC. A real, watermark-free,
    non-stock-agency search result can still be completely unrelated (a search for "Russia
    1998 financial crisis newspaper front page" once returned a Catherine: Full Body video
    game cover, reported 2026-07-27 against format long1) -- this catches that class of
    mismatch before it ends up burned into a video. Shared by long1 (documentary beat photos)
    and finance_roundup (format17's news item photos) -- both fetch a fresh, unpredictable
    query per item/beat with no permanent curated pool to fall back on. Fails closed (rejects)
    on an unreadable response, same reasoning as _has_visible_watermark above: for content
    this visible, discarding one good-but-unverified candidate costs less than accepting one
    wrong one."""
    from config import CLAUDE_MODEL
    from modules.claude_cli import call_claude, ClaudeCliError
    prompt = (
        f"@{image_path}\nDoes this image plausibly, genuinely relate to: \"{query}\"? "
        "Answer NO if it's unrelated content that just happened to match the search terms "
        "(e.g. anime/video game/cartoon art, an unrelated meme, a random stock photo with no "
        "real connection, adult content). Answer with exactly one word: YES or NO."
    )
    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
    except ClaudeCliError as e:
        logger.debug(f"dilemma: relevance check failed ({e}); rejecting candidate to be safe")
        return False
    return raw.strip().upper().startswith("YES")


def _download_pill_candidates(pool_dir: Path, needed: int) -> list[Path]:
    from io import BytesIO

    import requests
    from ddgs import DDGS
    from PIL import Image as PILImage

    downloaded: list[Path] = []
    existing_n = len(list(pool_dir.glob("*.jpg")))
    for query in _PILL_QUERIES:
        if len(downloaded) >= needed:
            break
        try:
            results = list(DDGS().images(query, max_results=20))
        except Exception as e:
            logger.warning(f"format9: pill image search failed for {query!r}: {e}")
            continue
        for r in results:
            if len(downloaded) >= needed:
                break
            url = r.get("image")
            source_url = r.get("url") or ""
            if url and (_is_stock_agency_url(url) or _is_stock_agency_url(source_url)):
                continue
            try:
                w, h = int(r.get("width") or 0), int(r.get("height") or 0)
            except (TypeError, ValueError):
                w = h = 0
            if not url or w < _PILL_MIN_SIZE or h < _PILL_MIN_SIZE:
                continue
            try:
                resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                PILImage.open(BytesIO(resp.content)).verify()
                img = PILImage.open(BytesIO(resp.content)).convert("RGB")
                out_path = pool_dir / f"pill_{existing_n + len(downloaded)}.jpg"
                img.save(out_path, format="JPEG", quality=90)
            except Exception as e:
                logger.debug(f"format9: skipping pill image candidate {url!r}: {e}")
                continue
            if _has_visible_watermark(out_path):
                logger.debug(f"format9: rejecting watermarked pill image candidate {url!r}")
                out_path.unlink(missing_ok=True)
                continue
            downloaded.append(out_path)
    if downloaded:
        logger.info(f"format9: cached {len(downloaded)} new pill image(s) in {pool_dir}")
    return downloaded


def _render_hook(text: str, out_path: Path) -> None:
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=78)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=78)
    wrapped = textwrap.fill(text, width=18, break_long_words=False)
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(overlay, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14)
    overlay.save(str(out_path), format="PNG")


def _split_option_text(raw: str) -> tuple[str, str]:
    """'Button A: $10,000 right now' -> ('Button A', '$10,000 right now')."""
    if ":" in raw:
        label, rest = raw.split(":", 1)
        return label.strip(), rest.strip()
    return raw.strip(), ""


def _build_split_bg() -> Image.Image:
    """Solid blue left / solid red right gradient, matching the red-pill/blue-pill hook
    photo's own colors so every split-screen stage reads as one continuous visual language.
    OPAQUE (unlike the other stage PNGs, which are transparent overlays on the bg photo) --
    fully replaces the photo underneath for its enable window. Shared by _render_split
    (the choice stage) and _render_teaser (the "answer is in the description" stage that
    follows it), so the colored split stays on screen continuously across both beats."""
    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 255))
    half = VIDEO_WIDTH // 2
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        f = y / (VIDEO_HEIGHT - 1)
        blue_row = tuple(int(_PILL_BLUE[c] + (_PILL_BLUE_DARK[c] - _PILL_BLUE[c]) * f) for c in range(3)) + (255,)
        red_row  = tuple(int(_PILL_RED[c]  + (_PILL_RED_DARK[c]  - _PILL_RED[c])  * f) for c in range(3)) + (255,)
        draw.line([(0, y), (half, y)], fill=blue_row)
        draw.line([(half, y), (VIDEO_WIDTH, y)], fill=red_row)
    draw.line([(half, 0), (half, VIDEO_HEIGHT)], fill=(245, 245, 248, 255), width=3)
    return img


def _render_split(opt_a: str, opt_b: str, out_path: Path) -> None:
    """Blue/red split background (see _build_split_bg) with each option's label + outcome
    text on its own half -- opt_a (blue, left) is the safer/certain choice, opt_b (red,
    right) is the riskier/higher-reward choice."""
    img = _build_split_bg()
    draw = ImageDraw.Draw(img)

    try:
        label_font = ImageFont.truetype(_FONT_SEMIBOLD, size=46)
        body_font  = ImageFont.truetype(_FONT_LIGHT, size=38)
    except OSError:
        label_font = body_font = ImageFont.load_default()

    label_a, text_a = _split_option_text(opt_a)
    label_b, text_b = _split_option_text(opt_b)

    def panel(cx: int, label: str, body: str):
        wrapped_label = textwrap.fill(label, width=14, break_long_words=False)
        bbox = draw.textbbox((0, 0), wrapped_label, font=label_font, align="center", spacing=10)
        lx = cx - (bbox[2] - bbox[0]) // 2
        ly = int(VIDEO_HEIGHT * 0.32)
        _soft_text(img, (lx, ly), wrapped_label, label_font, _TEXT_COLOR, align="center", spacing=10)

        wrapped_body = textwrap.fill(body, width=16, break_long_words=False)
        bbox2 = draw.textbbox((0, 0), wrapped_body, font=body_font, align="center", spacing=10)
        bx = cx - (bbox2[2] - bbox2[0]) // 2
        by = ly + (bbox[3] - bbox[1]) + 40
        _soft_text(img, (bx, by), wrapped_body, body_font, _TEXT_COLOR, align="center", spacing=10)

    panel(VIDEO_WIDTH // 4, label_a, text_a)
    panel(VIDEO_WIDTH * 3 // 4, label_b, text_b)

    img.save(str(out_path), format="PNG")


def _render_teaser(out_path: Path) -> None:
    """"The answer is in the description" over the same still-visible blue/red split --
    replaces the old 3-2-1 countdown. Kept as its own beat, separate from the final
    comment-ask CTA, so the description push and the comment push don't compete for
    attention in the same 3 seconds."""
    img = _build_split_bg()
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=68)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=68)
    wrapped = textwrap.fill("The answer is in the description", width=16, break_long_words=False)
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(img, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14,
               stroke_width=2, stroke_fill=(0, 0, 0, 235))
    img.save(str(out_path), format="PNG")


def _render_cta(text: str, out_path: Path) -> None:
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=72)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=72)
    wrapped = textwrap.fill(text, width=20, break_long_words=False)
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(overlay, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14)
    overlay.save(str(out_path), format="PNG")


def _dilemma_cmd(
    bg_photo: Path,
    scrim_png: Path,
    overlay_specs: list[tuple[Path, float, float]],
    total_dur: float,
    music_path: str | None,
    output_path: str,
) -> list[str]:
    """Looped bg photo (slow Ken Burns zoom for motion) + charcoal scrim +
    per-stage cross-fade pattern, same idea as format8's _pov_cmd."""
    n_frames = int(round(total_dur * VIDEO_FPS))
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(bg_photo), "-loop", "1", "-i", str(scrim_png)]
    for png, _, _ in overlay_specs:
        cmd += ["-loop", "1", "-i", str(png)]
    music_idx = 2 + len(overlay_specs)
    if music_path:
        cmd += ["-i", music_path]

    filters = [
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,"
        f"zoompan=z='min(zoom+0.0012,1.18)':d={n_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS},"
        f"format=yuv420p[bgz]",
        f"[1:v]format=rgba[scrim]",
        f"[bgz][scrim]overlay=0:0,format=yuv420p[base]",
    ]
    prev = "base"
    for j, (_, start, end) in enumerate(overlay_specs):
        png_idx = 2 + j
        lbl = "vout" if j == len(overlay_specs) - 1 else f"vo{j}"
        filters.append(
            f"[{png_idx}:v]format=rgba,fade=t=in:st={start:.3f}:d={_FADE_DUR}:alpha=1[fov{j}]"
        )
        filters.append(
            f"[{prev}][fov{j}]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'[{lbl}]"
        )
        prev = lbl

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]"]

    if music_path:
        fade_out = max(0, total_dur - 2)
        cmd += [
            "-map", f"{music_idx}:a",
            "-af", f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out:.2f}:d=2",
            "-shortest",
        ]

    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-t", str(total_dur),
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd
