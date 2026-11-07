"""
Main pipeline — produces one video for a given format and subject.

Usage:
  python pipeline.py [format] [subject]
  python pipeline.py format2 "investir avec 100 euros par mois"
  python pipeline.py format3
  python pipeline.py              # defaults: format2, random wealth subject
"""

import logging
import random
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pipeline")

sys.path.insert(0, str(Path(__file__).parent))

_DEFAULT_SUBJECTS = [
    "investing 100 dollars a month",
    "how the wealthy think differently about money",
    "the 3 habits of self-made millionaires",
    "why the rich don't work for money",
    "how the rule of 72 doubles your money",
    "stop buying what you don't need",
    "the difference between assets and liabilities",
    "how to build a cash machine",
]

def random_format() -> str:
    """Weighted random: format1 (clip edit) ~33%, format3 (luxury) ~67%."""
    return random.choices(["format1", "format3"], weights=[1, 2])[0]


def run(
    fmt: str,
    subject: str,
    clip_path: str | Path | None = None,
    use_hook: bool = False,
    hook_transition: str = "zoom_punch",
    pov_text: str | None = None,
    hook_speed_ramp: bool = True,
    hook_cut_style: str = "flash",   # "flash" | "glitch" | "none"
    manual: bool = False,
    beat_match: bool = False,
    hook_recut: bool = False,
    template_mode: bool = False,
    quote_override: str | None = None,
) -> Path:
    """
    clip_path: optional explicit clip for format1, bypassing the assets/clips/
    auto-rotation pool (e.g. a specific file the user hands over directly).
    Such a clip is used as-is and NOT archived into utilisés/.

    quote_override: format3/format3a/format3b and mf2 only -- forces the
    script's on-screen `hook` line (the ENTIRE on-screen text for these two
    "one quote/statement over B-roll" formats) to this exact text instead of
    whatever generate_script invented from `subject` alone. 2026-08-17, real
    user ask (modules.assembler.viral_remix's "quote" archetype): a remix of
    an already-viral quote clip should show a close PARAPHRASE of that quote
    -- same message, different wording -- not an unrelated line the script
    generator invented from just a topic (format3's own quote-writing prompt
    is even forced into a specific violent/mortality-stakes register by
    design, nothing like a neutral paraphrase). generate_script still runs
    normally beforehand for its body/cta/description/hashtags/visual_keywords
    -- only the hook field gets overwritten, right after generation, before
    visuals/assembly. Deliberately NOT threaded into mf2's `body` (its 2-3
    cards "decode" the hook line) -- overriding only the hook there can leave
    the decode cards read as slightly generic relative to the new line rather
    than a tight verbatim match; accepted for the same reason `subject` was
    already accepted as a generic topic elsewhere in this pipeline, and the
    common case (a brand with only format3, no mf2) never hits it anyway.

    use_hook: format3 only. Prepends a transitional hook (assets/hooks/)
    with a synced transition + "POV: ..." caption; falls back to the plain
    assembler if the hook bank is empty. hook_transition picks the cut style
    (hard_cut/whip_pan/zoom_punch/flash_white/distance/circle_crop/pixelize/
    dissolve/diagonal/squeeze/wind/radial/slice/cover/reveal/match_cut).
    hook_speed_ramp/hook_cut_style toggle the transition "polish" layer
    (speed-ramped hook tail + a flash or glitch accent + impact-hit landing
    on the cut) independently of the cut style -- on by default, can be
    disabled/switched to isolate which part of the edit is being judged.
    pov_text overrides
    the auto-generated caption (e.g. set from the dashboard).

    manual: routes this video to the manual-publish queue (modules/publish/manual_queue.py,
    only meaningful for format3). Hard-forces use_hook off -- a manual
    video is never hook-prefixed, the trending sound added by hand IS its hook.
    Also gets the hook video's beat-matched background cutting even without a
    hook (see modules.assembler.format3.assemble_format3a_manual).

    beat_match: format3 WITHOUT a hook or manual routing -- "Matcher sur le
    beat" toggle, cuts the mixed image/video pool on the music's real beat
    grid instead of the plain fixed-per-card duration (same mechanism as
    `manual`, just user-triggered rather than queue-triggered).

    hook_recut: format3 only, mutually exclusive with use_hook/manual/
    beat_match (checked in that order). The "interrupted payoff" structure --
    hook -> content -> back to the hook (continuation or a second, unrelated
    hook clip) -> content again, see modules.assembler.assemble_with_hook_recut.

    template_mode: format3 only, mutually exclusive with all of the above
    (checked last). "Mode template" -- no hook, no beat matching, exactly 9
    equal hard cuts totaling 8.5s, then split back into those 9 segments and
    uploaded to a per-video Drive folder for the user to edit for real in a
    CapCut template (see modules.assembler.format3.assemble_format3a_template
    and modules.publish.template_parts).
    """
    if manual or hook_recut:
        use_hook = False
    if template_mode:
        use_hook = manual = beat_match = hook_recut = False

    logger.info("=" * 60)
    logger.info(f"PIPELINE START | {fmt} | {subject}" + (" | MANUAL QUEUE" if manual else ""))
    logger.info("=" * 60)

    from modules.script_generator import generate_script
    # format3a/3b share the same script structure as format3
    script_fmt = "format3" if fmt in ("format3a", "format3b") else fmt
    script = generate_script(script_fmt, subject=subject, recut=hook_recut)
    script["format"] = fmt   # restore actual format for dispatcher
    logger.info(f"      hook: {script.get('hook', '')[:70]}")

    visuals = None   # only set by the format3*/3a branches (format1 sources no Drive clips)

    if fmt == "format1":
        # Clip edit: explicit clip if given, else pick from assets/clips/ auto-pool
        logger.info("[2/4] Sélection clip...")
        if clip_path is not None:
            clip_path = Path(clip_path)
            if not clip_path.exists():
                raise FileNotFoundError(f"Clip introuvable : {clip_path}")
            logger.info(f"      clip (fourni) : {clip_path.name}")
        else:
            from modules.clips.clip_selector import select_clip
            clip_path = select_clip()
            logger.info(f"      clip : {clip_path.name}")
        script["clip_used"] = clip_path.name   # persisted in script JSON for dedup

        from modules.clips.music_removal import maybe_strip_music
        clip_path = maybe_strip_music(clip_path)

        logger.info("[3/4] Transcription Whisper...")
        from modules.clips.clip_transcriber import transcribe_clip
        transcript = transcribe_clip(clip_path)
        logger.info(f"      mots={len(transcript['words'])} | durée={transcript['duration']:.1f}s")

        logger.info("[4/4] Assemblage clip edit...")
        from modules.assembler import assemble_clip_edit
        out = assemble_clip_edit(
            script, clip_path, transcript["words"],
            full_text=transcript.get("full_text", ""),
        )

    elif fmt == "format7":
        # Clip edit 2.0: same source pool as format1, but climax-trimmed,
        # silence-removed, B-roll burst (curated Pinterest pool, mood-matched),
        # word-by-word captions. Kept as its OWN format per explicit user
        # request, not folded into format1.
        logger.info("[2/4] Sélection clip...")
        if clip_path is not None:
            clip_path = Path(clip_path)
            if not clip_path.exists():
                raise FileNotFoundError(f"Clip introuvable : {clip_path}")
            logger.info(f"      clip (fourni) : {clip_path.name}")
        else:
            from modules.clips.clip_selector import select_clip
            clip_path = select_clip()
            logger.info(f"      clip : {clip_path.name}")
        script["clip_used"] = clip_path.name

        from modules.clips.music_removal import maybe_strip_music
        clip_path = maybe_strip_music(clip_path)

        logger.info("[3/4] Transcription Whisper...")
        from modules.clips.clip_transcriber import transcribe_clip
        transcript = transcribe_clip(clip_path)
        logger.info(f"      mots={len(transcript['words'])} | durée={transcript['duration']:.1f}s")

        logger.info("[4/4] Assemblage clip edit 2.0...")
        from modules.assembler import assemble_clip_edit_v2
        out = assemble_clip_edit_v2(
            script, clip_path, transcript["words"],
            full_text=transcript.get("full_text", ""),
            duration=transcript.get("duration"),
        )

    elif fmt in ("format3", "format3a", "format3b"):
        # Luxury slideshow: mixed pool of real Drive/Pexels video clips +
        # Drive/Pexels/Pollinations images, shuffled together -- whichever
        # media types are actually available. format3a/format3b are kept as
        # accepted aliases (old persisted scripts / retries) but behave
        # identically to format3 now.
        script["format"] = fmt
        if pov_text:
            script["pov_text_override"] = pov_text
        if quote_override:
            script["hook"] = quote_override
            logger.info(f"      hook overridden -> {quote_override[:70]}")
        logger.info("[2/3] Récupération visuels (vidéos + images)...")
        from modules.visuals import fetch_visuals
        visuals = fetch_visuals(script)
        logger.info(f"      videos={len(visuals.get('videos', []))} | images={len(visuals.get('images', []))}")

        logger.info("[3/3] Assemblage...")
        if manual:
            from modules.assembler.format3 import assemble_format3a_manual
            out = assemble_format3a_manual(script, visuals)
        elif template_mode:
            from modules.assembler.format3 import assemble_format3a_template
            out = assemble_format3a_template(script, visuals)
        elif use_hook:
            from modules.assembler import assemble_with_hook
            out = assemble_with_hook(script, visuals, transition=hook_transition,
                                     speed_ramp=hook_speed_ramp, cut_style=hook_cut_style)
        elif beat_match:
            from modules.assembler.format3 import assemble_format3a_manual
            out = assemble_format3a_manual(script, visuals)
        elif hook_recut:
            from modules.assembler import assemble_with_hook_recut
            out = assemble_with_hook_recut(script, visuals)
        else:
            from modules.assembler import assemble
            out = assemble(script, visuals, voice_result=None, timestamps_result=None, music_path=None)

    elif fmt == "format6":
        # Ranking list: TTS voice-over + Whisper transcript + dark Pexels B-roll
        # + number overlays + synced captions. Always English regardless of
        # CONTENT_LANGUAGE (the format's whole premise is English-language
        # listicle content, per the user's own examples).
        logger.info("[2/5] Génération voix (TTS)...")
        from modules.voice import generate_voice
        voice_result = generate_voice(script, voice="en-US-ChristopherNeural")
        if not voice_result:
            raise RuntimeError("format6 requires a voice-over but none was generated")
        logger.info(f"      audio: {voice_result['audio_path']}")

        logger.info("[3/5] Transcription Whisper (mots + timing)...")
        from modules.clips.clip_transcriber import transcribe_clip
        timestamps_result = transcribe_clip(Path(voice_result["audio_path"]))
        logger.info(f"      mots={len(timestamps_result['words'])} | durée={timestamps_result['duration']:.1f}s")

        logger.info("[4/5] Récupération B-roll dark Pexels...")
        from modules.visuals import fetch_visuals
        visuals = fetch_visuals(script)
        logger.info(f"      videos={len(visuals.get('videos', []))}")

        logger.info("[5/5] Assemblage...")
        from modules.assembler import assemble
        out = assemble(script, visuals, voice_result=voice_result,
                       timestamps_result=timestamps_result, music_path=None)

    elif fmt in ("format8", "format9", "format10", "format11", "format12", "format14", "format15", "format16", "format17", "long2", "long3",
                 "mf5", "mf6", "mf_long"):
        # Pure motion graphics / UI mockup / self-contained two-voice dialogue / news
        # slideshow / archetype-TTS long formats -- no shared voice/B-roll pipeline
        # needed, straight to assembly. (long1 is NOT here -- it needs real human
        # audio uploaded via its own FlowShort panel first, no CLI single-shot path.)
        # mf5/mf6/mf_long (MindFile brand) follow the exact same shape: mf5 reuses
        # format11's two-voice engine, mf6 reuses format10's chat-bubble engine,
        # mf_long synthesizes its own narrator TTS like long3 does. (mf4 was cut
        # entirely 2026-07-28, see config.py.)
        logger.info("[2/2] Assemblage...")
        from modules.assembler import assemble
        out = assemble(script, {}, voice_result=None, timestamps_result=None, music_path=None)

    elif fmt == "mf1":
        # MindFile "Case File": TTS voice-over + Whisper transcript + real-life
        # Pexels B-roll + synced captions -- same shape as format6's voice+broll
        # pipeline below, just via the generic assemble_voice_format engine
        # instead of format6's ranking/number-overlay one.
        logger.info("[2/5] Génération voix (TTS)...")
        from modules.voice import generate_voice
        voice_result = generate_voice(script, voice="en-US-JennyNeural")
        if not voice_result:
            raise RuntimeError("mf1 requires a voice-over but none was generated")
        logger.info(f"      audio: {voice_result['audio_path']}")

        logger.info("[3/5] Transcription Whisper...")
        from modules.clips.clip_transcriber import transcribe_clip
        timestamps_result = transcribe_clip(Path(voice_result["audio_path"]))
        logger.info(f"      mots={len(timestamps_result['words'])} | durée={timestamps_result['duration']:.1f}s")

        logger.info("[4/5] Récupération B-roll Pexels...")
        from modules.visuals import fetch_visuals
        visuals = fetch_visuals(script)
        logger.info(f"      videos={len(visuals.get('videos', []))}")

        logger.info("[5/5] Assemblage...")
        from modules.assembler import assemble
        out = assemble(script, visuals, voice_result=voice_result,
                       timestamps_result=timestamps_result, music_path=None)

    elif fmt == "mf2":
        # MindFile "Mind Aesthetic": no voice, quote-card overlay on striking
        # real-life video/image B-roll -- same visuals+assemble shape as
        # format3a's own branch above, no hook-prefix (that mechanism reads
        # from a ResetWorth-specific luxury hook bank, not reused here).
        if quote_override:
            script["hook"] = quote_override
            logger.info(f"      hook overridden -> {quote_override[:70]}")
        logger.info("[2/3] Récupération visuels...")
        from modules.visuals import fetch_visuals
        visuals = fetch_visuals(script)
        logger.info(f"      videos={len(visuals.get('videos', []))}")

        logger.info("[3/3] Assemblage...")
        from modules.assembler import assemble
        out = assemble(script, visuals, voice_result=None, timestamps_result=None, music_path=None)

    elif fmt == "mf3":
        # MindFile "Red Flag Ranking": TTS voice-over + Whisper transcript +
        # dark Pexels B-roll + number overlays -- identical shape to format6's
        # branch above, via the same assemble_format6 engine.
        logger.info("[2/5] Génération voix (TTS)...")
        from modules.voice import generate_voice
        voice_result = generate_voice(script, voice="en-US-ChristopherNeural")
        if not voice_result:
            raise RuntimeError("mf3 requires a voice-over but none was generated")
        logger.info(f"      audio: {voice_result['audio_path']}")

        logger.info("[3/5] Transcription Whisper (mots + timing)...")
        from modules.clips.clip_transcriber import transcribe_clip
        timestamps_result = transcribe_clip(Path(voice_result["audio_path"]))
        logger.info(f"      mots={len(timestamps_result['words'])} | durée={timestamps_result['duration']:.1f}s")

        logger.info("[4/5] Récupération B-roll dark Pexels...")
        from modules.visuals import fetch_visuals
        visuals = fetch_visuals(script)
        logger.info(f"      videos={len(visuals.get('videos', []))}")

        logger.info("[5/5] Assemblage...")
        from modules.assembler import assemble
        out = assemble(script, visuals, voice_result=voice_result,
                       timestamps_result=timestamps_result, music_path=None)

    else:
        raise ValueError(f"Format inconnu : {fmt}. Valides : format1, format3, format6, format7, format8, format9, format10, format11, format12, format14, format15, format16, format17, long2, long3 (long1 via FlowShort panel only), mf1, mf2, mf3, mf5, mf6, mf_long")

    # generate_script() already wrote the script JSON to disk BEFORE assembly ran --
    # assembly is where select_music/select_track mutate `script` in place (e.g.
    # `_music_credit`, see modules/music/music_matcher.py + music_library.py). Publishing
    # reloads the script from disk later (run_daily.py, app.py _find_script /
    # _generate_batch, modules.users._tag_script) by globbing the VIDEO's own
    # timestamp prefix -- so the file must both (a) carry that same prefix and
    # (b) contain whatever got mutated during assembly. Renaming to the video's
    # timestamp (instead of re-globbing for it, which breaks whenever assembly
    # takes long enough for the two timestamps to drift apart -- Drive/OAuth
    # waits, TTS, Whisper, slow encodes) fixes both at once.
    try:
        import json
        script_path = script.get("_script_path")
        if script_path:
            old_path = Path(script_path)
            new_path = old_path.parent / (out.stem[:15] + old_path.name[15:])
            if new_path != old_path:
                old_path.rename(new_path)
            new_path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"pipeline: couldn't persist/relocate script after assembly: {e}")

    size_mb = out.stat().st_size / (1024 * 1024)

    # Drive clips: mark sourced clips as used only after a successful render.
    drive_ids = (visuals or {}).get("drive_usage_ids", [])
    if drive_ids:
        try:
            from modules.drive_clips import commit_usage
            commit_usage(drive_ids)
        except Exception as e:
            logger.warning(f"drive_clips commit_usage failed (non-fatal): {e}")

    logger.info("=" * 60)
    logger.info(f"DONE | {out.name} | {size_mb:.1f} MB")
    logger.info("=" * 60)
    return out


if __name__ == "__main__":
    args    = sys.argv[1:]
    fmt     = args[0] if len(args) >= 1 else random_format()
    subject = args[1] if len(args) >= 2 else random.choice(_DEFAULT_SUBJECTS)

    valid = {"format1", "format3", "format6", "format7"}
    if fmt not in valid:
        logger.error(f"Format inconnu '{fmt}'. Valides : {', '.join(sorted(valid))}")
        sys.exit(1)

    try:
        video = run(fmt, subject)
        import subprocess
        subprocess.Popen(["start", "", str(video)], shell=True)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        sys.exit(1)
