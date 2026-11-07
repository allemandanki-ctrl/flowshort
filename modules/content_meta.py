"""
Content metadata generator — title, description, hashtags for all platforms.

Calls Claude to generate title + description from script content.
Falls back to script fields if Claude is unavailable.
"""

import json
import logging
import random
import re
from collections import deque

import os

from config import CLAUDE_MODEL, GUMROAD_LINK_MINDFILE
from modules.claude_cli import call_claude
from modules import users

logger = logging.getLogger(__name__)

# Pool of 40 niche tags — 3 sampled randomly per video (never the same combo twice)
_NICHE_TAG_POOL: list[str] = [
    "#moneymindset", "#wealthmindset", "#richmindset", "#mindsetshift", "#successmindset",
    "#buildwealth", "#wealthbuilding", "#investing", "#investingtips", "#stockmarket",
    "#compoundinterest", "#dividends", "#investor", "#personalfinance", "#financialliteracy",
    "#financialeducation", "#moneytips", "#budgeting", "#savemoney", "#financialgoals",
    "#passiveincome", "#sidehustle", "#multipleincome", "#incomestreams", "#cashflow",
    "#business", "#entrepreneur", "#success", "#businessmindset", "#entrepreneurship",
    "#growthmindset", "#hustle", "#richhabits", "#wealthhabits", "#discipline",
    "#moneymotivation", "#selfmade", "#financialtips", "#buildingwealth", "#moneycoach",
]

_NICHE_PER_VIDEO = 3

# Stable broad tags always appear at the END (lower spam risk there)
_STABLE_TAGS: list[str] = ["#money", "#wealth"]

# Track last used niche tags to avoid repeating too soon
_RECENT_NICHE: deque[str] = deque(maxlen=20)

# ── MindFile brand (Applied Psychology / Human Behavior) -- own tag pool, own
# rotation state, entirely separate from ResetWorth's wealth-niche pool above
# so the two brands never bleed into each other's hashtag history. ──────────
_NICHE_TAG_POOL_MINDFILE: list[str] = [
    "#psychologyfacts", "#humanpsychology", "#humanbehavior", "#mindsettips", "#psychologytips",
    "#socialpsychology", "#bodylanguage", "#cognitivebias", "#mindhacks", "#selfawareness",
    "#emotionalintelligence", "#relationshippsychology", "#psychologytricks", "#behavioralscience",
    "#mindgames", "#redflags", "#manipulationtactics", "#peoplepleaser", "#toxictraits",
    "#attachmentstyle", "#gaslighting", "#communicationskills", "#socialskills", "#psychologyofmind",
    "#mentalhealthawareness", "#understandingpeople", "#humannature", "#psychologystudent",
    "#thinkingfasterandslower", "#decisionmaking",
]
_NICHE_PER_VIDEO_MINDFILE = 3
_STABLE_TAGS_MINDFILE: list[str] = ["#psychology", "#mindfile"]
_RECENT_NICHE_MINDFILE: deque[str] = deque(maxlen=20)

_COMMENT_CTA_WORDS_MINDFILE: list[str] = ["MIND", "FILE", "TRUTH", "SIGNS", "PATTERN"]
_RECENT_CTA_WORDS_MINDFILE: deque[str] = deque(maxlen=3)


def _pick_hashtags_mindfile() -> list[str]:
    available = [t for t in _NICHE_TAG_POOL_MINDFILE if t not in _RECENT_NICHE_MINDFILE]
    if len(available) < _NICHE_PER_VIDEO_MINDFILE:
        _RECENT_NICHE_MINDFILE.clear()
        available = list(_NICHE_TAG_POOL_MINDFILE)
    niche = random.sample(available, _NICHE_PER_VIDEO_MINDFILE)
    _RECENT_NICHE_MINDFILE.extend(niche)
    return niche + _STABLE_TAGS_MINDFILE


def _pick_comment_cta_word_mindfile() -> str:
    available = [w for w in _COMMENT_CTA_WORDS_MINDFILE if w not in _RECENT_CTA_WORDS_MINDFILE]
    if not available:
        _RECENT_CTA_WORDS_MINDFILE.clear()
        available = list(_COMMENT_CTA_WORDS_MINDFILE)
    word = random.choice(available)
    _RECENT_CTA_WORDS_MINDFILE.append(word)
    return word

# Comment-to-DM trigger word, one per video (rotated so it's not the same word
# every time -- same anti-repeat approach as the hashtag pool above).
_COMMENT_CTA_WORDS: list[str] = [
    "GUIDE", "PLAN", "WEALTH", "GROWTH", "START", "MINDSET", "GOALS", "RICH",
]
_RECENT_CTA_WORDS: deque[str] = deque(maxlen=4)


def _pick_comment_cta_word() -> str:
    available = [w for w in _COMMENT_CTA_WORDS if w not in _RECENT_CTA_WORDS]
    if not available:
        _RECENT_CTA_WORDS.clear()
        available = list(_COMMENT_CTA_WORDS)
    word = random.choice(available)
    _RECENT_CTA_WORDS.append(word)
    return word


def _pick_hashtags() -> list[str]:
    """
    3 unique rotating niche tags (first — most visible on Insta/TikTok)
    + 2 stable broad tags (last) = 5 total, same set on every platform.
    Niche tags are sampled without replacement from the pool, avoiding recent picks.
    """
    available = [t for t in _NICHE_TAG_POOL if t not in _RECENT_NICHE]
    if len(available) < _NICHE_PER_VIDEO:
        _RECENT_NICHE.clear()
        available = list(_NICHE_TAG_POOL)
    niche = random.sample(available, _NICHE_PER_VIDEO)
    _RECENT_NICHE.extend(niche)
    return niche + _STABLE_TAGS


# Keep for backward compatibility
FIXED_HASHTAGS = _STABLE_TAGS

# Platform hashtag caps: 5 everywhere except YouTube, which allows up to 10.
# Every existing hand-written pool already tops out at 5, so this is a no-op
# for them -- it only matters for generate_meta_format18's Claude-proposed
# set, which can run up to 8 (ordered most-to-least relevant so the trim
# below always keeps the best ones).
_HASHTAGS_MAX_SOCIAL  = 5
_HASHTAGS_MAX_YOUTUBE = 10

