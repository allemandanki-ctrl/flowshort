"""
Tests for the transitional hooks system: transitions.py, hooks.py, music_rotation.py.
No real generation, no network calls except real ffprobe on the placeholder clips
(cheap/local) and real librosa beat detection (local, offline).
Run: python tests/test_hooks.py
"""

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import transitions, hooks, assembler
from modules.music import music_rotation


# ── transitions.py ──────────────────────────────────────────────────────

def test_is_hard():
    assert transitions.is_hard("hard_cut")
    assert transitions.is_hard("match_cut")
    assert not transitions.is_hard("zoom_punch")
    assert not transitions.is_hard("whip_pan")
    assert not transitions.is_hard("flash_white")


def test_xfade_name_mapping():
    assert transitions.xfade_name("zoom_punch") == "zoomin"
    assert transitions.xfade_name("whip_pan") == "hblur"
    assert transitions.xfade_name("flash_white") == "fadewhite"


def test_xfade_name_rejects_hard_cut():
    try:
        transitions.xfade_name("hard_cut")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_xfade_name_rejects_unknown():
    try:
        transitions.xfade_name("teleport")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_transition_filters_hard_cut_is_plain_concat():
    f = transitions.build_transition_filters("a", "b", "out", "hard_cut", hook_duration=3.0)
    assert len(f) == 1
    assert "concat=n=2:v=1:a=0" in f[0]
    assert "[a][b]" in f[0]
    assert "[out]" in f[0]
    assert "xfade" not in f[0]


def test_build_transition_filters_xfade_offset_lands_on_hook_end():
    hook_dur = 4.0
    dur = 0.25
    f = transitions.build_transition_filters("a", "b", "out", "zoom_punch", hook_dur, dur)
    assert len(f) == 1
    line = f[0]
    assert "xfade=transition=zoomin" in line
    # offset = hook_duration - duration -> transition finishes exactly at hook end
    assert f"offset={hook_dur - dur:.3f}" in line
    assert f"duration={dur:.3f}" in line


def test_build_transition_filters_offset_never_negative():
    # A very short hook with a longer transition duration must not produce offset<0.
    f = transitions.build_transition_filters("a", "b", "out", "whip_pan",
                                             hook_duration=0.1, duration=0.5)
    assert "offset=0.000" in f[0]


def _with_isolated_hooks_dir(fn):
    """
    Run fn() with hooks.HOOKS_DIR redirected to a throwaway temp dir, so
    synthetic placeholder fixtures NEVER touch the real assets/hooks/ bank
    (which now holds real transitionalhooks.com clips -- polluting it with
    gradient placeholders would let a test run silently degrade production).
    """
    import shutil
    orig = hooks.HOOKS_DIR
    tmp = Path(__file__).parent / "_tmp_hooks_dir"
    shutil.rmtree(tmp, ignore_errors=True)
    hooks.HOOKS_DIR = tmp
    try:
        return fn()
    finally:
        hooks.HOOKS_DIR = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_probe_duration_on_placeholder_hooks():
    def _run():
        hooks.generate_placeholder_hooks()
        for h in hooks.list_hooks():
            dur = transitions.probe_duration(h["_path"])
            assert dur > 0, h["id"]
            assert abs(dur - h["duration"]) < 0.2, (h["id"], dur, h["duration"])
    _with_isolated_hooks_dir(_run)


def test_detect_first_beat_returns_float_and_never_raises():
    def _run():
        made = hooks.generate_placeholder_hooks()
        # Beat detection runs against actual audio; the placeholder is silence
        # (anullsrc), so onset_detect should find nothing meaningful and fall
        # back to 0.0 -- the important thing is it NEVER raises, even on a
        # musically-empty track.
        silent_path = hooks.HOOKS_DIR / made[0]["filename"] if made else None
        t = transitions.detect_first_beat(str(silent_path)) if silent_path else 0.0
        assert isinstance(t, float)
        assert t >= 0.0
    _with_isolated_hooks_dir(_run)


