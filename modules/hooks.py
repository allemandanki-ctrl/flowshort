"""
Hook bank -- short "transitional hook" clips prepended to format3 luxury videos.

Bank layout: assets/hooks/<id>.mp4 + assets/hooks/<id>.json (sidecar manifest):
  {
    "id": "hook_001",
    "filename": "hook_001.mp4",
    "duration": 3.2,          # seconds, full clip length
    "cut_time": 3.2,          # exact transition point (usually == duration;
                                lower if there are trailing frames to trim off)
    "ambiance": "dark_tense", # one of _AMBIANCE_VOCAB, matched to script tone
    "hook_type": "pov_reveal",
    "source": "placeholder"   # e.g. "transitionalhooks.com" once real clips are added
  }

Drop real hooks (downloaded from transitionalhooks.com / social media hook
banks) into assets/hooks/ with a matching .json sidecar -- no code change
needed, list_hooks() picks them up automatically.

Selection: Claude classifies the script's ambiance into a small fixed
vocabulary (same vocabulary the dark-cinematic music matcher uses), matched
against each hook's `ambiance` tag. Falls back to random on any failure --
a missing API key or a network hiccup must never block generation.

Dedup: state/hook_combo_log.json tracks (hook_id, music_stem, subject) combos
seen in the last HOOK_DEDUP_DAYS days. pick_hook_and_music() retries a
different hook a few times on collision before giving up and reusing one
(logged) -- unavoidable once the bank is small relative to weekly volume.
"""

import hashlib
import json
import logging
import random
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import (
    CLAUDE_MODEL, HOOKS_DIR, HOOK_DEDUP_DAYS,
    STATE_DIR, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH,
)

logger = logging.getLogger(__name__)

_COMBO_LOG    = STATE_DIR / "hook_combo_log.json"
_MAX_RETRIES  = 5

# Kept identical in spirit to the music matcher's dark-cinematic direction --
# hooks should read as intriguing/tense, never bright or comedic.
_AMBIANCE_VOCAB = ["dark_tense", "mysterious", "epic_driving", "somber", "aggressive", "calm_cinematic"]


# ── Bank ─────────────────────────────────────────────────────────────────

# Spec target is 2-4s "courts clips intrigants"; a bit of slack up to this
# lets solid slightly-longer hooks through without allowing 10s+/minute-long
# clips (some transitionalhooks.com entries are much longer, not real hooks).
_MAX_HOOK_DURATION = 6.0


def list_hooks(max_duration: float = _MAX_HOOK_DURATION) -> list[dict]:
    """All hooks with a valid manifest + existing video file, short enough to
    actually function as a pre-roll hook (excludes overlong outliers)."""
    if not HOOKS_DIR.exists():
        return []
    out = []
    skipped_long = 0
    for manifest_path in sorted(HOOKS_DIR.glob("*.json")):
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"hooks: bad manifest {manifest_path.name}: {e}")
            continue
        video_path = HOOKS_DIR / m.get("filename", "")
        if not video_path.exists():
            logger.warning(f"hooks: manifest {manifest_path.name} -> missing file {m.get('filename')}")
            continue
        dur = float(m.get("duration") or m.get("cut_time") or 0)
        if max_duration and dur > max_duration:
            skipped_long += 1
            continue
        m["_path"] = str(video_path)
        out.append(m)
    if skipped_long:
        logger.info(f"hooks: {skipped_long} hook(s) excluded (longer than {max_duration}s)")
    return out


# ── Ambiance classification + POV text (Claude, fail-safe) ─────────────────

def _classify_ambiance(script: dict) -> str | None:
    try:
        from modules.claude_cli import call_claude
        hook = script.get("hook", "")
        body = " ".join(script.get("body", [])[:2])
        prompt = (
            "Classify the ambiance of this short-form video into EXACTLY ONE of "
            f"these tags: {', '.join(_AMBIANCE_VOCAB)}.\n"
            f"Hook: {hook}\nBody: {body}\n"
            "Answer with only the tag, nothing else."
        )
        text, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        tag = text.strip().lower()
        return tag if tag in _AMBIANCE_VOCAB else None
    except Exception as e:
        logger.warning(f"hooks: ambiance classification failed ({e}); falling back to random hook")
        return None