_EPIC_TRACKS: list[dict] = [
    # ── Phonk ──────────────────────────────────────────────────────────────────
    {"name": "Sahara",                "artist": "Hensonn",             "type": "phonk",    "where": "Cherche 'Sahara Hensonn' dans les sons"},
    {"name": "Metamorphosis",         "artist": "Interworld",          "type": "phonk",    "where": "Cherche 'Metamorphosis Interworld' dans les sons"},
    {"name": "Close Eyes",            "artist": "DVRST",               "type": "phonk",    "where": "Cherche 'Close Eyes DVRST' dans les sons"},
    {"name": "Shadow",                "artist": "DVRST",               "type": "phonk",    "where": "Cherche 'Shadow DVRST' dans les sons"},
    {"name": "Murder In My Mind",     "artist": "Kordhell",            "type": "phonk",    "where": "Cherche 'Murder In My Mind Kordhell' dans les sons"},
    {"name": "Memory Reboot",         "artist": "VØJ & Narvent",       "type": "phonk",    "where": "Cherche 'Memory Reboot VØJ Narvent' dans les sons"},
    {"name": "Discipline",            "artist": "Mafia House",         "type": "phonk",    "where": "Cherche 'Discipline Mafia House' dans les sons"},
    {"name": "Nightwhisper",          "artist": "son phonk TikTok",    "type": "phonk",    "where": "Cherche 'Nightwhisper phonk' dans les sons TikTok"},
    {"name": "Dies Irae",             "artist": "son phonk TikTok",    "type": "phonk",    "where": "Cherche 'Dies Irae phonk' dans les sons TikTok"},
    {"name": "Brodyaga Funk",         "artist": "son phonk TikTok",    "type": "phonk",    "where": "Cherche 'Brodyaga Funk' dans les sons TikTok"},
    {"name": "Phonky Town",           "artist": "Playaphonk",          "type": "phonk",    "where": "Cherche 'Phonky Town Playaphonk' dans les sons"},
    {"name": "Kugelsicher",           "artist": "son phonk TikTok",    "type": "phonk",    "where": "Cherche 'Kugelsicher phonk' dans les sons TikTok"},
    {"name": "Gigachad Theme",        "artist": "son phonk TikTok",    "type": "phonk",    "where": "Cherche 'Gigachad Theme' dans les sons TikTok"},
    {"name": "Devil Eyes (slowed)",   "artist": "Hippie Sabotage",     "type": "phonk",    "where": "Cherche 'Devil Eyes slowed Hippie Sabotage' dans les sons"},
    # ── Brazilian Funk / Montagem Slowed ───────────────────────────────────────
    {"name": "Saravia Funk (ultra slowed)",            "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Saravia Funk slowed' dans les sons TikTok/Reels",          "beat_match": True},
    {"name": "Funk Sigilo (super slowed)",             "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Funk Sigilo slowed' dans les sons TikTok"},
    {"name": "Montagem Pegadora (slowed)",             "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Montagem Pegadora slowed' dans les sons TikTok"},
    {"name": "Funk Secreto (ultra slowed)",            "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Funk Secreto ultra slowed' dans les sons TikTok"},
    {"name": "El Control (slowed)",                    "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'El Control slowed' dans les sons TikTok"},
    {"name": "Automotivo Infernal 1.0 Purple (slowed)","artist": "son automotivo brésilien", "type": "funk", "where": "Cherche 'Automotivo Infernal Purple slowed' dans les sons TikTok", "beat_match": True},
    {"name": "Montagem Coral",                         "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Montagem Coral' dans les sons TikTok"},
    {"name": "Montagem Tomada",                        "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Montagem Tomada' dans les sons TikTok"},
    {"name": "Montagem Xonada",                        "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Montagem Xonada' dans les sons TikTok"},
    {"name": "Automotivo Bibi Fogosa",                 "artist": "son automotivo brésilien", "type": "funk", "where": "Cherche 'Automotivo Bibi Fogosa' dans les sons TikTok"},
    {"name": "Beat Dengoso",                           "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Beat Dengoso' dans les sons TikTok/Reels"},
    {"name": "Montagem Russamente",                    "artist": "son funk brésilien",       "type": "funk", "where": "Cherche 'Montagem Russamente' dans les sons TikTok"},
    # ── Cinématique / Émotionnel ───────────────────────────────────────────────
    {"name": "Cornfield Chase",               "artist": "Hans Zimmer",          "type": "cinematic", "where": "Cherche 'Cornfield Chase Hans Zimmer Interstellar' dans les sons"},
    {"name": "Luminary",                      "artist": "son cinématique",       "type": "cinematic", "where": "Cherche 'Luminary cinematic' dans les sons TikTok"},
    {"name": "Just Raw (Want to Love)",       "artist": "son TikTok",            "type": "cinematic", "where": "Cherche 'Just Raw Want to Love' dans les sons TikTok"},
    {"name": "Just Raw (Dies Irae)",          "artist": "son TikTok",            "type": "cinematic", "where": "Cherche 'Just Raw Dies Irae' dans les sons TikTok"},
    {"name": "Feeling Blue",                  "artist": "son cinématique",       "type": "cinematic", "where": "Cherche 'Feeling Blue cinematic' dans les sons TikTok"},
    {"name": "Hymn for the Weekend",          "artist": "Coldplay",              "type": "cinematic", "where": "Cherche 'Hymn for the Weekend Coldplay' dans les sons"},
    {"name": "A Good Man With a Broken Heart","artist": "son TikTok",            "type": "cinematic", "where": "Cherche 'A Good Man With a Broken Heart' dans les sons TikTok"},
    {"name": "That Drip From Your Eyes",      "artist": "son TikTok",            "type": "cinematic", "where": "Cherche 'That Drip From Your Eyes' dans les sons TikTok"},
    {"name": "PNDAX97",                       "artist": "son TikTok",            "type": "cinematic", "where": "Cherche 'PNDAX97' dans les sons TikTok"},
    {"name": "Snowfall",                      "artist": "Øneheart x reidenshi",  "type": "cinematic", "where": "Cherche 'Snowfall Øneheart reidenshi' dans les sons"},
    {"name": "Time",                          "artist": "Hans Zimmer",           "type": "cinematic", "where": "Cherche 'Time Hans Zimmer' dans la bibliothèque musicale"},
    {"name": "Experience",                    "artist": "Ludovico Einaudi",      "type": "cinematic", "where": "Cherche 'Experience Ludovico Einaudi' dans les sons"},
    {"name": "Idea 10",                       "artist": "Gibran Alcocer",        "type": "cinematic", "where": "Cherche 'Idea 10 Gibran Alcocer' dans les sons"},
    {"name": "Idea 22",                       "artist": "Gibran Alcocer",        "type": "cinematic", "where": "Cherche 'Idea 22 Gibran Alcocer' dans les sons"},
    {"name": "Resonance",                     "artist": "Home",                  "type": "cinematic", "where": "Cherche 'Resonance Home' dans les sons"},
]

