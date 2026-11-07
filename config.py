import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# API keys
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
PEXELS_API_KEY     = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY    = os.getenv("PIXABAY_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
GUMROAD_LINK       = os.getenv("GUMROAD_LINK", "")
# MindFile's own book ("The Sabotage Point") -- separate link, never
# ResetWorth's. Empty until the Gumroad listing is live; content_meta.py
# only starts promoting it once this is filled in (see GUMROAD_LINK_MINDFILE
# usage in generate_meta_mindfile/generate_meta_mf_long).
GUMROAD_LINK_MINDFILE = os.getenv("GUMROAD_LINK_MINDFILE", "")
# Freesound (format1 B-roll ambient SFX) -- the "Client secret" from the
# apiv2/apply form IS the token, confirmed against the live search API.
FREESOUND_CLIENT_SECRET = os.getenv("FREESOUND_CLIENT_SECRET", "")

# Publishing
YOUTUBE_CLIENT_SECRETS = os.getenv("YOUTUBE_CLIENT_SECRETS", "state/youtube_client_secrets.json")
YOUTUBE_TOKEN_FILE     = os.getenv("YOUTUBE_TOKEN_FILE", "state/youtube_token.json")
TIKTOK_PROFILE_DIR          = os.getenv("TIKTOK_PROFILE_DIR", "state/browser_profiles/tiktok")
INSTAGRAM_PROFILE_DIR       = os.getenv("INSTAGRAM_PROFILE_DIR", "state/browser_profiles/instagram")
YOUTUBE_STUDIO_PROFILE_DIR  = os.getenv("YOUTUBE_STUDIO_PROFILE_DIR", "state/browser_profiles/youtube_studio")
PUBLISH_PLATFORMS      = [p.strip() for p in os.getenv("PUBLISH_PLATFORMS", "tiktok,instagram,youtube").split(",")]
PUBLISH_HOUR           = int(os.getenv("PUBLISH_HOUR", "19"))   # 7pm daily

# Instagram mobile (Appium + BlueStacks) — set to "true" to use mobile uploader with music support
INSTAGRAM_USE_MOBILE   = os.getenv("INSTAGRAM_USE_MOBILE", "false").lower() == "true"
APPIUM_SERVER_URL      = os.getenv("APPIUM_SERVER_URL", "http://127.0.0.1:4723")
BLUESTACKS_ADB_HOST    = os.getenv("BLUESTACKS_ADB_HOST", "127.0.0.1")
BLUESTACKS_ADB_PORT    = os.getenv("BLUESTACKS_ADB_PORT", "5555")
ADB_PATH               = os.getenv("ADB_PATH", "adb")

# Direct-to-SaaS publishing (Ayrshare + Zernio) — see publishers/ and profiles.yaml.
# Keys live in .env, only their env-var NAMES live in profiles.yaml.
PUBLISH_PROFILES_FILE = os.getenv("PUBLISH_PROFILES_FILE", "profiles.yaml")

# "fr" or "en" — drives TTS voice selection and Whisper language hint
CONTENT_LANGUAGE = os.getenv("CONTENT_LANGUAGE", "fr")

# Pollinations.ai — free, no key needed
POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt"

# Paths
# ResetWorth is the default brand (see modules/users.py) -- it lives in its own
# users/resetworth/ folder just like every other brand. ASSETS_DIR/LOGS_DIR stay
# at the project root: they're shared infrastructure (fonts, music, sfx,
# transitions, voices, hooks, ops logs) used by every brand's renderer.
RESETWORTH_DIR = BASE_DIR / "users" / "resetworth"
ASSETS_DIR = BASE_DIR / "assets"
RESETWORTH_ASSETS_DIR = RESETWORTH_DIR / "assets"
OUTPUT_DIR = RESETWORTH_DIR / "output"
STATE_DIR = RESETWORTH_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"

# Claude model
CLAUDE_MODEL = "claude-sonnet-4-6"

# Whisper model for clip transcription: base (~74MB, fast) | small (~244MB, recommended)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

# Format1 clip edit: min words to consider a clip as "speech" (otherwise silent mode)
MIN_SPEECH_WORDS = int(os.getenv("MIN_SPEECH_WORDS", "8"))
# Show quote band on silent clips (no speech detected). Set SHOW_QUOTE_ON_SILENT_CLIPS=false to disable.
SHOW_QUOTE_ON_SILENT_CLIPS = os.getenv("SHOW_QUOTE_ON_SILENT_CLIPS", "true").lower() == "true"

# Video specs
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30

# CTA book ratio: show book card every N videos. 1 = every eligible video
# (explicit user call, 2026-07-29: "pour resetworth c censé etre sur toutes
# les videos" -- previously 4). Kept as a config knob, not ripped out, in
# case a rotation is wanted again later.
BOOK_CTA_EVERY_N = 1

# Valid formats
FORMATS = {
    "format1":  "Speech Edit",
    "format3":  "Luxury",
    "format6":  "Ranking List — Dark B-roll",
    "format7":  "Clip Edit 2.0 (B-roll + rythme)",
    "format8":  "POV Realite vs Illusion (Motion Graphics)",
    "format9":  "Dilemme du Choix (Gamification)",
    "format10": "Fake Conversation (Chat Bubbles)",
    "format11": "Dialogue Mentor/Eleve (2 voix)",
    "format12": "Audit Financier de Personnage",
    "format13": "Fake-out Drop (Text-Only, Comedic Cut)",
    "format14": "Interrogatoire (2 voix, personnage vs interrogateur)",
    "format15": "Duel Financier (2 personnages, jauge de vie)",
    "format16": "Telephone Rouge (Crossover, appel panique)",
    "format17": "Weekly Finance & Markets Roundup",
    "format18": "White Card Reaction (SHE/THEM/POV meme, clip on white bg)",
    "long1": "L'Autopsie Financiere (Long-Form, vraie voix)",
    "long2": "La Reunion Secrete (Long-Form, mastermind crossover)",
    "long3": "Le Simulateur de Ruine (Long-Form, courbe patrimoine)",
    "youtube_long": "YouTube Long-Form",
    # ── MindFile brand (Applied Psychology / Human Behavior, see users/mindfile/) ──
    "mf1": "MindFile — POV Reenactment (voice + broll)",
    "mf2": "MindFile — Decode (quote + video bg)",
    "mf3": "MindFile — Which One Are You (ranking)",
    # mf4 "Your Brain's Blind Spot" cut entirely 2026-07-28 (explicit user call,
    # "j'ai pas compris l'objectif du truc" -- the drastic-improvement attempt
    # didn't land). Do not re-add without asking first.
    "mf5": "MindFile — Two Minds (dialogue)",
    "mf6": "MindFile — The Exchange (text thread)",
    "mf_long": "MindFile — Deep Dive (Long-Form)",
}

# Default format set for ResetWorth (the default brand) and for any new brand
# until its own users/<name>/config.json declares an explicit "formats" list
# (see modules/users.py's UserContext.formats) -- every non-MindFile format.
# format14/youtube_long staying UI-hidden (see templates/index.html's own
# dropdown exclusions) is a separate, existing concern from brand OWNERSHIP
# of a format -- both still belong to ResetWorth here.
RESETWORTH_FORMATS = [fid for fid in FORMATS if not fid.startswith("mf")]

# YouTube long-form compilation (modules/long_format/long_format_generator.py) — landscape,
# separate from the VIDEO_WIDTH/HEIGHT portrait canvas used by every other format.
LONG_FORMAT_WIDTH  = 1920
LONG_FORMAT_HEIGHT = 1080
LONG_FORMAT_MIN_SEC = 11 * 60   # 660 — hard floor, keep adding clips until crossed
LONG_FORMAT_MAX_SEC = 15 * 60   # 900 — hard ceiling, stop adding clips before crossing
LONG_FORMAT_REUSE_CAP_RATIO = 0.40   # clips already used in a past long-format video
LONG_FORMAT_MUSIC_TRACK_CAP_RATIO = 0.15   # any single track, share of total music bed
# transition -> xfade duration in seconds ("" / 0.0 = hard cut, plain concat)
LONG_FORMAT_TRANSITIONS = {
    "hard_cut": 0.0,
    "dissolve": 0.4,
    "whip_pan": 0.25,
    "wind":     0.3,
}

# Transitional hooks (format3 pre-roll: hook clip -> synced transition -> content)
HOOKS_DIR = ASSETS_DIR / "hooks"
# "pool"   = existing semantic matcher picks a track per video (unchanged default)
# "weekly" = one track chosen once per ISO week, reused across all hook videos that week
HOOK_MUSIC_MODE = os.getenv("HOOK_MUSIC_MODE", "pool")
HOOK_DEDUP_DAYS = int(os.getenv("HOOK_DEDUP_DAYS", "7"))   # "same week" window for hook+music+content dedup

# Manual publish queue (modules/publish/manual_queue.py) — a fixed per-week quota of
# batch slots is PULLED from the normal auto-schedule (never added on top),
# rendered hook-free so a trending native sound can be added by hand before
# posting -- TikTok/YouTube's publish APIs can't attach a platform trending
# sound themselves.
MANUAL_ENABLED          = os.getenv("MANUAL_ENABLED", "true").lower() == "true"
MANUAL_PER_WEEK         = int(os.getenv("MANUAL_PER_WEEK", "3"))
MANUAL_ELIGIBLE_FORMATS = {"format3"}
MANUAL_FORCE_NO_HOOK    = True
TREND_MARKETS           = ["US", "UK", "GLOBAL"]   # never FR