# ── assembler.py: hook-join filter graph (regression for the runaway-duration bug) ─

def test_hook_join_trims_pov_overlay_to_hook_duration():
    # Bug: [2:v] (POV PNG) is fed via `-loop 1` (infinite, never sends EOF).
    # xfade never noticed (only reads up to the transition window) but concat
    # (hard_cut/match_cut) hung reading [hookpov] forever since it never got
    # an EOF signal -- rendered for 8+ minutes instead of ~hook_dur+content_dur.
    # The overlay output MUST be explicitly trimmed to hook_dur so both paths
    # (xfade and concat) get a properly-terminating stream.
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=2.6, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path="music.mp3", music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "trim=duration=2.600" in filter_complex, filter_complex
    # The trim must apply BEFORE hookpov is consumed by concat.
    assert filter_complex.index("trim=duration=2.600") < filter_complex.index("[hookpov]")


def test_hook_join_has_shortest_flag():
    # Bug: music is fed in untrimmed (often a several-minute full track). Without
    # -shortest, ffmpeg's output duration follows the LONGEST mapped stream (the
    # full music track), not the video -- producing a video that freezes on its
    # last frame while music keeps playing for minutes. -shortest makes the
    # (correctly-bounded) video stream govern total output length instead.
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path="music.mp3", music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
    )
    assert "-shortest" in cmd


def test_hook_join_trims_pov_overlay_for_xfade_too():
    # Same fix must apply to the xfade path -- it "worked" before only by luck
    # (xfade never read past the transition window), not because it was correct.
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="zoom_punch", t_dur=0.2, output_path="out.mp4",
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "trim=duration=3.000" in filter_complex, filter_complex


# ── transitions.py: speed ramp + flash/impact (the "polish" layer) ────────

def test_speed_ramp_effective_duration_is_shorter():
    filters, eff = transitions.build_speed_ramp_filters(
        "in", "out", hook_dur=3.0, ramp_duration=0.5, ramp_factor=1.35)
    assert filters, "expected ramp filters for a hook long enough to hold one"
    # 0.5s at 1.35x plays back in 0.5/1.35 = 0.370s -> shorter than the original.
    assert eff < 3.0
    assert abs(eff - (2.5 + 0.5 / 1.35)) < 0.001


def test_speed_ramp_noop_when_it_does_not_fit():
    # A 0.6s hook can't lose 0.5s to a ramp (>= 80% of hook_dur) and still be coherent.
    filters, eff = transitions.build_speed_ramp_filters(
        "in", "out", hook_dur=0.6, ramp_duration=0.5, ramp_factor=1.35)
    assert filters == []
    assert eff == 0.6


def test_speed_ramp_filters_split_at_correct_point():
    filters, eff = transitions.build_speed_ramp_filters(
        "hookv", "out", hook_dur=4.0, ramp_duration=1.0, ramp_factor=2.0)
    joined = ";".join(filters)
    assert "trim=0:3.000" in joined            # head = hook_dur - ramp_duration
    assert "trim=3.000:4.000" in joined        # tail = last ramp_duration seconds
    assert "/2.000" in joined                  # tail slowed/sped by ramp_factor
    assert eff == 3.0 + 1.0 / 2.0


def test_detect_beat_grid_on_real_phonk_track():
    # Regression for a real bug: an ambient/silent intro before the beat
    # kicks in produced one huge first gap (8s+) -- one long static clip
    # instead of a fast-cut montage. Durations must be capped, not just
    # summed, so a long intro still gets cut at a reasonable rate.
    src = Path(__file__).parent.parent / "assets" / "music_test_phonk" / "this_is_phonk.mp3"
    if not src.exists():
        return   # test-only asset, not present in every environment
    durs = transitions.detect_beat_grid(str(src), start_offset=0.0)
    assert len(durs) > 10
    assert all(0.15 < d <= 2.5 for d in durs), [d for d in durs if not (0.15 < d <= 2.5)]


