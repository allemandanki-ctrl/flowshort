"""
Procedurally-synthesized sound effects for format15's arena hits -- pure
ffmpeg DSP (sine/noise + envelope), not sourced audio, so there's no
network dependency/licensing question and every render gets a consistent,
reliable result.
"""

from pathlib import Path

from modules.assembler import _run_ffmpeg


def make_impact_sound(out_path: Path) -> None:
    """Layered arcade-style hit: low bass thump + a sharp, compressed noise
    crack + a fast descending pitch snap -- deeper bass and a harder-
    compressed crack than the first version, for a punchier hit that reads
    closer to a real fighting-game impact."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=55:duration=0.5",
        "-f", "lavfi", "-i", "anoisesrc=d=0.1:c=white",
        "-f", "lavfi", "-i", "aevalsrc=0.6*sin(2*PI*(320-270*t/0.15)*t):d=0.15",
        "-filter_complex",
        "[0:a]afade=t=out:st=0.05:d=0.45,volume=11dB[bass];"
        "[1:a]highpass=f=1800,acompressor=threshold=-12dB:ratio=8:attack=1:release=30,"
        "afade=t=out:st=0.01:d=0.09,volume=13dB[crack];"
        "[2:a]afade=t=out:st=0.05:d=0.1,volume=8dB[snap];"
        "[bass][crack][snap]amix=inputs=3:duration=longest:normalize=0[out]",
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ])


def stylize_announcer_audio(in_path: Path, out_path: Path) -> None:
    """Post-process OUR OWN synthesized announcer line (TTS output, not a
    sampled recording) to sound less flat/robotic and more like a deep
    arcade-hype voice: pitch shifted down slightly (asetrate+atempo keeps
    the speed/duration the same while lowering pitch), compressed for
    punch, with a touch of echo for "arena" depth."""
    _run_ffmpeg([
        "ffmpeg", "-y", "-i", str(in_path),
        "-af",
        "asetrate=24000*0.90,aresample=24000,atempo=1.1111,"
        "acompressor=threshold=-16dB:ratio=4:attack=5:release=60,"
        "aecho=0.8:0.85:35:0.22,volume=4dB",
        "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ])


def make_whoosh_sound(out_path: Path) -> None:
    """Short filtered noise burst -- a missed/blocked swing."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anoisesrc=d=0.3:c=pink",
        "-af", "highpass=f=800,afade=t=out:st=0.05:d=0.25,volume=3dB",
        "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ])


def make_crowd_reaction_sound(out_path: Path) -> None:
    """Stylized UFC/arcade-crowd "OOOH!" swell -- three noise layers
    band-limited to different vocal-ish ranges, mixed and enveloped. No
    free keyless real-crowd-audio source is wired up (Freesound needs an
    API key the user doesn't have yet -- see BESOIN_TON_AVIS.md-style
    tradeoff notes), so this is a synthesized stand-in, mixed under the
    impact sound on every LANDED hit."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anoisesrc=d=1.0:c=pink",
        "-f", "lavfi", "-i", "anoisesrc=d=1.0:c=brown",
        "-f", "lavfi", "-i", "anoisesrc=d=1.0:c=white",
        "-filter_complex",
        "[0:a]bandpass=f=500:width_type=h:w=400,volume=5dB[l1];"
        "[1:a]bandpass=f=900:width_type=h:w=500,volume=5dB[l2];"
        "[2:a]bandpass=f=1600:width_type=h:w=700,volume=3dB[l3];"
        "[l1][l2][l3]amix=inputs=3:duration=longest:normalize=0[mixed];"
        "[mixed]afade=t=in:st=0:d=0.15,afade=t=out:st=0.6:d=0.4,volume=10dB[out]",
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ])


def make_crowd_boo_sound(out_path: Path) -> None:
    """Disappointed crowd groan/boo -- lower, slower-decaying noise layers
    than the cheer, mixed under the whoosh sound on every MISSED/BLOCKED
    swing."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anoisesrc=d=1.1:c=brown",
        "-f", "lavfi", "-i", "anoisesrc=d=1.1:c=pink",
        "-filter_complex",
        "[0:a]bandpass=f=250:width_type=h:w=300,volume=6dB[l1];"
        "[1:a]bandpass=f=450:width_type=h:w=350,volume=4dB[l2];"
        "[l1][l2]amix=inputs=2:duration=longest:normalize=0[mixed];"
        "[mixed]afade=t=in:st=0:d=0.2,afade=t=out:st=0.7:d=0.4,volume=9dB[out]",
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ])


def mix_audio(paths: list[Path], out_path: Path, gain_db: float = 0.0) -> None:
    """Combine several short sound effects into one mixed clip (e.g. impact
    crack + crowd reaction playing together on a landed hit). gain_db lets
    the caller pull the whole mixed result down -- amix with normalize=0
    doesn't auto-level, so a real recorded hit sound mixed with the
    synthesized crowd cheer can end up uncomfortably loud/harsh."""
    cmd = ["ffmpeg", "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    inputs = "".join(f"[{i}:a]" for i in range(len(paths)))
    cmd += [
        "-filter_complex",
        f"{inputs}amix=inputs={len(paths)}:duration=longest:normalize=0,volume={gain_db}dB[out]",
        "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ]
    _run_ffmpeg(cmd)


def make_arena_music_bed(out_path: Path, loop_dur: float = 1.0) -> None:
    """Procedurally-synthesized, seamlessly-loopable background energy bed
    -- a pulsing bass throb plus a light higher pulse a fifth above, both
    period-locked to `loop_dur` (4 tremolo pulses/second, loop_dur=1.0s ->
    exactly 4 whole cycles per loop, so tiling it back-to-back via
    -stream_loop has no audible seam/click at the join).

    Real gap found 2026-08-26: assemble_format15 accepted a `music_path`
    argument and never used it anywhere at all -- the whole video ran on
    dead silence between lines/SFX, which read as low-energy/empty even
    when the actual content was fine. Mixed in heavily ducked under every
    segment's own audio (see duel._MUSIC_BED_GAIN_DB) -- texture, not a
    competing track."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=110:duration={loop_dur}",
        "-f", "lavfi", "-i", f"sine=frequency=165:duration={loop_dur}",
        "-filter_complex",
        "[0:a]tremolo=f=4:d=0.7,volume=8dB[bass];"
        "[1:a]tremolo=f=4:d=0.5,highpass=f=140,volume=4dB[pad];"
        "[bass][pad]amix=inputs=2:duration=longest:normalize=0[out]",
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ])


def make_ko_sound(out_path: Path) -> None:
    """Descending pitch sweep -- final blow / K.O. card."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "aevalsrc='0.6*sin(2*PI*(220-160*t/0.9)*t)':d=0.9",
        "-af", "afade=t=out:st=0.6:d=0.3,volume=6dB",
        "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
    ])