def generate_pov_text(hook: dict, script: dict | None = None) -> str:
    """
    'POV: ...' caption for the HOOK CLIP itself -- NOT the financial video that
    follows. Its only job is to build curiosity about what's happening in THIS
    footage so the viewer waits through the cut. It must never reference the
    video's actual message/lesson (that's a separate, unrelated payload shown
    only after the transition). script['pov_text_override'] still wins if the
    dashboard set one explicitly.
    """
    override = (script or {}).get("pov_text_override")
    if override:
        text = override.strip()
        return text if text.upper().startswith("POV") else f"POV: {text}"

    title = hook.get("title") or hook.get("id", "").removeprefix("th_").replace("_", " ")
    fallback = "POV: she had no idea what was coming"
    try:
        from modules.claude_cli import call_claude
        # Curiosity-gap framework: open a loop, tease a payoff, reveal nothing.
        # Naming the specific action ("you just snapped someone's ankles",
        # "you just broke the laws of physics") was tried and rejected --
        # it answers the question instead of asking it, so there's nothing
        # left to wait for. Real viral POV captions stay generic on purpose.
        prompt = (
            "Write a short 'POV:' caption (5-9 words after 'POV:') for a viral "
            "short-form video HOOK clip -- the first 2-4 seconds before a cut "
            "into unrelated content. The ONLY job is to open a curiosity gap: "
            "signal something surprising/unexpected is about to happen WITHOUT "
            "describing what it is. Never name the specific action, object, or "
            "outcome in the footage -- that answers the question instead of "
            "asking it, and kills the reason to keep watching.\n\n"
            "Good (vague, teases a payoff, generic on purpose):\n"
            "  POV: she had no idea what was coming\n"
            "  POV: he didn't see this coming\n"
            "  POV: nobody expected this to happen\n"
            "  POV: this wasn't supposed to happen\n"
            "  POV: you won't believe what happens next\n\n"
            "Bad (names the specific action -- rejected, do not do this):\n"
            "  POV: you just snapped someone's ankles in half\n"
            "  POV: you just broke the laws of physics somehow\n\n"
            f"Hook clip title/theme (for your own context only, do NOT put "
            f"this or any synonym of it in the caption): {title!r}\n"
            "Output ONLY the caption text after 'POV:', no quotes, no the word POV."
        )
        text, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        text = text.strip().strip('"')
        return f"POV: {text}"[:70] if text else fallback
    except Exception as e:
        logger.warning(f"hooks: POV text generation failed ({e}); using fallback")
        return fallback


# ── Dedup (hook + music + subject combo, weekly window) ────────────────────

def _combo_hash(hook_id: str, music_path: str | None, subject: str) -> str:
    music_stem = Path(music_path).stem if music_path else "none"
    raw = f"{hook_id}|{music_stem}|{subject.strip().lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _load_combo_log() -> dict:
    if _COMBO_LOG.exists():
        try:
            return json.loads(_COMBO_LOG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_combo_log(data: dict) -> None:
    _COMBO_LOG.parent.mkdir(parents=True, exist_ok=True)
    _COMBO_LOG.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def is_combo_recent(hook_id: str, music_path: str | None, subject: str) -> bool:
    h = _load_combo_log().get(_combo_hash(hook_id, music_path, subject))
    if not h:
        return False
    try:
        last = datetime.fromisoformat(h)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).days < HOOK_DEDUP_DAYS
    except Exception:
        return False


def record_combo(hook_id: str, music_path: str | None, subject: str) -> None:
    data = _load_combo_log()
    data[_combo_hash(hook_id, music_path, subject)] = datetime.now(timezone.utc).isoformat()
    _save_combo_log(data)


# ── Payoff-moment analysis (real per-hook cut point, Claude vision) ────────

_MIN_HOOK_DURATION = 2.0   # never cut a hook shorter than this, whatever the vision call says --
                            # under ~2s the viewer doesn't have time to read what's happening
                            # before the transition cuts away (user feedback 2026-08-06)


