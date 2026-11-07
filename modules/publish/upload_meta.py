"""
Title/description for a directly-uploaded video with no known script/subject.

Two frames only (start + end of the video) sent to Claude vision -- not
several like the burned-caption check. Some videos are prepended with a
"hook" shot (a POV/reaction clip unrelated to the actual content); if the
start and end frames look like clearly different, unrelated scenes, the
prompt tells Claude to treat the END frame as the true subject and ignore
the start frame's content for the title/description.

Routed through modules.claude_cli (subscription CLI), NOT the direct
anthropic.Anthropic(api_key=...) client this used to call -- that violated
the project's standing "Claude subscription, not API" rule (see CLAUDE.md)
and, concretely, started hard-failing every upload's title/description with
"Your credit balance is too low" once the separately-billed ANTHROPIC_API_KEY
ran out of credits (2026-08-10), silently degrading every manually-uploaded
video to the generic _fallback() description below -- exactly the "phrase
vide" the user was seeing on real Snapchat posts. Images go in as `@path`
references in the prompt text (same pattern modules/assembler/
white_card_reaction.py's vision calls already use), not as base64 message
content -- the CLI has no such content-block API.
"""

import json
import logging
import re
import subprocess
from pathlib import Path

from config import BASE_DIR, CLAUDE_MODEL
from modules.transitions import probe_duration

logger = logging.getLogger(__name__)

# INSIDE the project tree, not the OS temp folder -- these get fed to a
# Claude vision call (@path), and the non-interactive `claude -p` CLI has
# been observed to inconsistently refuse reads outside BASE_DIR (see
# modules/clips/watermark_scan.py's _SCRATCH_DIR / white_card_reaction.py's
# _extract_preview_frames for the same fix and the concrete failure that
# prompted it).
_TMP_DIR = BASE_DIR / "scratch" / "upload_meta_preview"

_SYSTEM_PROMPT = (
    "You are a social media copywriter. Write in English, punchy and "
    "attention-grabbing. No em dashes."
)


def _build_prompt(frames: list[tuple[Path, str]]) -> str:
    """`@path` image references (the claude CLI's own vision syntax) prefixed
    with a label line each, same idea as the old base64 message-content list
    but expressed as plain prompt text."""
    frame_lines = "\n".join(f"This is the {label}:\n@{f}" for f, label in frames)
    return (
        f"{frame_lines}\n\n"
        "Write a title and description for this short-form video based on these "
        "frames.\n\n"
        "IMPORTANT: some videos open with an unrelated 'hook' shot (e.g. a "
        "reaction/POV clip) that has NOTHING to do with the video's actual "
        "subject. If the first and last frame look like clearly different, "
        "unrelated scenes, treat the LAST frame as the true subject of the video "
        "and ignore the first frame's content for the title/description.\n\n"
        'Return ONLY valid JSON: {"title": "<max 60 characters>", "description": "<1-2 sentences>"}'
    )


def _grab_frame(video_path: Path, at_sec: float, tag: str) -> Path | None:
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    out = _TMP_DIR / f"{video_path.stem}_{tag}.jpg"
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{at_sec:.2f}", "-i", str(video_path),
         "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", str(out)],
        capture_output=True,
    )
    return out if r.returncode == 0 and out.exists() else None


def generate_meta_from_video(video_path: Path) -> dict:
    """
    Returns {title, description, music_query, music_suggestion} -- same shape
    as content_meta.generate_meta, so build_social_caption /
    build_youtube_description work unchanged on the result.
    """
    from modules.content_meta import get_music_query, get_music_suggestion

    dur     = probe_duration(str(video_path))
    start_t = min(1.0, max(0.0, dur * 0.05))
    end_t   = max(0.0, dur - 1.0)

    start_frame = _grab_frame(video_path, start_t, "start")
    end_frame   = _grab_frame(video_path, end_t, "end")
    frames      = [(f, label) for f, label in
                  ((start_frame, "first frame (start of video)"),
                   (end_frame, "last frame (end of video)")) if f]

    if not frames:
        logger.warning("upload_meta: no usable frames extracted — generic fallback")
        return _fallback(get_music_query, get_music_suggestion)

    try:
        from modules.claude_cli import call_claude
        raw, _, _ = call_claude(_SYSTEM_PROMPT, _build_prompt(frames), model=CLAUDE_MODEL, timeout=90)
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        title       = str(parsed.get("title", ""))[:60].strip()
        description = str(parsed.get("description", "")).strip()
        if title and description:
            logger.info(f"upload_meta: Claude generated title={title!r}")
            return {
                "title": title, "description": description,
                "music_query": get_music_query({}), "music_suggestion": get_music_suggestion(),
            }
    except Exception as e:
        logger.warning(f"upload_meta: Claude vision call failed ({e}), using fallback")
    finally:
        for f, _ in frames:
            f.unlink(missing_ok=True)

    return _fallback(get_music_query, get_music_suggestion)


def _fallback(get_music_query, get_music_suggestion) -> dict:
    return {
        "title": "Watch this",
        "description": "A video worth your time.",
        "music_query": get_music_query({}),
        "music_suggestion": get_music_suggestion(),
    }
