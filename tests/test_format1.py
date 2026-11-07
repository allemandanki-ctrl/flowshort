"""
Tests for the format1 "next level" rebuild: clip_silence.py, clip_transcriber's
find_climax_segment, clip_broll.py. Pure-logic sanity checks (no real ffmpeg/API
calls) -- the real ffmpeg filter graphs were verified manually against real
clips during development (see project memory).
Run: python tests/test_format1.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.clips import clip_silence, clip_transcriber, clip_broll


# ── clip_silence.py ─────────────────────────────────────────────────────

def test_remove_silences_noop_on_too_few_words():
    words = [{"word": "hi", "start": 0.0, "end": 0.3}]
    clip, new_words, dur = clip_silence.remove_silences(Path("x.mp4"), words, 0.3)
    assert clip == Path("x.mp4")
    assert new_words == words
    assert dur == 0.3


def test_remove_silences_noop_when_no_gaps_exceed_threshold():
    # Consecutive words with tiny gaps -- nothing worth cutting.
    words = [
        {"word": "a", "start": 0.0, "end": 0.3},
        {"word": "b", "start": 0.35, "end": 0.6},
        {"word": "c", "start": 0.65, "end": 0.9},
    ]
    clip, new_words, dur = clip_silence.remove_silences(Path("x.mp4"), words, 1.0)
    assert clip == Path("x.mp4")
    assert new_words == words
    assert dur == 1.0


def test_remove_silences_skips_when_too_many_gaps():
    # Pathological transcript: gap before every single word -- treat as noisy
    # rather than trying to cut 40+ times.
    words = [{"word": f"w{i}", "start": i * 2.0, "end": i * 2.0 + 0.2} for i in range(50)]
    clip, new_words, dur = clip_silence.remove_silences(Path("x.mp4"), words, 100.0)
    assert clip == Path("x.mp4")   # unchanged -- too many gaps to trust


# ── clip_transcriber.find_climax_segment ────────────────────────────────

def test_find_climax_segment_noop_on_short_clip():
    words = [{"word": f"w{i}", "start": i * 0.3, "end": i * 0.3 + 0.25} for i in range(30)]
    start, end = clip_transcriber.find_climax_segment(words, 8.0)   # under _CLIMAX_MIN_DUR
    assert start == 0.0
    assert end == 8.0


def test_find_climax_segment_noop_on_too_few_words():
    words = [{"word": "hi", "start": 0.0, "end": 0.3}]
    start, end = clip_transcriber.find_climax_segment(words, 60.0)
    assert start == 0.0
    assert end == 60.0


def test_find_climax_segment_falls_back_on_cli_failure():
    # find_climax_segment now goes through modules.claude_cli.call_claude
    # (subscription-authenticated CLI) instead of gating on
    # ANTHROPIC_API_KEY -- exercise the fallback via a CLI failure.
    from unittest.mock import patch
    from modules.claude_cli import ClaudeCliError
    words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(30)]
    with patch("modules.claude_cli.call_claude", side_effect=ClaudeCliError("boom")):
        start, end = clip_transcriber.find_climax_segment(words, 20.0)
    assert start == 0.0
    assert end == 20.0


# ── clip_broll.py ────────────────────────────────────────────────────────

def test_extract_broll_keywords_falls_back_on_cli_failure():
    # extract_broll_keywords now goes through modules.claude_cli.call_claude
    # (the Claude Code CLI, subscription-authenticated) instead of gating on
    # ANTHROPIC_API_KEY -- the fallback path is exercised by a CLI failure,
    # not a missing key.
    from unittest.mock import patch
    from modules.claude_cli import ClaudeCliError
    with patch("modules.claude_cli.call_claude", side_effect=ClaudeCliError("boom")):
        kw = clip_broll.extract_broll_keywords("some speech text here")
    assert kw == clip_broll._FALLBACK_KEYWORDS


def test_extract_broll_keywords_falls_back_on_empty_text():
    kw = clip_broll.extract_broll_keywords("")
    assert kw == clip_broll._FALLBACK_KEYWORDS


def test_build_rounded_mask_png_produces_transparent_center_opaque_corners():
    import tempfile
    from PIL import Image

    out = Path(tempfile.gettempdir()) / "test_rounded_mask.png"
    clip_broll._build_rounded_mask_png(200, 100, out)
    img = Image.open(out)
    assert img.mode == "RGBA"
    assert img.size == (200, 100)
    # corner (well inside the radius) must be opaque black
    assert img.getpixel((2, 2))[3] == 255
    # center must be fully transparent (the "hole")
    assert img.getpixel((100, 50))[3] == 0
    out.unlink(missing_ok=True)


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