def analyze_payoff_moment(hook_path: str, duration: float, n_samples: int = 6) -> float:
    """
    Vision-based cut point: samples n_samples evenly-spaced frames across the
    clip, asks Claude which frame is the LAST one before the hook's own
    payoff/climax becomes visible (a fall landing, an impact, a reveal --
    the moment the anticipation resolves), and returns that frame's
    timestamp as the real cut_time.

    User feedback that replaced an earlier "-1s from the end" heuristic:
    that was a rule nobody asked for -- the real spec is content-aware ("cut
    right as we understand the person is about to fall, not after we see it,
    but not before there's enough to understand it either"), which a fixed
    time offset can't express (a slow buildup and a fast cut need different
    trims). This costs one real Claude vision call per hook, cached into the
    hook's own manifest afterward so it only runs once per hook ever.

    Falls back to `duration` (no trim -- the old default) on any failure.
    Floors at _MIN_HOOK_DURATION regardless of what the vision call returns.
    """
    if duration <= _MIN_HOOK_DURATION:
        return duration
    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_payoff"
    tmp_dir.mkdir(exist_ok=True)

    times = [duration * (i + 1) / (n_samples + 1) for i in range(n_samples)]
    frames = []
    for i, t in enumerate(times):
        out = tmp_dir / f"payoff_{Path(hook_path).stem}_{i}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", hook_path,
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", str(out)],
            capture_output=True)
        if r.returncode == 0 and out.exists():
            frames.append((t, out))

    if len(frames) < 3:
        return duration

    try:
        from modules.claude_cli import call_claude
        prompt_lines = [f"Frame {i} (t={t:.2f}s): @{path}" for i, (t, path) in enumerate(frames)]
        prompt_lines.append(
            "This is a short-form video HOOK clip -- frames sampled across it in order. "
            "It's meant to be cut short and transition to unrelated content right before "
            "its own payoff (a fall, an impact, a trick landing, a reveal) actually happens, "
            "so the viewer never sees the resolution -- only the anticipation. "
            "Which frame is the LAST one that still shows anticipation/setup, "
            "right before the payoff becomes visible? If there's no clear payoff moment "
            "(the clip is just ambient/atmospheric footage), answer -1.\n"
            'Answer with JSON only: {"frame_index": N}'
        )
        text, _, _ = call_claude("", "\n".join(prompt_lines), model=CLAUDE_MODEL)
        import re
        m = re.search(r"\{.*\}", text, re.S)
        idx = json.loads(m.group(0))["frame_index"] if m else -1
        if not isinstance(idx, int) or idx < 0 or idx >= len(frames):
            return duration
        cut_time = frames[idx][0]
        return max(_MIN_HOOK_DURATION, min(duration, cut_time))
    except Exception as e:
        logger.warning(f"hooks: payoff analysis failed for {hook_path} ({e}); using full duration")
        return duration


def _ensure_payoff_analyzed(hook: dict) -> dict:
    """
    Lazily analyzes and CACHES a hook's real cut_time into its manifest file
    the first time it's picked -- subsequent picks reuse the cached value,
    no repeated vision-call cost. `payoff_analyzed: true` marks a hook whose
    cut_time is a real per-clip value (vs the old cut_time == duration
    placeholder every hook ingests with).
    """
    if hook.get("payoff_analyzed"):
        return hook
    duration = float(hook.get("duration") or 0.0)
    cut_time = analyze_payoff_moment(hook["_path"], duration)
    hook["cut_time"] = round(cut_time, 2)
    hook["payoff_analyzed"] = True
    manifest_path = HOOKS_DIR / f"{hook['id']}.json"
    try:
        saved = {k: v for k, v in hook.items() if k != "_path"}
        manifest_path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        logger.info(f"hooks: payoff cut_time for {hook['id']} -> {hook['cut_time']:.2f}s "
                    f"(was {duration:.2f}s)")
    except Exception as e:
        logger.warning(f"hooks: could not cache payoff cut_time for {hook['id']} ({e})")
    return hook


# ── Orchestration ────────────────────────────────────────────────────────