def test_detect_beat_grid_anchored_at_zero_not_first_raw_beat():
    # Regression for a real bug found in user testing: the first clip in the
    # rendered montage wasn't aligned to the music, while every clip after it
    # was "perfect". Root cause: beat_track (periodic tracker) and
    # detect_first_beat (onset detector, used to trim the audio so it starts
    # ON a beat) don't always agree on where t=0's beat actually is -- using
    # beat_track's own first detected timestamp as the grid's start left a
    # mismatched opening gap. A regular grid (one fixed period, all durations
    # identical) removes the dependency on that first raw timestamp entirely.
    src = Path(__file__).parent.parent / "assets" / "music_test_phonk" / "this_is_phonk.mp3"
    if not src.exists():
        return
    durs = transitions.detect_beat_grid(str(src), start_offset=0.0, max_duration=16.0)
    assert len(durs) > 5
    assert len(set(round(d, 3) for d in durs)) == 1, "all cut durations must be identical (regular grid)"


def test_loop_clips_list_clip_dur_uses_exact_count():
    # Real beat-grid durations aren't evenly spaced -- _loop_clips_to_duration
    # must produce exactly len(clip_dur) entries (one per beat-grid slot),
    # not a duration-summed count like the uniform-float path.
    clips = ["a.mp4", "b.mp4"]
    beat_durs = [1.0, 0.8, 1.2, 0.9, 1.1]
    result = assembler._loop_clips_to_duration(clips, target_duration=999, clip_dur=beat_durs)
    assert len(result) == len(beat_durs)


def test_format1_cmd_list_clip_dur_trims_each_clip_individually():
    cmd = assembler._format1_cmd(
        bg_clips=["a.mp4", "b.mp4", "c.mp4"], overlay_pngs=["p0.png"],
        seg_dur=4, total_dur=4, music_path=None, output_path="out.mp4",
        clip_dur=[1.0, 0.7, 1.3],
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "trim=duration=1.000" in filter_complex
    assert "trim=duration=0.700" in filter_complex
    assert "trim=duration=1.300" in filter_complex


def test_speed_ramp_runs_on_real_hevc_hook():
    # Regression for a real bug: some transitionalhooks.com clips are HEVC
    # at 1440x2560 (not 1080x1920). Referencing the same scaled label twice
    # (implicit fan-out) triggered an ffmpeg bug where the second reference
    # bypassed scale/crop entirely, feeding concat raw decoder dimensions --
    # "Input link parameters do not match" -- traced to this exact file.
    # Fixed with an explicit `split` filter. This test runs REAL ffmpeg
    # against the actual clip that exposed it, because the string-only tests
    # above would not have caught it (the generated filter syntax was valid,
    # it just behaved wrong at runtime for this source).
    src = Path(__file__).parent.parent / "assets" / "hooks" / "th_ankle_break_hook.mp4"
    if not src.exists():
        return   # asset not present in this environment -- nothing to verify
    import subprocess
    # fps=30 passed here too -- a second real bug on this same file: the ramp's
    # concat left hookv on a high-precision (1/1000000) timebase instead of
    # 1/30, and xfade (unlike concat) refuses to join mismatched timebases.
    # Chaining into a real xfade below (not just probing [hookv] alone) is
    # what catches that -- an isolated [hookv] probe stayed green the whole
    # time this bug was live.
    filters, eff = transitions.build_speed_ramp_filters(
        "hookv_scaled", "hookv", hook_dur=1.8, ramp_duration=0.5, ramp_factor=1.35, fps=30)
    graph = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,format=yuv420p,fps=30[hookv_scaled];"
        + ";".join(filters)
        + f";color=c=black:s=1080x1920:d=2:rate=30[bg];"
          f"[hookv][bg]xfade=transition=zoomin:duration=0.2:offset={eff-0.2:.3f}[out]"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-filter_complex", graph,
         "-map", "[out]", "-f", "null", "-"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-500:]


