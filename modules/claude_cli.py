"""
Routes LLM calls through the local `claude` CLI (subscription-authenticated
via `/login`) instead of the metered Anthropic API -- the project's own
ANTHROPIC_API_KEY ran out of credits, and the user wants pipeline LLM calls
to ride on their Claude Pro subscription instead of separate API billing.

Note: a Claude subscription's usage is a shared pool with any interactive
Claude Code sessions on this machine -- running many videos back-to-back
pulls from the same budget, not a separate one.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class ClaudeCliError(RuntimeError):
    pass


# Matches an `@path/to/frame.jpg` image attachment reference (the CLI's own
# vision syntax, used throughout the pipeline's frame-analysis calls -- see
# e.g. viral_remix._analyze_source_clip, white_card_reaction, upload_meta).
# Deliberately does NOT match @file.mp4/@file.py/etc -- only the raster
# formats actually used as vision attachments here, so plain text/code calls
# that happen to contain an unrelated "@something" never trip the grounding
# check below.
_IMAGE_ATTACHMENT_RE = re.compile(r"@\S+\.(?:jpe?g|png|webp)\b", re.IGNORECASE)


def _resolve_claude_bin() -> str:
    """On Windows, `claude` on PATH resolves to `claude.cmd` (an npm shim),
    and subprocess.run(["claude", ...]) without shell=True doesn't reliably
    find it -- CreateProcess needs the exact executable, it doesn't apply
    PATHEXT resolution the way a shell does. shutil.which() does that
    resolution correctly and returns the real, full path."""
    resolved = shutil.which("claude")
    if not resolved:
        raise ClaudeCliError("`claude` CLI not found on PATH -- is Claude Code installed?")
    return resolved


def call_claude(system_prompt: str, user_prompt: str, model: str, timeout: int = 180,
                 retries: int = 2) -> tuple[str, int, int]:
    """Invoke `claude -p` non-interactively, return (text, input_tokens,
    output_tokens). Retries transient failures (empty-stderr "exited 1" bursts
    seen when many calls fire back-to-back and collide with a shared-account
    rate limit -- see the module docstring) with a short backoff before
    giving up. Does not retry a missing CLI binary -- that won't fix itself."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            text, input_tokens, output_tokens, num_turns = _call_claude_once(
                system_prompt, user_prompt, model, timeout)
            return _maybe_reground_vision_call(
                system_prompt, user_prompt, model, timeout, text, input_tokens, output_tokens, num_turns)
        except ClaudeCliError as e:
            last_err = e
            if "not found on PATH" in str(e) or attempt == retries:
                raise
            wait = 4 * (attempt + 1)
            logger.warning(f"claude_cli: attempt {attempt + 1}/{retries + 1} failed ({e}), "
                           f"retrying in {wait}s")
            time.sleep(wait)
    raise last_err  # unreachable, keeps type-checkers happy


def _maybe_reground_vision_call(
    system_prompt: str, user_prompt: str, model: str, timeout: int,
    text: str, input_tokens: int, output_tokens: int, num_turns: int,
) -> tuple[str, int, int]:
    """
    2026-08-14, real bug caught live: an `@frame.jpg`-attached vision call
    doesn't always actually LOOK at the attachment before answering -- the
    CLI sometimes responds in a single turn (num_turns == 1) with a
    plausible-sounding but ungrounded guess (confirmed by hand: the exact
    same prompt against the exact same frame, run twice back-to-back,
    answered once with a generic non-answer unrelated to the real frame
    content and once -- num_turns == 2 that time -- with an accurate,
    specific description; the num_turns==1 run's tiny cache_creation_input_
    tokens confirmed the image was never actually loaded into context). This
    silently degraded every vision-dependent judgment call in the pipeline
    (archetype/caption/has_onscreen_text detection, crop checks, watermark
    localization, etc), not just one caller -- fixed centrally here so every
    caller benefits without touching call sites.

    Only fires for prompts carrying an image attachment (_IMAGE_ATTACHMENT_RE)
    AND only when num_turns <= 1 (the observed signature of a skipped read).
    Retries exactly ONCE with an explicit "you must actually open this" nudge
    prepended -- not a loop (this project's own hard rule: don't burn shared
    CLI subscription quota retrying past the first sign something's off, see
    project memory). Keeps the retry's result only if IT looked more grounded
    (num_turns increased) -- if the retry also skipped the read, silently
    keeping a second guess wouldn't be any more trustworthy than the first,
    so the original answer is kept and only a warning is logged.
    """
    if num_turns > 1 or not _IMAGE_ATTACHMENT_RE.search(user_prompt):
        return text, input_tokens, output_tokens

    logger.warning("claude_cli: vision call answered in a single turn (num_turns=1) -- likely "
                   "never actually opened the attached image, retrying once with an explicit nudge")
    reinforced_prompt = (
        "IMPORTANT: before answering, you MUST actually open and look at each @-referenced image "
        "file below -- do not guess from filenames, folder names, or general familiarity with this "
        "project/codebase.\n\n" + user_prompt
    )
    try:
        text2, input_tokens2, output_tokens2, num_turns2 = _call_claude_once(
            system_prompt, reinforced_prompt, model, timeout)
    except ClaudeCliError as e:
        logger.warning(f"claude_cli: vision-grounding retry failed ({e}), keeping the first (possibly "
                        f"ungrounded) result")
        return text, input_tokens, output_tokens

    if num_turns2 > 1:
        logger.info(f"claude_cli: retry actually opened the image (num_turns 1 -> {num_turns2}), "
                    f"using the retry's result")
        return text2, input_tokens2, output_tokens2
    logger.warning("claude_cli: retry still answered in a single turn -- keeping the first result, "
                   "both may be ungrounded (not retrying further, see docstring)")
    return text, input_tokens, output_tokens