def pick_hook_and_music(script: dict) -> tuple[dict | None, str | None]:
    """
    Pick a hook (ambiance-matched to the script) + its music (pool or weekly
    per config.HOOK_MUSIC_MODE), retrying a different hook up to _MAX_RETRIES
    times if the (hook, music, subject) combo was used in the last
    HOOK_DEDUP_DAYS days. Returns (None, None) if the bank is empty.
    """
    hooks = list_hooks()
    if not hooks:
        logger.warning(f"hooks: bank empty ({HOOKS_DIR}) -- no hook applied")
        return None, None

    from modules.music.music_rotation import get_hook_music
    subject = script.get("subject", "")
    music_path = get_hook_music(script)

    tag = _classify_ambiance(script)
    ranked = [h for h in hooks if tag and tag in (h.get("ambiance") or "").lower()] or hooks
    random.shuffle(ranked)

    for hook in ranked[:_MAX_RETRIES]:
        if not is_combo_recent(hook["id"], music_path, subject):
            record_combo(hook["id"], music_path, subject)
            return _ensure_payoff_analyzed(hook), music_path

    hook = ranked[0]
    logger.warning(f"hooks: no fresh combo found after {_MAX_RETRIES} tries "
                   f"(bank may be small relative to volume) -- reusing {hook['id']}")
    record_combo(hook["id"], music_path, subject)
    return _ensure_payoff_analyzed(hook), music_path


def pick_unrelated_hook(script: dict, exclude_id: str) -> dict | None:
    """
    format3 hook-RECUT structure only (see assemble_with_hook_recut): the
    segment-3 fallback when the first hook doesn't have enough leftover
    footage past its own cut point to continue from. Deliberately the
    OPPOSITE of pick_hook_and_music's ambiance-matching -- picks a second
    clip that does NOT share the first hook's ambiance tag (per user
    feedback: it should look and feel like an unrelated clip, that
    mismatch IS the joke, not a smooth continuation). Falls back to any
    other hook if no differently-tagged one exists. Returns None if the
    bank has nothing left to pick after excluding the first hook.
    """
    hooks = [h for h in list_hooks() if h["id"] != exclude_id]
    if not hooks:
        return None
    tag = _classify_ambiance(script)
    unrelated = [h for h in hooks if not tag or tag not in (h.get("ambiance") or "").lower()] or hooks
    random.shuffle(unrelated)
    return _ensure_payoff_analyzed(unrelated[0])


# ── Placeholder generation (for testing before real hooks are downloaded) ──

_PLACEHOLDER_SPECS = [
    {"id": "ph_hook_dark",   "ambiance": "dark_tense",     "color1": "0x1a1a1a", "color2": "0x3a1a1a", "duration": 2.6},
    {"id": "ph_hook_myst",   "ambiance": "mysterious",     "color1": "0x0a0a1a", "color2": "0x1a1a3a", "duration": 3.2},
    {"id": "ph_hook_epic",   "ambiance": "epic_driving",   "color1": "0x1a1a0a", "color2": "0x3a2a0a", "duration": 2.2},
]


def generate_placeholder_hooks() -> list[dict]:
    """
    Synthetic hook clips (gradient pulse, zoom, silent) for pipeline testing
    before real hooks are downloaded from transitionalhooks.com. Overwrites
    any existing placeholder with the same id.
    """
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    made = []
    for spec in _PLACEHOLDER_SPECS:
        video_path = HOOKS_DIR / f"{spec['id']}.mp4"
        manifest_path = HOOKS_DIR / f"{spec['id']}.json"
        dur = spec["duration"]

        # Two-stop gradient that slowly zooms -- reads as "something building",
        # good enough as a placeholder for real hook footage.
        filt = (
            f"color=c={spec['color1']}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d={dur}[c1];"
            f"color=c={spec['color2']}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d={dur}[c2];"
            f"[c1][c2]blend=all_expr='A*(1-T/{dur})+B*(T/{dur})',"
            f"zoompan=z='1+0.15*(on/{int(dur*VIDEO_FPS)})':d={int(dur*VIDEO_FPS)}:"
            f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
        )
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", filt,
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(dur),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(video_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            logger.error(f"hooks: placeholder generation failed for {spec['id']}: {r.stderr[-500:]}")
            continue

        manifest = {
            "id": spec["id"], "filename": video_path.name, "duration": dur,
            "cut_time": dur, "ambiance": spec["ambiance"],
            "hook_type": "placeholder_gradient", "source": "placeholder",
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        made.append(manifest)
        logger.info(f"hooks: placeholder {spec['id']} ready ({dur}s, {spec['ambiance']})")

    return made


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    made = generate_placeholder_hooks()
    print(f"\n{len(made)} placeholder hooks ready in {HOOKS_DIR}")
    for m in made:
        print(f"  {m['id']}  ({m['ambiance']}, {m['duration']}s)")