def test_synthesize_impact_hit_produces_real_audio():
    out = Path(__file__).parent / "_tmp_impact.wav"
    try:
        transitions.synthesize_impact_hit(0.18, str(out))
        assert out.exists()
        dur = transitions.probe_duration(str(out))
        assert abs(dur - 0.18) < 0.05
    finally:
        out.unlink(missing_ok=True)


def test_build_flash_cut_filters_positions_fade_at_hook_tail():
    # fade's `st` defaults to the clip's start (t=0) -- without explicitly
    # positioning it at hook_duration - flash_duration, the hook would flash
    # white on its very FIRST frame instead of right before the cut.
    filters = transitions.build_flash_cut_filters(
        "hookpov", "contv", "hookpov_fl", "contv_fl",
        hook_duration=3.0, flash_duration=0.1)
    joined = ";".join(filters)
    assert "fade=t=out:st=2.900:d=0.100:color=white" in joined
    assert "fade=t=in:st=0.000:d=0.100:color=white" in joined


# ── assembler.py: polish layer wired into the hook-join filter graph ──────

def test_hook_join_speed_ramp_shrinks_pov_window():
    # Critical invariant: once speed_ramp shortens the hook, the POV overlay
    # window / trim / transition offset must all use the SHORTER effective
    # duration -- using the original hook_dur here would reintroduce exactly
    # the class of sync bug already fixed twice today.
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        speed_ramp=True, ramp_duration=0.5, ramp_factor=1.35,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    expected_eff = 2.5 + 0.5 / 1.35   # head + ramped tail
    assert f"between(t,0,{expected_eff:.3f})" in filter_complex, filter_complex
    assert f"trim=duration={expected_eff:.3f}" in filter_complex


def test_hook_join_no_ramp_uses_original_duration():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        speed_ramp=False,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "between(t,0,3.000)" in filter_complex


def test_hook_join_flash_applied_to_both_hook_and_content():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        cut_style="flash", cut_accent_duration=0.1,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[hookpov]fade=t=out" in filter_complex
    assert "[contv]fade=t=in" in filter_complex
    # the flashed labels, not the raw ones, must feed the transition/concat
    assert "[hookpov_fl][contv_fl]concat" in filter_complex


def test_hook_join_glitch_applied_to_both_hook_and_content():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        cut_style="glitch", cut_accent_duration=0.12,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[hookpov]rgbashift" in filter_complex
    assert "[contv]rgbashift" in filter_complex
    assert "[hookpov_gl][contv_gl]concat" in filter_complex


def test_hook_join_cut_style_none_uses_raw_labels():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        cut_style="none", cut_accent_duration=0.0,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "fade=t=out" not in filter_complex
    assert "rgbashift" not in filter_complex
    assert "[hookpov][contv]concat" in filter_complex


def test_hook_join_prop_wipe_inserts_third_video_segment():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="prop_wipe", t_dur=0.2, output_path="out.mp4",
        cut_style="flash", cut_accent_duration=0.1,   # must be ignored for prop_wipe
    )
    from modules.transitions import PROP_WIPE_ASSET
    assert str(PROP_WIPE_ASSET) in cmd
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "[hookpov][wipev][contv]concat=n=3:v=1:a=0[vout]" in filter_complex
    # cut_style must be skipped -- prop_wipe IS the visual accent, no double effect
    assert "fade=t=out" not in filter_complex
    assert "rgbashift" not in filter_complex


def test_hook_join_prop_wipe_asset_not_added_for_other_transitions():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
    )
    from modules.transitions import PROP_WIPE_ASSET
    assert str(PROP_WIPE_ASSET) not in cmd


def test_hook_join_impact_hit_added_and_positioned():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        impact_path="impact.wav", impact_duration=0.18,
    )
    assert "impact.wav" in cmd
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2:duration=first" in filter_complex
    # impact starts exactly AT the cut (loud immediately), not before it like
    # the old whoosh's fade-in -- so it's positioned via adelay to hook_dur,
    # no separate fade-out math on the hook's own audio.
    assert "adelay=3000|3000[impact_pos]" in filter_complex