_RECENT_PICKS: deque[str] = deque(maxlen=12)

def get_music_suggestion() -> dict:
    """
    25% chance trending (auto-refreshed monthly via music_updater), 75% fixed list.
    Fixed list uses a 12-track rotation window to avoid repeats.
    Choice is purely random — no link to video title or content.
    """
    if random.random() < 0.25:
        try:
            from modules.music.music_updater import get_trending_sounds
            trending = get_trending_sounds()
            if trending:
                return random.choice(trending)
        except Exception as e:
            logger.warning(f"get_music_suggestion: trending fetch failed ({e}), using fixed fallback")

    pool   = [t for t in _EPIC_TRACKS if t["name"] not in _RECENT_PICKS] or _EPIC_TRACKS
    choice = random.choice(pool)
    _RECENT_PICKS.append(choice["name"])
    return choice


_MOOD_A: list[str] = [
    "epic motivational powerful",
    "energetic hype trap",
    "powerful cinematic beat",
    "intense workout motivation",
    "hard hitting motivation bass",
]

_MOOD_B: list[str] = [
    "inspirational cinematic",
    "uplifting emotional piano",
    "motivational ambient build",
    "inspiring orchestral",
    "emotional rise cinematic wealth",
]


def get_music_query(script: dict) -> str:  # noqa: ARG001
    """Pick a random query from mood A (energetic) or mood B (inspirational), 50/50."""
    pool = random.choice([_MOOD_A, _MOOD_B])
    return random.choice(pool)


_SYSTEM_PROMPT = (
    "You are a social media copywriter specializing in wealth, money, and financial freedom content. "
    "Write in English. Be direct, punchy, aspirational. No em dashes."
)

_OPENING_STYLES = [
    "Start with a bold claim specific to this video's core idea.",
    "Start with a short rhetorical question that reframes how the viewer thinks about this topic.",
    "Start with a concrete number, stat, or time frame drawn from the video content.",
    "Start with a sharp contrast: what most people do vs. what actually works.",
    "Start with a one-sentence story hook grounded in the specific scenario this video covers.",
    "Start with a direct address to the viewer ('You've been told...', 'You already know...').",
    "Start with the single most counterintuitive insight from this video.",
    "Start with a confession or admission that reframes the topic unexpectedly.",
]


