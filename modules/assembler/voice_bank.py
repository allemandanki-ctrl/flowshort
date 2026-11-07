"""
Voice bank for character-driven TTS formats (format14 "Interrogatoire" and
any future clash-persona formats) -- maps an ARCHETYPE profile (gender, age
register, tone, accent) to the closest stock edge-tts voice.

Deliberately archetype-only: profiles describe traits a character
plausibly has (e.g. "gravelly", "mature", "american"), never the specific
real actor/performer's own timbre. See BESOIN_TON_AVIS.md -- targeting
recognition of a real, identifiable person's voice is a hard no regardless
of fidelity; this bank only ever returns one of Microsoft's stock neural
voices, picked for a general resemblance to the ROLE, not the performer.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class _VoiceEntry:
    voice_id: str
    gender: str            # "male" | "female"
    accent: str            # see _ACCENT_ALIASES keys
    age_register: str      # "young" | "adult" | "mature"
    tone: tuple[str, ...]  # free-form descriptive tags


# Tags come from edge-tts's own VoicePersonalities metadata where available,
# supplemented with the documented character of each stock voice.
_BANK: tuple[_VoiceEntry, ...] = (
    _VoiceEntry("en-US-ChristopherNeural", "male", "american", "mature", ("authoritative", "reliable", "calm", "serious")),
    _VoiceEntry("en-US-GuyNeural",         "male", "american", "adult",  ("passionate", "energetic", "intense")),
    _VoiceEntry("en-US-EricNeural",        "male", "american", "adult",  ("rational", "deadpan", "measured")),
    _VoiceEntry("en-US-RogerNeural",       "male", "american", "mature", ("lively", "gravelly", "weathered")),
    _VoiceEntry("en-US-SteffanNeural",     "male", "american", "adult",  ("rational", "calm", "measured")),
    _VoiceEntry("en-US-AndrewNeural",      "male", "american", "adult",  ("warm", "confident", "honest")),
    _VoiceEntry("en-US-BrianNeural",       "male", "american", "young",  ("casual", "approachable", "sincere")),
    _VoiceEntry("en-GB-RyanNeural",        "male", "british",  "adult",  ("authoritative", "reliable", "crisp")),
    _VoiceEntry("en-GB-ThomasNeural",      "male", "british",  "young",  ("soft", "casual", "sincere")),
    _VoiceEntry("en-AU-WilliamMultilingualNeural", "male", "australian", "adult", ("friendly", "confident")),
    _VoiceEntry("en-IE-ConnorNeural",      "male", "irish",    "adult",  ("warm", "friendly", "lively")),
    _VoiceEntry("en-IN-PrabhatNeural",     "male", "indian",   "adult",  ("calm", "measured", "formal")),
    _VoiceEntry("en-ZA-LukeNeural",        "male", "south_african", "adult", ("firm", "measured")),
    _VoiceEntry("en-CA-LiamNeural",        "male", "canadian", "adult",  ("friendly", "calm")),

    _VoiceEntry("en-US-AriaNeural",        "female", "american", "young",  ("confident", "positive", "sharp")),
    _VoiceEntry("en-US-JennyNeural",       "female", "american", "adult",  ("warm", "considerate", "comforting")),
    _VoiceEntry("en-US-MichelleNeural",    "female", "american", "adult",  ("pleasant", "friendly", "neutral")),
    _VoiceEntry("en-US-AvaNeural",         "female", "american", "young",  ("expressive", "caring", "pleasant")),
    _VoiceEntry("en-US-EmmaNeural",        "female", "american", "young",  ("cheerful", "energetic", "clear")),
    _VoiceEntry("en-US-AnaNeural",         "female", "american", "young",  ("cute", "childlike", "light")),
    _VoiceEntry("en-GB-SoniaNeural",       "female", "british",  "adult",  ("warm", "crisp", "formal")),
    _VoiceEntry("en-GB-LibbyNeural",       "female", "british",  "young",  ("light", "friendly")),
    _VoiceEntry("en-AU-NatashaNeural",     "female", "australian", "adult", ("friendly", "confident")),
    _VoiceEntry("en-IE-EmilyNeural",       "female", "irish",    "adult",  ("warm", "friendly")),
    _VoiceEntry("en-IN-NeerjaNeural",      "female", "indian",   "adult",  ("calm", "formal")),
    _VoiceEntry("en-IN-NeerjaExpressiveNeural", "female", "indian", "adult", ("expressive", "warm")),
    _VoiceEntry("en-ZA-LeahNeural",        "female", "south_african", "adult", ("firm", "measured")),
    _VoiceEntry("en-CA-ClaraNeural",       "female", "canadian", "adult",  ("friendly", "calm")),
)

_ACCENT_ALIASES = {
    "american":      {"american", "us", "usa", "general american", "neutral"},
    "british":       {"british", "uk", "english"},
    "australian":    {"australian", "aussie"},
    "irish":         {"irish"},
    "indian":        {"indian"},
    "canadian":      {"canadian"},
    "south_african": {"south african", "south_african"},
}

_AGE_ALIASES = {
    "young":  {"young", "youthful", "teen", "child", "childlike", "20s"},
    "adult":  {"adult", "30s", "40s", "middle-aged", "middle aged"},
    "mature": {"mature", "old", "elderly", "50s", "60s", "aged", "weathered"},
}


def _normalize(value: str, aliases: dict[str, set[str]], default: str) -> str:
    v = (value or "").strip().lower()
    for key, names in aliases.items():
        if v in names:
            return key
    return default


def select_voice(gender: str, accent: str = "", age_register: str = "", tone: list[str] | None = None) -> str:
    """
    Pick the closest stock edge-tts voice for an archetype profile.
    Gender is a hard filter (a mismatched gender reads as wrong far more
    than any tone/accent miss); accent and age each add a fixed weight;
    tone adds one point per overlapping tag. Ties break on bank order, so
    the same profile always resolves to the same voice.
    """
    gender_n = (gender or "male").strip().lower()
    accent_n = _normalize(accent, _ACCENT_ALIASES, "american")
    age_n    = _normalize(age_register, _AGE_ALIASES, "adult")
    tone_set = {t.strip().lower() for t in (tone or [])}

    candidates = [v for v in _BANK if v.gender == gender_n] or list(_BANK)

    def score(v: _VoiceEntry) -> tuple[int, int, int]:
        return (
            2 if v.accent == accent_n else 0,
            len(tone_set & set(v.tone)),
            1 if v.age_register == age_n else 0,
        )

    return max(candidates, key=score).voice_id