def test_hook_join_impact_uses_effective_duration_with_ramp():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        speed_ramp=True, ramp_duration=0.5, ramp_factor=1.35,
        impact_path="impact.wav", impact_duration=0.18,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    eff = 2.5 + 0.5 / 1.35
    assert f"adelay={int(eff*1000)}|{int(eff*1000)}[impact_pos]" in filter_complex, filter_complex


def test_hook_join_no_impact_skips_mixing():
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
        impact_path=None,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "amix" not in filter_complex


def test_hook_join_no_music_pads_audio_to_full_length():
    # Bug: without music, [aout] only covered the hook window (~1-6s) while
    # [vout] covers hook+content (often 15-20s+). -shortest then clamped the
    # WHOLE output to the short audio, silently discarding all the content --
    # found by forcing a real no-music render. apad must extend [aout] with
    # silence so -shortest is governed by the (longer) video instead.
    cmd = assembler._hook_join_cmd(
        hook_path="hook.mp4", hook_dur=3.0, hook_has_audio=True,
        content_path="content.mp4", pov_png="pov.png",
        music_path=None, music_offset=0.0, music_bleed_in=0.0,
        transition="hard_cut", t_dur=0.2, output_path="out.mp4",
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "apad" in filter_complex
    assert "[hooka]anull[aout]" not in filter_complex   # the old (buggy) unpadded pattern specifically
    assert "-shortest" in cmd   # apad only works because -shortest still bounds the output


# ── assembler.py: fast-cut montage (post-hook content rhythm) ─────────────

def test_loop_clips_fast_cut_produces_enough_entries():
    # Bug: 3 real Drive clips (each several seconds long) already summed past
    # a 20s target on their own, so the old accumulation logic stopped at 3
    # entries -- one cut every ~7s, no rhythm. clip_dur makes every entry
    # count as a SHORT fixed length regardless of the source clip's real
    # length, so enough entries get generated for a fast-cut montage.
    clips = ["a.mp4", "b.mp4", "c.mp4"]
    result = assembler._loop_clips_to_duration(clips, target_duration=20.0, clip_dur=1.2)
    assert len(result) == math.ceil(20.0 / 1.2)   # 17 entries, not 3
    assert set(result) <= set(clips)


def test_loop_clips_fast_cut_cycles_with_repeats_when_pool_small():
    result = assembler._loop_clips_to_duration(["only.mp4"], target_duration=6.0, clip_dur=1.2)
    assert len(result) == 5
    assert all(c == "only.mp4" for c in result)


def test_loop_clips_no_clip_dur_keeps_original_behavior():
    # Plain (non-hook, non-beat-match) format3 path -- clips play their own
    # full native length, unchanged from before this feature existed.
    clips = ["a.mp4", "b.mp4"]
    result = assembler._loop_clips_to_duration(clips, target_duration=20.0, clip_dur=None)
    assert result == clips or len(result) >= 2   # falls through to ffprobe-based accumulation


def test_format1_cmd_clip_dur_trims_each_background_clip():
    cmd = assembler._format1_cmd(
        bg_clips=["a.mp4", "b.mp4", "c.mp4"], overlay_pngs=["p0.png"],
        seg_dur=4, total_dur=4, music_path=None, output_path="out.mp4",
        clip_dur=1.2,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert filter_complex.count("trim=duration=1.200") == 3   # one per bg clip, not just the final concat trim


def test_format1_cmd_no_clip_dur_only_trims_final_concat():
    cmd = assembler._format1_cmd(
        bg_clips=["a.mp4", "b.mp4"], overlay_pngs=["p0.png"],
        seg_dur=4, total_dur=8, music_path=None, output_path="out.mp4",
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert filter_complex.count("trim=duration=") == 1   # only the post-concat total_dur trim


def test_format1_cmd_loops_image_inputs_not_video_inputs():
    # Fast-cut montage mixes real Drive/Pexels images alongside videos --
    # images need -loop 1 (a plain image input is one frame, not something
    # `trim` can extend) but videos must NOT get -loop 1 (would make an
    # already-finite video stream loop forever instead of playing once).
    cmd = assembler._format1_cmd(
        bg_clips=["a.mp4", "photo.jpg", "b.mov", "shot.png"], overlay_pngs=["p0.png"],
        seg_dur=4, total_dur=4, music_path=None, output_path="out.mp4",
        clip_dur=1.0,
    )
    # exactly 2 "-loop","1" pairs -- one per image, none for the two videos.
    loop_positions = [i for i, tok in enumerate(cmd) if tok == "-loop"]
    assert len(loop_positions) == 2
    for i in loop_positions:
        assert cmd[i + 1] == "1"
        assert cmd[i + 2] == "-i"
        assert cmd[i + 3].lower().endswith((".jpg", ".png"))


# ── hooks.py: bank + dedup ──────────────────────────────────────────────

def test_list_hooks_finds_placeholders():
    def _run():
        hooks.generate_placeholder_hooks()
        found = hooks.list_hooks()
        ids = {h["id"] for h in found}
        assert {"ph_hook_dark", "ph_hook_myst", "ph_hook_epic"}.issubset(ids)
        for h in found:
            assert Path(h["_path"]).exists()
    _with_isolated_hooks_dir(_run)


def test_list_hooks_excludes_overlong_clips():
    def _run():
        import json
        hooks.HOOKS_DIR.mkdir(parents=True, exist_ok=True)
        long_manifest = hooks.HOOKS_DIR / "th_toolong.json"
        long_video = hooks.HOOKS_DIR / "th_toolong.mp4"
        long_video.write_bytes(b"fake")
        long_manifest.write_text(json.dumps({
            "id": "th_toolong", "filename": "th_toolong.mp4",
            "duration": 45.0, "cut_time": 45.0, "ambiance": "dark_tense",
        }), encoding="utf-8")
        found = hooks.list_hooks()
        assert "th_toolong" not in {h["id"] for h in found}
        # Raising max_duration should surface it again.
        found_wide = hooks.list_hooks(max_duration=60.0)
        assert "th_toolong" in {h["id"] for h in found_wide}
    _with_isolated_hooks_dir(_run)


def test_combo_hash_deterministic_and_sensitive():
    h1 = hooks._combo_hash("hook_a", "music/x.mp3", "Why The Rich Stay Calm")
    h2 = hooks._combo_hash("hook_a", "music/x.mp3", "why the rich stay calm")  # case-insensitive
    h3 = hooks._combo_hash("hook_b", "music/x.mp3", "why the rich stay calm")  # different hook
    assert h1 == h2
    assert h1 != h3


def test_dedup_roundtrip(tmp_log=None):
    # Isolate from the real state file.
    orig = hooks._COMBO_LOG
    hooks._COMBO_LOG = Path(__file__).parent / "_tmp_hook_combo_log.json"
    hooks._COMBO_LOG.unlink(missing_ok=True)
    try:
        assert not hooks.is_combo_recent("hookX", "musicY.mp3", "subject Z")
        hooks.record_combo("hookX", "musicY.mp3", "subject Z")
        assert hooks.is_combo_recent("hookX", "musicY.mp3", "subject Z")
        # Different subject -> different combo -> not recent
        assert not hooks.is_combo_recent("hookX", "musicY.mp3", "totally different subject")
    finally:
        hooks._COMBO_LOG.unlink(missing_ok=True)
        hooks._COMBO_LOG = orig


def test_dedup_respects_window_expiry():
    orig = hooks._COMBO_LOG
    hooks._COMBO_LOG = Path(__file__).parent / "_tmp_hook_combo_log2.json"
    try:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()
        h = hooks._combo_hash("hookOld", "m.mp3", "old subject")
        hooks._save_combo_log({h: old_ts})
        assert not hooks.is_combo_recent("hookOld", "m.mp3", "old subject")
    finally:
        hooks._COMBO_LOG.unlink(missing_ok=True)
        hooks._COMBO_LOG = orig


def test_generate_pov_text_uses_override():
    hook = {"id": "th_plate_toss", "title": "Plate Toss"}
    script = {"pov_text_override": "you finally see it", "hook": "unrelated financial hook line"}
    text = hooks.generate_pov_text(hook, script)
    assert text == "POV: you finally see it"


def test_generate_pov_text_override_already_prefixed():
    hook = {"id": "th_plate_toss", "title": "Plate Toss"}
    script = {"pov_text_override": "POV: already prefixed", "hook": "unrelated financial hook line"}
    text = hooks.generate_pov_text(hook, script)
    assert text == "POV: already prefixed"


def test_analyze_payoff_moment_on_real_hook():
    # Regression for a real bug: an earlier "-1s from the end" heuristic was
    # explicitly rejected by the user (not content-aware -- cut too early on
    # some clips, too late/showed the payoff on others). Real per-clip
    # analysis lands in the actual anticipation window, not at a fixed offset.
    src = Path(__file__).parent.parent / "assets" / "hooks" / "th_sign_smash.mp4"
    if not src.exists():
        return
    cut = hooks.analyze_payoff_moment(str(src), 1.9)
    assert 1.0 <= cut < 1.9, cut   # >= the 1.0s floor, strictly before the clip's raw end


def test_analyze_payoff_moment_floors_at_minimum_duration():
    # Short clips (<= _MIN_HOOK_DURATION) skip the vision call entirely --
    # nothing left to safely trim.
    cut = hooks.analyze_payoff_moment("nonexistent.mp4", 0.8)
    assert cut == 0.8


def test_ensure_payoff_analyzed_caches_into_manifest():
    def _run():
        import json as _json
        hooks.HOOKS_DIR.mkdir(parents=True, exist_ok=True)
        video_path = hooks.HOOKS_DIR / "th_cache_test.mp4"
        video_path.write_bytes(b"fake")
        manifest_path = hooks.HOOKS_DIR / "th_cache_test.json"
        hook = {"id": "th_cache_test", "filename": "th_cache_test.mp4",
                "duration": 3.0, "cut_time": 3.0, "_path": str(video_path)}
        manifest_path.write_text(_json.dumps(hook), encoding="utf-8")

        orig = hooks.analyze_payoff_moment
        hooks.analyze_payoff_moment = lambda path, dur, n_samples=6: 1.75
        try:
            result = hooks._ensure_payoff_analyzed(dict(hook))
        finally:
            hooks.analyze_payoff_moment = orig

        assert result["cut_time"] == 1.75
        assert result["payoff_analyzed"] is True
        saved = _json.loads(manifest_path.read_text(encoding="utf-8"))
        assert saved["cut_time"] == 1.75
        assert saved["payoff_analyzed"] is True
        assert "_path" not in saved   # internal runtime key, not persisted

        # Second call must NOT re-invoke analysis -- cached via payoff_analyzed.
        hooks.analyze_payoff_moment = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
        try:
            result2 = hooks._ensure_payoff_analyzed(result)
        finally:
            hooks.analyze_payoff_moment = orig
        assert result2["cut_time"] == 1.75
    _with_isolated_hooks_dir(_run)


def test_generate_pov_text_ignores_video_message_without_override():
    # Regression: POV must come from the HOOK CLIP's own title, never from the
    # video's financial message -- that was the exact bug reported ("POV: you
    # started 90 days ago and already won" on a plate-toss clip, completely
    # unrelated to the footage).
    import config as _config
    orig_key = _config.ANTHROPIC_API_KEY
    try:
        import modules.hooks as hooks_mod
        hooks_mod.ANTHROPIC_API_KEY = ""   # force the offline fallback path
        hook = {"id": "th_plate_toss", "title": "Plate Toss"}
        script = {"hook": "Rich is a number. Wealthy is a position.", "body": ["90 days changes everything"]}
        text = hooks.generate_pov_text(hook, script)
        assert "90 day" not in text.lower()
        assert "wealth" not in text.lower() and "rich" not in text.lower()
    finally:
        import modules.hooks as hooks_mod
        hooks_mod.ANTHROPIC_API_KEY = orig_key


# ── music_rotation.py ────────────────────────────────────────────────────

def test_iso_week_key_format():
    dt = datetime(2026, 7, 3, tzinfo=timezone.utc)
    key = music_rotation._iso_week_key(dt)
    assert key.startswith("2026-W")
    assert len(key.split("-W")[1]) == 2


def test_weekly_rotation_reuses_same_track():
    # _get_weekly_track re-picks if the persisted file no longer exists on disk
    # (defensive: survives a cleared cache) -- so the fake track must be a REAL
    # file for this test to exercise the "reuse" branch instead of that fallback.
    orig_file = music_rotation._WEEKLY_FILE
    music_rotation._WEEKLY_FILE = Path(__file__).parent / "_tmp_weekly_music.json"
    music_rotation._WEEKLY_FILE.unlink(missing_ok=True)
    real_track = Path(__file__).parent / "_tmp_weekly_track.mp3"
    real_track.write_bytes(b"fake mp3 bytes")

    calls = {"n": 0}

    def fake_select_phonk():
        calls["n"] += 1
        return str(real_track)

    orig_select = music_rotation._select_phonk_track
    music_rotation._select_phonk_track = fake_select_phonk
    try:
        p1 = music_rotation._get_weekly_track({"subject": "a"})
        p2 = music_rotation._get_weekly_track({"subject": "b"})
        assert p1 == p2, "same ISO week must reuse the same track"
        assert calls["n"] == 1, "phonk selection should only be called once for the week"
    finally:
        music_rotation._select_phonk_track = orig_select
        music_rotation._WEEKLY_FILE.unlink(missing_ok=True)
        music_rotation._WEEKLY_FILE = orig_file
        real_track.unlink(missing_ok=True)


def test_get_hook_music_dispatches_by_mode():
    import config
    orig_mode = config.HOOK_MUSIC_MODE

    orig_select = music_rotation._select_phonk_track
    music_rotation._select_phonk_track = lambda: "/fake/pool_track.mp3"
    try:
        config.HOOK_MUSIC_MODE = "pool"
        music_rotation.HOOK_MUSIC_MODE = "pool"
        p = music_rotation.get_hook_music({"subject": "x"})
        assert p == "/fake/pool_track.mp3"
    finally:
        music_rotation._select_phonk_track = orig_select
        config.HOOK_MUSIC_MODE = orig_mode
        music_rotation.HOOK_MUSIC_MODE = orig_mode


def test_select_phonk_track_avoids_recent_repeats():
    orig_dir = music_rotation._PHONK_DIR
    orig_used_file = music_rotation._PHONK_USED_FILE
    tmp_dir = Path(__file__).parent / "_tmp_phonk_pool"
    tmp_dir.mkdir(exist_ok=True)
    for i in range(3):
        (tmp_dir / f"track{i}.mp3").write_bytes(b"fake")
    music_rotation._PHONK_DIR = tmp_dir
    music_rotation._PHONK_USED_FILE = Path(__file__).parent / "_tmp_phonk_used.json"
    music_rotation._PHONK_USED_FILE.unlink(missing_ok=True)
    try:
        # pool has 3 tracks, exclude last 4 -- picking 3 times in a row must
        # never repeat immediately (each pick removes itself from "available"
        # until the whole pool has cycled once).
        picks = [music_rotation._select_phonk_track() for _ in range(3)]
        assert len(set(picks)) == 3, f"expected 3 distinct picks, got {picks}"
    finally:
        music_rotation._PHONK_DIR = orig_dir
        music_rotation._PHONK_USED_FILE.unlink(missing_ok=True)
        music_rotation._PHONK_USED_FILE = orig_used_file
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Runner ────────────────────────────────────────────────────────────────

def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