def generate_meta(script: dict) -> dict:
    """
    Return {title, description} for this script.

    If script["_meta"] is already set (pre-computed by publisher), returns it directly
    without calling Claude — guarantees a single API call per video across all platforms.
    youtube_long has no hook/body (it's a compilation, not a scripted short) — dispatch
    to its own prompt.
    """
    if script.get("_meta"):
        return script["_meta"]

    if script.get("format") == "youtube_long":
        return generate_meta_long(script)

    if script.get("format") == "format9":
        return generate_meta_format9(script)

    if script.get("format") == "format17":
        return generate_meta_format17(script)

    if script.get("format") == "format18":
        return generate_meta_format18(script)

    if script.get("format") == "long1":
        return generate_meta_long1(script)

    if script.get("format") in ("long2", "long3"):
        return generate_meta_long_scripted(script)

    if script.get("format") in ("mf1", "mf2", "mf3", "mf5", "mf6"):
        return generate_meta_mindfile(script)

    if script.get("format") == "mf_long":
        return generate_meta_mf_long(script)

    # Generic brand-factory niche brand (2026-08-12, see modules/brand_factory.py)
    # -- any brand beyond ResetWorth/MindFile whose config.json carries a
    # "niche" block gets its own voice/hashtags/book promo here instead of
    # falling into ResetWorth's default prompt+pool below. Only reached for
    # the "shared" format ids (format1, format3, format10, format11...) --
    # format9/17/long1/long2/long3 above still use ResetWorth's own caption
    # voice for a niche brand (known limitation, not yet split per brand).
    # No-op for ResetWorth/MindFile: their config.json never has "niche".
    niche = _niche_config_for_script(script)
    if niche:
        return generate_meta_niche_brand(script, niche)

    hook    = script.get("hook", "")
    subject = script.get("subject", "")
    body    = "\n".join(f"- {line}" for line in script.get("body", []))
    opening_style = random.choice(_OPENING_STYLES)

    prompt = f"""You are writing a caption for a short-form video about: "{subject}"

Video script:
Hook: {hook}
Body:
{body}

Generate a title and description for this SPECIFIC video.

Opening style to use for the description: {opening_style}

Return ONLY valid JSON:
{{
  "title": "<max 60 characters, punchy, scroll-stopping>",
  "description": "<2-3 sentences derived from THIS video's specific content and angle>"
}}

Rules:
- title: max 60 characters, no hashtags, no em dashes
- description: must be unique to this video — do NOT use generic openers like "Most people don't realize", "The truth about", "Have you ever wondered", "In today's world", or any template phrase that could apply to any money video
- description: English only, ends with a strong statement or hook
- No em dashes anywhere"""

    try:
        raw, _, _ = call_claude(_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        title = str(parsed.get("title", ""))[:60].strip()
        description = str(parsed.get("description", "")).strip()
        if title and description:
            logger.info(f"content_meta: Claude generated title={title!r}")
            return _with_music_credit(
                {"title": title, "description": description, "music_query": get_music_query(script), "music_suggestion": get_music_suggestion()},
                script,
            )
    except Exception as e:
        logger.warning(f"content_meta: Claude call failed ({e}), using fallback")

    return _fallback_meta(script)


def generate_meta_format9(script: dict) -> dict:
    """
    format9 ("Dilemme du Choix") tells viewers on-screen "the answer is in the
    description" -- this is what fills that promise: a genuinely reasoned,
    specific verdict on the given dilemma (not "it depends" filler), stored in
    meta["_reveal"] and surfaced by build_youtube_description/build_social_caption.
    Also pins the comment-to-DM word to "GUIDE" (matches what's actually being
    offered here) instead of the rotating pool every other format uses.
    """
    if script.get("_meta"):
        return script["_meta"]

    hook    = script.get("hook", "")
    body    = script.get("body", [])
    opt_a   = body[0] if len(body) > 0 else ""
    opt_b   = body[1] if len(body) > 1 else ""
    subject = script.get("subject", "")
    opening_style = random.choice(_OPENING_STYLES)

    prompt = f"""You are writing a caption for a short-form "dilemma" video about: "{subject}"

Video script:
Hook: {hook}
Option A (blue panel, left, the safer/certain choice): {opt_a}
Option B (red panel, right, the riskier/higher-upside choice): {opt_b}

The on-screen CTA tells viewers "the answer is in the description" -- so the description you
write MUST deliver on that: a genuine, reasoned verdict on THIS specific dilemma, not a vague
"it depends" cop-out.

Opening style to use for the description: {opening_style}

Return ONLY valid JSON:
{{
  "title": "<max 60 characters, punchy, scroll-stopping>",
  "description": "<1-2 sentences setting up the dilemma for THIS specific video -- do NOT reveal the answer here, that's what 'reveal' is for>",
  "reveal": "<2-4 sentences taking a real, specific, justified position between Option A and Option B for THIS dilemma. Ground it in an actual principle (opportunity cost, risk-adjusted return, loss aversion, base rates -- whatever genuinely applies here), not generic advice. End by acknowledging the other option is still defensible, so the debate stays open.>"
}}

Rules:
- title: max 60 characters, no hashtags, no em dashes
- description: must be unique to this video, no generic openers like "Most people don't realize", no em dashes
- reveal: must be substantive and specific to THIS dilemma, no em dashes
- English only"""

    try:
        raw, _, _ = call_claude(_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        title = str(parsed.get("title", ""))[:60].strip()
        description = str(parsed.get("description", "")).strip()
        reveal = str(parsed.get("reveal", "")).strip()
        if title and description and reveal:
            logger.info(f"content_meta: Claude generated format9 title={title!r}")
            meta = {
                "title": title, "description": description, "_reveal": reveal,
                "_comment_cta_word": "GUIDE",
                "music_query": get_music_query(script), "music_suggestion": get_music_suggestion(),
            }
            return _with_music_credit(meta, script)
    except Exception as e:
        logger.warning(f"content_meta: Claude call failed for format9 ({e}), using fallback")

    meta = _fallback_meta(script)
    meta["_comment_cta_word"] = "GUIDE"
    return meta


_SYSTEM_PROMPT_FORMAT18 = (
    "You are writing the social media caption/description for a ResetWorth 'reaction meme' "
    "short -- a repost-style video built around a THEM:/ME: or POV joke card over reaction "
    "footage (the kind you'd see reposted from Snapchat Spotlight or TikTok). This is a joke, "
    "not a motivational essay: match the dry, deadpan, banter tone of the joke itself. Never "
    "moralize and never default into generic 'financial freedom' / 'build your wealth' "
    "preaching -- only lean into money/mindset/hustle language when the joke itself is "
    "actually about that. Write in English. Short, punchy, meme-literate. No em dashes."
)


def _sanitize_hashtags(raw) -> list[str]:
    """Normalize Claude's freeform hashtag list into clean '#tag' strings --
    used by generate_meta_format18 since, unlike the fixed hand-written pools
    elsewhere in this file, these come straight from an LLM per video and
    need validating (leading '#', no spaces/punctuation, no dupes) before
    they can be trusted in a real caption."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        tag = re.sub(r"\s+", "", str(t).strip())
        if not tag:
            continue
        tag = "#" + re.sub(r"[^\w]", "", tag.lstrip("#"))
        low = tag.lower()
        if len(tag) <= 1 or low in seen:
            continue
        seen.add(low)
        out.append(tag)
        if len(out) >= 12:
            break
    return out


# 2026-08-23 (user request): pause hashtags/description on White Card posts to
# test whether they're hurting reach -- caption/YouTube-description collapse
# to just the short punchy title on every platform (Shorts, TikTok, Insta,
# Facebook) while this is True. Flip back to False to restore the full
# description+hashtags caption. See build_youtube_description/
# build_social_caption/youtube_title, which all check meta["_minimal_caption"].
# 2026-08-24 (user request): reach test ended -- reverted to False to restore
# full description+hashtags captions on White Card posts.
_FORMAT18_MINIMAL_CAPTION = False


def generate_meta_format18(script: dict) -> dict:
    """
    White Card ("format18") reaction-meme videos: their joke text (the
    THEM:/ME:/POV card actually on screen, saved verbatim as subject/hook by
    white_card_reaction._save_format18_script) is banter, not a scripted
    money essay -- generate_meta()'s default wealth/"financial freedom"
    system prompt read as stiff and off-tone against it, and its fixed
    wealth-niche hashtag pool (#investing, #dividends...) often had nothing
    to do with what the joke was actually about (2026-08-23 user report).

    This generator instead (1) writes in the same dry/banter voice as the
    joke card itself, riffing on the SPECIFIC joke rather than moralizing,
    and (2) has Claude propose hashtags straight from the joke's own topic
    in the same call, ordered most-to-least relevant so downstream platform
    capping (see _HASHTAGS_MAX_SOCIAL/_HASHTAGS_MAX_YOUTUBE below) keeps the
    best ones first. Falls back to the generic wealth pool only if that
    comes back empty/too short. Same book-CTA/promo mechanism as every other
    ResetWorth format -- unaffected."""
    if script.get("_meta"):
        return script["_meta"]

    joke = script.get("hook") or script.get("subject") or ""

    prompt = f"""The video is a "reaction meme" short: a joke card over reaction footage. The joke card reads:

{joke}

Write a title and description for THIS specific joke/clip, plus hashtags.

Return ONLY valid JSON:
{{
  "title": "<max 40 characters, under 10 words, punchy, scroll-stopping, easy to read at a glance>",
  "description": "<3-5 sentences that riff on THIS specific joke -- deadpan/banter tone, not a motivational essay. Develop the bit: extend the joke, add a callback or a second beat, paint the scene a little -- more than a one-liner, but every sentence still earns its place, no padding or filler>",
  "hashtags": ["<10 to 12 lowercase hashtags directly relevant to what THIS joke is actually about, each starting with #, no spaces, ordered from MOST to LEAST relevant/specific -- only the first 5 are shown on most platforms, up to 10 are used on YouTube>"]
}}

Rules:
- title: max 40 characters, under 10 words, no hashtags, no em dashes
- description: must be unique to this joke -- do NOT use generic openers like "Most people don't realize", "The truth about", or any template phrase
- description: English only, no em dashes, 3-5 sentences (longer than a one-liner, still punchy, no filler)
- hashtags: must match the joke's actual topic/subject -- do NOT default to generic money/finance tags unless the joke is genuinely about money"""

    try:
        raw, _, _ = call_claude(_SYSTEM_PROMPT_FORMAT18, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        # 2026-08-23: tightened from 60→40 chars / <10 words specifically for
        # White Card's now-title-only caption (see _FORMAT18_MINIMAL_CAPTION)
        # -- the title IS the whole caption now, so it needs to read at a glance.
        title = str(parsed.get("title", ""))[:40].strip()
        description = str(parsed.get("description", "")).strip()
        tags = _sanitize_hashtags(parsed.get("hashtags"))
        if title and description:
            logger.info(f"content_meta: Claude generated format18 title={title!r} tags={tags}")
            return _with_music_credit(
                {
                    "title": title, "description": description,
                    "_hashtags": tags if len(tags) >= 3 else _pick_hashtags(),
                    "_comment_cta_word": _pick_comment_cta_word(),
                    "_minimal_caption": _FORMAT18_MINIMAL_CAPTION,
                    "music_query": get_music_query(script), "music_suggestion": get_music_suggestion(),
                },
                script,
            )
    except Exception as e:
        logger.warning(f"content_meta: Claude call failed for format18 ({e}), using fallback")

    meta = _fallback_meta(script)
    meta["_hashtags"] = _pick_hashtags()
    meta["_comment_cta_word"] = _pick_comment_cta_word()
    meta["_minimal_caption"] = _FORMAT18_MINIMAL_CAPTION
    return meta


_SYSTEM_PROMPT_MINDFILE = (
    "You are a social media copywriter for MindFile, a channel about applied psychology and human "
    "behavior. Write in English. Be direct, specific, and genuinely insightful -- never salesy, "
    "MindFile has no product or book to sell. No em dashes."
)


_MINDFILE_BOOK_TITLE = "The Sabotage Point"


def _mindfile_book_promo() -> dict | None:
    """MindFile's own book promo block for meta["_book_promo"] -- own title/
    link/comment phrasing, NEVER ResetWorth's GUMROAD_LINK. Returns None
    until GUMROAD_LINK_MINDFILE is actually filled in (2026-07-29: book
    written, not yet listed on Gumroad) -- once it is, every MindFile video
    starts promoting it automatically via generate_meta_mindfile/
    generate_meta_mf_long below, no other code change needed."""
    if not GUMROAD_LINK_MINDFILE:
        return None
    return {
        "title": _MINDFILE_BOOK_TITLE,
        "link": GUMROAD_LINK_MINDFILE,
        "comment_suffix": "and I'll send you the book.",
    }


def generate_meta_mindfile(script: dict) -> dict:
    """
    Shared caption/title generator for MindFile's short formats. Same shape
    as the default generate_meta() above, but with MindFile's own system
    prompt/hashtag pool, and its own book promo (see _mindfile_book_promo)
    instead of ResetWorth's -- on every video once a real Gumroad link
    exists, per explicit user instruction (2026-07-29), never before.
    """
    if script.get("_meta"):
        return script["_meta"]

    hook    = script.get("hook", "")
    subject = script.get("subject", "")
    body    = "\n".join(f"- {line}" for line in script.get("body", []))
    opening_style = random.choice(_OPENING_STYLES)

    prompt = f"""You are writing a caption for a MindFile short-form video about: "{subject}"

Video script:
Hook: {hook}
Body:
{body}

Generate a title and description for this SPECIFIC video.

Opening style to use for the description: {opening_style}

Return ONLY valid JSON:
{{
  "title": "<max 60 characters, punchy, scroll-stopping>",
  "description": "<2-3 sentences derived from THIS video's specific content and angle>"
}}

Rules:
- title: max 60 characters, no hashtags, no em dashes
- description: must be unique to this video -- do NOT use generic openers like "Most people don't
  realize", "The truth about", "Have you ever wondered", or any template phrase that could apply to
  any psychology video
- description: English only, ends with a strong statement or question
- No em dashes anywhere, no product/book mention"""

    try:
        raw, _, _ = call_claude(_SYSTEM_PROMPT_MINDFILE, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        title = str(parsed.get("title", ""))[:60].strip()
        description = str(parsed.get("description", "")).strip()
        if title and description:
            logger.info(f"content_meta: Claude generated MindFile title={title!r}")
            promo = _mindfile_book_promo()
            meta = {
                "title": title, "description": description, "_no_promo": promo is None,
                "_hashtags": _pick_hashtags_mindfile(), "_comment_cta_word": _pick_comment_cta_word_mindfile(),
                "music_query": get_music_query(script), "music_suggestion": get_music_suggestion(),
            }
            if promo:
                meta["_book_promo"] = promo
            return _with_music_credit(meta, script)
    except Exception as e:
        logger.warning(f"content_meta: Claude call failed for MindFile ({e}), using fallback")

    promo = _mindfile_book_promo()
    meta = _fallback_meta(script)
    meta["_no_promo"] = promo is None
    meta["_hashtags"] = _pick_hashtags_mindfile()
    meta["_comment_cta_word"] = _pick_comment_cta_word_mindfile()
    if promo:
        meta["_book_promo"] = promo
    return meta


def _niche_config_for_script(script: dict) -> dict | None:
    """The producing brand's own "niche" block, if any -- resolves via the
    script's persisted _user tag (set by modules.users right after
    generation), falling back to whichever brand is currently active (covers
    the meta-precompute call in publish/template_parts.py that can run
    before tagging). None for ResetWorth/MindFile, whose config.json never
    has a "niche" key, so this is a no-op for both."""
    try:
        name = script.get("_user") or users.active_user()
        ctx = users.get_user(name)
    except Exception:
        return None
    return (ctx.config or {}).get("niche") or None


def _niche_system_prompt(niche: dict) -> str:
    label = niche.get("label", "this channel's niche")
    voice = niche.get("voice", "").strip() or "Punchy, specific, no fluff -- every word earns its place."
    return (
        f"You are writing captions for a short-form video channel about {label}. "
        "Write in English. Be direct, specific, and genuinely insightful -- never salesy. "
        f"{voice} No em dashes."
    )


def generate_meta_niche_brand(script: dict, niche: dict) -> dict:
    """Generic caption/title generator for any brand-factory niche brand (see
    modules/brand_factory.py) -- same shape as generate_meta_mindfile above,
    but voice/hashtag pool/book promo all come from the brand's own
    config.json "niche" block instead of a hand-written per-brand constant.
    Always sets meta['_hashtags']/['_comment_cta_word'] itself (even on the
    Claude-call-failed fallback path) so build_social_caption's own
    no_promo-based pool guess -- tuned only for ResetWorth vs. MindFile --
    never has to run for a third+ brand."""
    if script.get("_meta"):
        return script["_meta"]

    hashtag_pool = niche.get("hashtag_pool") or []
    stable_tags  = niche.get("stable_tags") or []
    cta_words    = niche.get("cta_words") or []
    book         = niche.get("book") or {}

    def _hashtags() -> list[str]:
        if not hashtag_pool:
            return _pick_hashtags()  # brand has no researched pool -- don't ship an empty caption
        n = min(3, len(hashtag_pool))
        return random.sample(hashtag_pool, n) + stable_tags

    def _cta_word() -> str | None:
        return random.choice(cta_words) if cta_words else None

    promo = None
    gumroad_env = book.get("gumroad_env")
    if gumroad_env and os.environ.get(gumroad_env):
        promo = {
            "title": book.get("title", ""),
            "link": os.environ[gumroad_env],
            "comment_suffix": book.get("comment_suffix") or "and I'll send you the book.",
        }

    hook    = script.get("hook", "")
    subject = script.get("subject", "")
    body    = "\n".join(f"- {line}" for line in script.get("body", []))
    opening_style = random.choice(_OPENING_STYLES)
    label = niche.get("label", "this channel's niche")

    prompt = f"""You are writing a caption for a video about "{label}", on the topic: "{subject}"

Video script:
Hook: {hook}
Body:
{body}

Generate a title and description for this SPECIFIC video.

Opening style to use for the description: {opening_style}

Return ONLY valid JSON:
{{
  "title": "<max 60 characters, punchy, scroll-stopping>",
  "description": "<2-3 sentences derived from THIS video's specific content and angle>"
}}

Rules:
- title: max 60 characters, no hashtags, no em dashes
- description: must be unique to this video -- no generic template openers
- description: English only, ends with a strong statement or question
- No em dashes anywhere"""

    try:
        raw, _, _ = call_claude(_niche_system_prompt(niche), prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        title = str(parsed.get("title", ""))[:60].strip()
        description = str(parsed.get("description", "")).strip()
        if title and description:
            logger.info(f"content_meta: Claude generated niche-brand title={title!r}")
            meta = {
                "title": title, "description": description, "_no_promo": promo is None,
                "_hashtags": _hashtags(), "_comment_cta_word": _cta_word(),
                "music_query": get_music_query(script), "music_suggestion": get_music_suggestion(),
            }
            if promo:
                meta["_book_promo"] = promo
            return _with_music_credit(meta, script)
    except Exception as e:
        logger.warning(f"content_meta: Claude call failed for niche brand ({e}), using fallback")

    meta = _fallback_meta(script)
    meta["_no_promo"] = promo is None
    meta["_hashtags"] = _hashtags()
    meta["_comment_cta_word"] = _cta_word()
    if promo:
        meta["_book_promo"] = promo
    return meta


def generate_meta_mf_long(script: dict) -> dict:
    """
    mf_long ("MindFile Deep Dive") already generated its own title/description/
    hashtags at script-gen time (see script_generator._generate_mf_long_script)
    -- reuse them directly, same reasoning as generate_meta_long1. Uses
    MindFile's own book promo (see _mindfile_book_promo), never ResetWorth's.
    """
    if script.get("_meta"):
        return script["_meta"]

    promo = _mindfile_book_promo()
    meta = {
        "title": (script.get("title") or script.get("subject") or "MindFile Deep Dive")[:100],
        "description": script.get("description") or script.get("case_subject", ""),
        "_no_promo": promo is None,
        "_hashtags": script.get("hashtags") or _pick_hashtags_mindfile(),
        "music_query": get_music_query(script), "music_suggestion": get_music_suggestion(),
    }
    if promo:
        meta["_book_promo"] = promo
    return _with_music_credit(meta, script)


def generate_meta_format17(script: dict) -> dict:
    """
    format17 ("Weekly Finance & Markets Roundup") already generated a FULLY
    formatted description at script-gen time (see
    script_generator._generate_format17_script) -- intro line, one line per
    item with detail, the "Comment GUIDE" CTA, and hashtags, all per the
    user's exact template. _self_contained_description tells
    build_youtube_description/build_social_caption to use it verbatim
    instead of layering their own book-CTA/hashtag/comment-CTA appends on
    top of an already-complete caption.
    """
    if script.get("_meta"):
        return script["_meta"]

    meta = {
        "title": (script.get("subject") or "Weekly Finance & Markets Roundup")[:60],
        "description": script.get("description", ""),
        "_self_contained_description": True,
        "music_query": get_music_query(script),
        "music_suggestion": get_music_suggestion(),
    }
    return _with_music_credit(meta, script)


def generate_meta_long1(script: dict) -> dict:
    """
    long1 ("L'Autopsie Financiere") already generated its own title/
    description/hashtags at script-gen time (see
    script_generator._generate_long1_script) -- reuse them directly instead
    of a second Claude call over mostly the same inputs, same idea as
    generate_meta_long reusing long_format_generator's own title/description.
    """
    if script.get("_meta"):
        return script["_meta"]

    meta = {
        "title": (script.get("title") or script.get("subject") or "Financial Autopsy")[:100],
        "description": script.get("description") or script.get("case_subject", ""),
        "_hashtags": script.get("hashtags") or _pick_hashtags(),
        "music_query": get_music_query(script),
        "music_suggestion": get_music_suggestion(),
    }
    return _with_music_credit(meta, script)


def generate_meta_long_scripted(script: dict) -> dict:
    """
    long2 ("La Reunion Secrete") and long3 ("Le Simulateur de Ruine") both
    already generate their own title/description/hashtags at script-gen time
    (see script_generator._generate_long2_script / _generate_long3_script) --
    reuse them directly, same reasoning as generate_meta_long1.
    """
    if script.get("_meta"):
        return script["_meta"]

    meta = {
        "title": (script.get("title") or script.get("subject") or "Watch this")[:100],
        "description": script.get("description", ""),
        "_hashtags": script.get("hashtags") or _pick_hashtags(),
        "music_query": get_music_query(script),
        "music_suggestion": get_music_suggestion(),
    }
    return _with_music_credit(meta, script)


def _with_music_credit(meta: dict, script: dict) -> dict:
    """Carry the embedded track's attribution (set by select_music/select_track,
    see modules/music/music_matcher.py and modules/music/music_library.py) through to the
    caption builder -- generate_meta's return value is the only thing that
    survives from generation to the (separate, later) publish step."""
    if script.get("_music_credit"):
        meta["_music_credit"] = script["_music_credit"]
    return meta


def _fallback_meta(script: dict) -> dict:
    hook = script.get("hook", script.get("subject", "")).strip()
    title = hook[:60] if hook else "Watch this"
    body_lines = script.get("body", [])
    description = body_lines[0] if body_lines else hook
    return _with_music_credit(
        {"title": title, "description": description, "music_query": get_music_query(script), "music_suggestion": get_music_suggestion()},
        script,
    )


def generate_meta_long(script: dict) -> dict:
    """
    Title + description for a youtube_long compilation. Same voice/rules as
    generate_meta(), but the script only has subject + clip count/duration —
    no hook/body to draw specific lines from, so the prompt describes it as
    a compilation video instead of a scripted short.
    """
    if script.get("_meta"):
        return script["_meta"]

    # Already generated once at render time (long_format_generator._save_script)
    # and persisted in the script JSON -- reuse it instead of paying for a
    # second Claude call with the exact same inputs.
    if script.get("title") and script.get("description"):
        return {
            "title": script["title"], "description": script["description"],
            "music_query": get_music_query(script), "music_suggestion": get_music_suggestion(),
            "_hashtags": script.get("hashtags") or _pick_hashtags(),
        }

    subject      = script.get("subject", "")
    n_clips      = len(script.get("clips_used", []))
    duration_min = round(script.get("duration_sec", 0) / 60, 1)
    opening_style = random.choice(_OPENING_STYLES)

    prompt = f"""You are writing a YouTube title and description for a long-form compilation video about: "{subject}"

This is a {duration_min}-minute compilation of {n_clips} short clips/stories, all connected by this theme.

Generate a title and description for this SPECIFIC video.

Opening style to use for the description: {opening_style}

Return ONLY valid JSON:
{{
  "title": "<max 80 characters, punchy, scroll-stopping, YouTube-style (can use a colon or bracket tag like '(2026)' or '| Compilation')>",
  "description": "<2-3 sentences derived from THIS video's specific theme, written as a compilation, ending with a question or hook to invite comments>"
}}

Rules:
- title: max 80 characters, no hashtags, no em dashes
- description: must be unique to this theme — do NOT use generic openers like "Most people don't realize", "The truth about", "Have you ever wondered", "In today's world", or any template phrase that could apply to any money video
- description: English only, ends with a strong statement or question
- No em dashes anywhere"""

    try:
        raw, _, _ = call_claude(_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        title = str(parsed.get("title", ""))[:80].strip()
        description = str(parsed.get("description", "")).strip()
        if title and description:
            logger.info(f"content_meta: Claude generated long-format title={title!r}")
            return {"title": title, "description": description, "music_query": get_music_query(script), "music_suggestion": get_music_suggestion()}
    except Exception as e:
        logger.warning(f"content_meta: Claude call failed for youtube_long ({e}), using fallback")

    return _fallback_meta_long(script)


def _fallback_meta_long(script: dict) -> dict:
    subject = script.get("subject", "").strip()
    title = (subject[:80] if subject else "Watch this")
    description = f"A compilation about {subject}." if subject else "Watch this compilation."
    return {"title": title, "description": description, "music_query": get_music_query(script), "music_suggestion": get_music_suggestion()}


def _book_promo_line(meta: dict) -> str:
    """The '📘 Get my book' footer line -- always just the bare phrase, no
    title, no URL. Per user (2026-08-06): the real link lives in bio, never
    paste it into the caption itself. Same text for every brand -- no
    per-brand title/link branching anymore. Never called when
    meta['_no_promo'] is set -- callers guard that."""
    return "📘 Get my book"


def _comment_cta_line(meta: dict) -> str:
    """The 'Comment "WORD" ...' line -- always a full sentence, same template
    for every brand including MindFile's no-promo-yet state (per user,
    2026-08-06: never leave it as a bare 'Comment "WORD".' fragment). A
    promo's own comment_suffix (see _mindfile_book_promo) overrides the
    default 'and I'll DM you my guide.' wording once that promo goes live.
    Returns '' if no comment-CTA word is set at all."""
    word = meta.get("_comment_cta_word")
    if not word:
        return ""
    promo = meta.get("_book_promo")
    if promo and promo.get("comment_suffix"):
        return f'Comment "{word}" {promo["comment_suffix"]}'
    return f'Comment "{word}" and I\'ll DM you my guide.'


def build_youtube_description(meta: dict) -> str:
    """Full YouTube description: generated text + optional reveal/comment-CTA
    (format9's "the answer is in the description" promise, see
    generate_meta_format9 — absent for every other format, so this is a no-op
    change for them) + book CTA + rotating hashtag set.

    _self_contained_description (format17 -- see generate_meta_format17) skips
    all of that: the description was already fully formatted (intro, per-item
    detail, its own CTA, its own hashtags) at script-gen time, so appending
    another book CTA/hashtag set on top would just duplicate it.

    _minimal_caption (format18 White Card, see _FORMAT18_MINIMAL_CAPTION):
    2026-08-23 reach test -- collapse to just the short title, no description,
    no hashtags, no CTA."""
    if meta.get("_minimal_caption"):
        return meta["title"]
    if meta.get("_self_contained_description"):
        return meta["description"]

    parts = [meta["description"]]
    if meta.get("_reveal"):
        parts.append(f"The verdict: {meta['_reveal']}")
    cta_line = _comment_cta_line(meta)
    if cta_line:
        parts.append(cta_line)
    if not meta.get("_no_promo"):
        parts.append(_book_promo_line(meta))
    parts.append(" ".join(meta.get("_hashtags", _pick_hashtags())[:_HASHTAGS_MAX_YOUTUBE]))
    return "\n\n".join(parts)


def build_social_caption(meta: dict) -> str:
    """Instagram / TikTok / Snapchat caption: comment-to-DM CTA leads, then the
    generated text, then the book CTA, then the rotating hashtag set, then the
    music credit last. CTA leads (not trails, unlike build_youtube_description)
    because IG/TikTok/Snapchat truncate the visible preview after ~2-3 lines
    behind "see more" -- the "Comment WORD" hook needs to be seen without a tap,
    so it has to be the very first thing in the caption (per user, 2026-08-06).

    Hashtags are stored in meta['_hashtags'] so YouTube and social share the SAME set per video.
    Same idea for the comment-CTA word (meta['_comment_cta_word']) -- rotated
    per video, but stays identical across platforms for a given video.

    _self_contained_description (format17): use the pre-formatted description
    verbatim, see build_youtube_description's docstring for why.

    _minimal_caption (format18 White Card): 2026-08-23 reach test -- same
    short-title-only collapse as build_youtube_description, see there.
    """
    if meta.get("_minimal_caption"):
        return meta["title"]
    if meta.get("_self_contained_description"):
        return meta["description"]

    no_promo = meta.get("_no_promo", False)
    if "_hashtags" not in meta:
        meta["_hashtags"] = _pick_hashtags_mindfile() if no_promo else _pick_hashtags()
    tags_str = " ".join(meta["_hashtags"][:_HASHTAGS_MAX_SOCIAL])

    if "_comment_cta_word" not in meta:
        meta["_comment_cta_word"] = _pick_comment_cta_word_mindfile() if no_promo else _pick_comment_cta_word()
    cta_line = _comment_cta_line(meta)

    body = meta["description"]
    if meta.get("_reveal"):
        # format9's "the answer is in the description" payoff -- see generate_meta_format9.
        # Absent for every other format, so this is a no-op for them.
        body += f"\n\nThe verdict: {meta['_reveal']}"

    parts = []
    if cta_line:
        parts.append(cta_line)
    parts.append(body)
    if not no_promo:
        parts.append(_book_promo_line(meta))
    parts.append(tags_str)
    caption = "\n\n".join(parts)

    credit = meta.get("_music_credit")
    if credit and credit.get("title"):
        license_short = credit["license"].split(" (")[0]   # "CC-BY-4.0 (Kevin MacLeod / ...)" -> "CC-BY-4.0"
        line = f"🎵 \"{credit['title']}\" by {credit.get('artist', 'unknown')} — licensed {license_short}"
        if credit.get("url"):
            line += f" — {credit['url']}"
        caption += f"\n\n{line}"

    return caption


def youtube_title(meta: dict) -> str:
    """YouTube post title -- the video's own first real description sentence
    (2026-08-10 correction: an earlier version of this used the "Comment
    WORD..." CTA line itself, which is exactly what the user said they did
    NOT want -- "ca doit pas être 'comment [word]'... ca doit être la 1ère
    phrase qui va après", i.e. the sentence that comes AFTER the CTA, not
    the CTA line).

    Also fixes a real bug spotted 2026-08-09: Zernio's YouTube post never
    had an explicit title sent, so it fell back to its own undocumented
    default (docs.zernio.com/platforms/youtube: "Defaults to first line of
    content") -- which mangled the CTA line into something unreadable
    instead of using real video content at all.

    Falls back to meta['title'] if description is empty.

    _minimal_caption (format18 White Card): 2026-08-23 reach test -- use the
    short punchy title directly instead of pulling a sentence out of a
    description that isn't being shown anywhere, see
    build_youtube_description's docstring."""
    if meta.get("_minimal_caption"):
        return (meta.get("title") or "Watch this")[:100]
    description = (meta.get("description") or "").strip()
    if description:
        m = re.search(r"[.!?](?=\s|$)", description)
        first_sentence = description[:m.end()].strip() if m else description
        if first_sentence:
            return first_sentence[:100]
    return (meta.get("title") or "Watch this")[:100]