def _call_claude_once(system_prompt: str, user_prompt: str, model: str, timeout: int) -> tuple[str, int, int, int]:
    """Explicitly strips ANTHROPIC_API_KEY from the child process's
    environment -- config.py's load_dotenv() puts the project's own
    (separately-billed) API key into os.environ, and the claude CLI would
    otherwise prefer that key over the logged-in subscription session,
    silently defeating the whole point of this module.
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    # The user prompt goes over STDIN, not as a CLI argument -- a long
    # prompt (JSON braces, escaped quotes, Windows backslash paths from
    # @file references) passed as a positional argument gets silently
    # mangled by subprocess's Windows command-line reconstruction
    # (list2cmdline's backslash/quote escaping), which truncated/corrupted
    # real prompts and made the CLI respond as if no request had been
    # attached at all. A manual `claude -p "<same text>"` typed directly in
    # a shell doesn't go through that reconstruction and worked fine --
    # stdin sidesteps the whole problem since there's no argv escaping
    # involved.
    #
    # The SYSTEM prompt has the exact same problem -- script_generator.py's
    # system prompt alone is several KB of brand-voice instructions with
    # its own quotes/dashes/parens, which timed out the CLI (180s) the
    # first time this ran end-to-end, almost certainly the same argv
    # mangling corrupting the command line badly enough to hang rather
    # than cleanly fail. --system-prompt-file (path, not raw text) sidesteps
    # it the same way stdin does for the user prompt.
    sys_prompt_file = None
    cmd = [_resolve_claude_bin(), "-p", "--model", model, "--output-format", "json"]
    if system_prompt:
        # An EMPTY --system-prompt "" is not equivalent to omitting the
        # flag -- passing it blank made the CLI treat plain classification/
        # analysis prompts as ambiguous coding requests and ask for
        # clarification instead of just answering (a real, reproduced bug).
        fd, sys_prompt_file = tempfile.mkstemp(suffix=".txt", prefix="claude_sysprompt_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system_prompt)
        cmd += ["--system-prompt-file", sys_prompt_file]
    # subprocess.run's own `timeout` only kills the direct child on Windows --
    # `claude.cmd` is an npm shim that execs a `node` grandchild, and if that
    # node process survives the parent's timeout, it keeps the stdout pipe
    # open, so communicate() hangs forever past its stated timeout (seen
    # live: a real end-to-end test sat well past 180s with a leftover
    # claude.exe/node process still holding ~0% CPU). taskkill /T kills the
    # whole process tree instead of just the wrapper PID -- same fix already
    # used for yt-dlp/ffmpeg in character_clip.py's _run_yt_dlp.
    #
    # encoding="utf-8" is required, not optional -- text=True alone falls back to
    # locale.getpreferredencoding(), which on this Windows machine is cp1252. Any
    # prompt containing an emoji or other non-cp1252 character (e.g. format17's "👇")
    # crashed the stdin write with UnicodeEncodeError before this was explicit.
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env,
        )
    except FileNotFoundError as e:
        if sys_prompt_file:
            Path(sys_prompt_file).unlink(missing_ok=True)
        raise ClaudeCliError("`claude` CLI not found on PATH -- is Claude Code installed?") from e

    try:
        stdout, stderr = proc.communicate(input=user_prompt, timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise ClaudeCliError(f"claude CLI timed out after {timeout}s")
    finally:
        if sys_prompt_file:
            Path(sys_prompt_file).unlink(missing_ok=True)

    if returncode != 0:
        raise ClaudeCliError(f"claude CLI exited {returncode}: {stderr.strip()[:500]}")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"claude CLI returned non-JSON output: {stdout[:500]!r}") from e

    if payload.get("is_error"):
        raise ClaudeCliError(f"claude CLI reported an error: {payload.get('result')!r}")

    text = payload.get("result", "")
    usage = payload.get("usage") or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    num_turns = payload.get("num_turns", 1)
    return text, input_tokens, output_tokens, num_turns
