"""
Brand factory -- one-click "new brand" automation (2026-08-12).

Turns the manual MindFile-from-ResetWorth playbook (see docs/MINDFILE_BRAND.md)
into a scriptable background pipeline: pick/refine a niche, scaffold the brand
(modules.users.create_user), write a "niche" config block that
modules/script_generator.py's and modules/content_meta.py's generic paths key
off (see their _active_niche_config / _niche_config_for_script), write a
companion ebook (manuscript + designed PDF), and drop in placeholder brand
art. Deliberately stops short of anything that needs a human decision outside
this process -- social account linking stays manual via the existing Zernio
UI, exactly like modules.users.create_user() already leaves profiles.yaml
with placeholder account_ids for any new brand.

Every new brand gets ResetWorth's full existing format lineup for free
(config.RESETWORTH_FORMATS) -- format1/3/6/9/10/11/... already work against
ANY brand's own config.json, no per-format code needed (see
modules/users.py's UserContext.formats). This module only adds what makes
that content sound like the new brand instead of ResetWorth: a niche
system-prompt, a hashtag/book-promo pool, and a companion book -- the same
three things ResetWorth/MindFile each got by hand.

Runs as a checkpointed background job (see run()), mirroring the batch
crash-resume pattern already used for video batches -- progress is written to
disk after every step so a crash/restart can be inspected. Unlike a video
batch, a partially-built brand is NOT auto-resumed: it's left in users/<name>/
for manual cleanup, or the dashboard can just be asked to try again under a
fresh name.
"""

import json
import logging
import random
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, CLAUDE_MODEL
from modules import users
from modules.claude_cli import call_claude

logger = logging.getLogger(__name__)

_JOBS_DIR = BASE_DIR / "state" / "brand_factory"

STEPS = ["research", "scaffold", "book_manuscript", "book_pdf", "brand_art", "done"]


# ── Job progress (disk-backed, polled by the dashboard) ────────────────────

def _job_file(job_id: str) -> Path:
    return _JOBS_DIR / f"{job_id}.json"


def _write_progress(job_id: str, **fields) -> None:
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    f = _job_file(job_id)
    data = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["job_id"] = job_id   # so list_jobs()/the dashboard can identify + resume-poll a job
    data.update(fields)
    data["updated_at"] = datetime.now().isoformat()
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_status(job_id: str) -> dict | None:
    f = _job_file(job_id)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_jobs() -> list[dict]:
    if not _JOBS_DIR.exists():
        return []
    out = []
    for f in sorted(_JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def active_job() -> dict | None:
    """The most recent job still in flight (queued/running), if any -- used to
    block a second concurrent launch (see app.py's /api/brand_factory/start).
    Found live 2026-08-12: nothing stopped two clicks/retries from firing two
    full research+book Claude runs in parallel for no benefit, since the job
    survives independently of whatever triggered it."""
    for j in list_jobs():
        if j.get("status") in ("queued", "running"):
            return j
    return None


# ── Step 1: research (text-only Claude reasoning, no live web search --
#    same caveat as every other Claude-authored content in this pipeline) ──

_RESEARCH_SYSTEM_PROMPT = """You are a short-form content strategist picking a brand-new niche for a
faceless, fully-automated TikTok/Shorts/Reels + YouTube channel, exactly like a strategist would when
launching a new channel from scratch. Weigh RPM (ad revenue per 1000 views), how saturated/beatable the
competition is, and how broad the demographic reach is (broad all-ages content beats a narrow
subculture skew, unless the user's hint clearly asks for a narrow one). Never pick anything needing
legal caution (true crime, medical/health claims, financial advice requiring disclaimers) unless the
hint explicitly asks for it. Ground every claim in real, genuine facts/concepts -- never invent a fake
study or statistic. Output English only."""


def research_niche(hint: str = "") -> dict:
    """Step 1 -- pick (or refine a user-supplied hint into) a niche, brand
    name, voice, hashtag pool, and companion-book concept."""
    hint_line = (
        f'The user\'s own hint/direction: "{hint.strip()}"'
        if hint.strip() else
        "No hint given -- pick a fresh niche yourself, genuinely different from "
        "money/wealth (the ResetWorth brand) and applied psychology/human behavior "
        "(the MindFile brand), the two niches this system already has."
    )
    prompt = f"""{hint_line}

Design a brand-new faceless short-form content brand. Return ONLY valid JSON with this exact shape:

{{
  "brand_name": "<one word or short compound, lowercase, no spaces, e.g. 'stoicmind'>",
  "niche_label": "<2-6 word niche label, e.g. 'Stoic Philosophy & Modern Discipline'>",
  "why": "<2-4 sentences: why this niche, vs. what alternative, citing the RPM/competition/reach tradeoff>",
  "voice": "<4-6 sentences of concrete brand-voice rules for a script writer: tone, what's grounded/real vs. banned (e.g. never invent studies), phrasing to avoid, how hooks should open>",
  "hashtag_pool": ["<20-30 real, relevant hashtags, lowercase, each starting with #>"],
  "stable_tags": ["<2 short always-included tags, one can be the brand name itself>"],
  "cta_words": ["<4-6 short ALL-CAPS single words for a 'Comment WORD' engagement hook, on-theme>"],
  "palette": ["<3 hex colors, dark/moody works best for text legibility, e.g. '#12141a'>"],
  "book_title": "<a short, sellable ebook title in this niche, distinct from 'The 90-Day Advisor' and 'The Sabotage Point'>",
  "book_angle": "<1-2 sentences: the book's specific angle/promise, narrow and different from generic competitors in this space>",
  "default_networks": ["tiktok", "youtube", "instagram"]
}}"""
    raw, _, _ = call_claude(_RESEARCH_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL, timeout=180)
    data = _parse_json(raw)
    slug = re.sub(r"[^a-z0-9_]", "", data.get("brand_name", "").strip().lower().replace(" ", "_"))
    data["brand_name"] = slug or f"brand{random.randint(1000, 9999)}"
    return data


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    return json.loads(fence.group(1).strip() if fence else raw)


def _unique_brand_name(base: str) -> str:
    """create_user() rejects an existing brand name outright -- rather than
    fail the whole job on a name collision, suffix it (mirrors how a human
    would just pick 'stoicmind2' by hand)."""
    existing = set(users.list_users())
    if base not in existing:
        return base
    for i in range(2, 50):
        candidate = f"{base}{i}"
        if candidate not in existing:
            return candidate
    return f"{base}{uuid.uuid4().hex[:6]}"


# ── Step 2: scaffold the brand + write its niche config ────────────────────

def scaffold_brand(concept: dict) -> "users.UserContext":
    name = _unique_brand_name(concept["brand_name"])
    ctx = users.create_user(
        name, generator=users.DEFAULT_USER,
        default_networks=concept.get("default_networks") or ["tiktok", "youtube", "instagram"],
    )
    cfg = dict(ctx.config)
    gumroad_env = f"GUMROAD_LINK_{name.upper()}"
    cfg["niche"] = {
        "label": concept.get("niche_label", ""),
        "why": concept.get("why", ""),
        "voice": concept.get("voice", ""),
        "hashtag_pool": concept.get("hashtag_pool", []),
        "stable_tags": concept.get("stable_tags", []),
        "cta_words": concept.get("cta_words", []),
        "palette": concept.get("palette", []),
        "book": {
            "title": concept.get("book_title", ""),
            "angle": concept.get("book_angle", ""),
            "gumroad_env": gumroad_env,
            "comment_suffix": "and I'll send you the book.",
        },
    }
    (ctx.root / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    (ctx.root / "book").mkdir(parents=True, exist_ok=True)
    (ctx.root / "output" / "brand").mkdir(parents=True, exist_ok=True)
    logger.info(f"brand_factory: scaffolded brand '{name}' (niche: {cfg['niche']['label']!r})")
    return users.get_user(name)  # re-read so ctx.config reflects the niche block just written


# ── Step 3: companion ebook -- manuscript, then a designed PDF ─────────────

_BOOK_SYSTEM_PROMPT = """You are a nonfiction ebook author writing a short, genuinely useful lead-magnet
book to accompany a short-form video brand. Real, concrete, actionable advice -- never invent a fake
study or statistic. No em dashes. English only."""


def write_book_manuscript(concept: dict) -> dict:
    """Returns {"title": str, "chapters": [{"heading": str, "body": str}]} --
    body is plain paragraphs (markdown-light: blank line = new paragraph,
    lines starting with '- ' = a bullet), parsed out of one Claude call."""
    title = concept.get("book_title", "Untitled")
    angle = concept.get("book_angle", "")
    niche = concept.get("niche_label", "")
    prompt = f"""Write a short ebook titled "{title}" for the "{niche}" niche.

Its angle: {angle}

Structure: an Introduction, then 4-5 chapters, then a short "Quick-Start Toolkit" closing chapter with
a numbered action checklist. Roughly 2500-3500 words total. Each chapter should have at least one
concrete example or scenario, not just abstract advice.

Return ONLY valid JSON with this exact shape:
{{
  "chapters": [
    {{"heading": "Introduction", "body": "<paragraphs, blank line between each, '- ' prefix for bullet lines>"}},
    {{"heading": "Chapter 1: ...", "body": "..."}},
    ...
    {{"heading": "Quick-Start Toolkit", "body": "..."}}
  ]
}}"""
    raw, _, _ = call_claude(_BOOK_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL, timeout=600)
    data = _parse_json(raw)
    return {"title": title, "chapters": data.get("chapters", [])}


def _book_html(manuscript: dict, palette: list[str]) -> str:
    bg     = (palette[0] if len(palette) > 0 else "#14161c")
    accent = (palette[1] if len(palette) > 1 else "#c9a45c")
    ink    = (palette[2] if len(palette) > 2 else "#eeece4")

    def _para(body: str) -> str:
        blocks = []
        for chunk in re.split(r"\n\s*\n", body.strip()):
            lines = [l.strip() for l in chunk.splitlines() if l.strip()]
            if not lines:
                continue
            if all(l.startswith("- ") for l in lines):
                items = "".join(f"<li>{_esc(l[2:])}</li>" for l in lines)
                blocks.append(f"<ul>{items}</ul>")
            else:
                blocks.append(f"<p>{_esc(' '.join(lines))}</p>")
        return "\n".join(blocks)

    chapters_html = []
    for ch in manuscript["chapters"]:
        chapters_html.append(
            f'<section class="chapter"><h2>{_esc(ch.get("heading", ""))}</h2>{_para(ch.get("body", ""))}</section>'
        )

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(manuscript['title'])}</title>
<style>
  @page {{ margin: 0; }}
  * {{ box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  body {{ margin: 0; background: {bg}; color: {ink}; font-family: Georgia, 'Times New Roman', serif; }}
  .cover {{
    width: 6in; height: 9in; padding: 1in 0.75in; background: {bg}; color: {ink};
    display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center;
    page-break-after: always;
  }}
  .cover .kicker {{ font-family: Arial, sans-serif; letter-spacing: 0.3em; font-size: 11px; color: {accent}; margin-bottom: 24px; text-transform: uppercase; }}
  .cover h1 {{ font-size: 40px; line-height: 1.15; margin: 0 0 20px; }}
  .cover .rule {{ width: 60px; height: 2px; background: {accent}; margin: 20px 0; }}
  .chapter {{ width: 6in; min-height: 9in; padding: 0.9in 0.8in; page-break-after: always; }}
  .chapter h2 {{ font-family: Arial, sans-serif; color: {accent}; font-size: 22px; letter-spacing: 0.02em; margin: 0 0 24px; border-bottom: 1px solid {accent}55; padding-bottom: 12px; }}
  .chapter p {{ font-size: 13.5px; line-height: 1.7; margin: 0 0 14px; }}
  .chapter ul {{ margin: 0 0 14px; padding-left: 22px; }}
  .chapter li {{ font-size: 13.5px; line-height: 1.7; margin-bottom: 6px; }}
</style></head><body>
<div class="cover"><div class="kicker">A companion guide</div><h1>{_esc(manuscript['title'])}</h1><div class="rule"></div></div>
{''.join(chapters_html)}
</body></html>"""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_book_pdf(manuscript: dict, palette: list[str], out_path: Path) -> Path:
    """HTML -> PDF via headless Chrome (Playwright). Two print-CSS bugs bit
    MindFile's own book (see docs/MINDFILE_BRAND.md section 7) and are
    avoided here directly: Chrome strips print backgrounds unless told
    otherwise (print_background=True below), and @page margins never paint
    the document background regardless of color-adjust rules (fixed by
    zeroing the @page margin in CSS + real padding on each page div instead,
    and passing margin=0 to page.pdf() too)."""
    from playwright.sync_api import sync_playwright

    html = _book_html(manuscript, palette)
    html_path = out_path.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.pdf(
            path=str(out_path), width="6in", height="9in",
            print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()
    return out_path


# ── Step 4: placeholder brand art (pp + banner) ─────────────────────────────

def render_brand_art(brand_name: str, niche_label: str, palette: list[str], out_dir: Path) -> dict:
    """Simple, deterministic placeholder art (gradient + brand initial/name) --
    explicitly NOT trying to be final professional artwork, just something
    non-blank the user can see immediately and swap out later. No external
    image-gen API is wired into this background pipeline."""
    from PIL import Image, ImageDraw, ImageFont

    bg     = _hex(palette[0] if len(palette) > 0 else "#14161c")
    accent = _hex(palette[1] if len(palette) > 1 else "#c9a45c")
    ink    = _hex(palette[2] if len(palette) > 2 else "#eeece4")
    out_dir.mkdir(parents=True, exist_ok=True)

    def _font(size: int):
        for candidate in ("arialbd.ttf", "Arial Bold.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _gradient(w: int, h: int) -> Image.Image:
        img = Image.new("RGB", (w, h), bg)
        top, bot = bg, tuple(max(0, c - 20) for c in bg)
        for y in range(h):
            t = y / max(1, h - 1)
            row = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
            ImageDraw.Draw(img).line([(0, y), (w, y)], fill=row)
        return img

    def _centered_text(img: Image.Image, text: str, font, fill):
        d = ImageDraw.Draw(img)
        bbox = d.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((img.width - w) / 2 - bbox[0], (img.height - h) / 2 - bbox[1]), text, font=font, fill=fill)

    # Profile picture: brand initial in a circle
    pp = _gradient(1024, 1024)
    d = ImageDraw.Draw(pp)
    pad = 60
    d.ellipse([pad, pad, 1024 - pad, 1024 - pad], outline=accent, width=10)
    _centered_text(pp, brand_name[:1].upper(), _font(420), accent)
    pp_path = out_dir / f"pp_{brand_name}.jpg"
    pp.convert("RGB").save(pp_path, quality=92)

    # Banner: brand name + niche label
    banner = _gradient(2560, 1440)
    d = ImageDraw.Draw(banner)
    name_font, label_font = _font(140), _font(46)
    _centered_text(banner, brand_name.upper(), name_font, ink)
    label_img_y = 1440 // 2 + 110
    bbox = d.textbbox((0, 0), niche_label, font=label_font)
    w = bbox[2] - bbox[0]
    d.text(((2560 - w) / 2, label_img_y), niche_label, font=label_font, fill=accent)
    banner_path = out_dir / f"banniere_{brand_name}.jpg"
    banner.convert("RGB").save(banner_path, quality=92)

    return {"pp": str(pp_path), "banner": str(banner_path)}


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) != 6:
        return (20, 22, 28)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ── Orchestrator ─────────────────────────────────────────────────────────

def run(job_id: str, hint: str = "") -> None:
    """Runs the whole pipeline synchronously in the calling thread -- app.py
    wraps this in a background threading.Thread (see run_async). Every step
    checkpoints to disk via _write_progress so the dashboard can poll status
    mid-run, and a step's own failure is recorded without losing whatever
    already succeeded (a brand that's scaffolded but has no book yet is
    still a usable brand -- see the module docstring)."""
    _write_progress(job_id, step="research", status="running", hint=hint, error=None)
    try:
        concept = research_niche(hint)
    except Exception as e:
        logger.exception("brand_factory: research step failed")
        _write_progress(job_id, step="research", status="failed", error=str(e))
        return
    _write_progress(job_id, step="research", status="done", concept=concept)

    _write_progress(job_id, step="scaffold", status="running")
    try:
        ctx = scaffold_brand(concept)
    except Exception as e:
        logger.exception("brand_factory: scaffold step failed")
        _write_progress(job_id, step="scaffold", status="failed", error=str(e))
        return
    # Auto-activate the new brand immediately, same as the manual "+ Marque"
    # flow (app.py's api_create_user) already does -- otherwise a user who
    # clicks the button has to remember to switch to it by hand afterward,
    # inconsistent with the flow it's meant to replace.
    try:
        users.set_active_user(ctx.name)
    except Exception as e:
        logger.warning(f"brand_factory: couldn't auto-activate '{ctx.name}' ({e})")
    _write_progress(job_id, step="scaffold", status="done", brand=ctx.name)

    palette = concept.get("palette") or []

    _write_progress(job_id, step="book_manuscript", status="running")
    manuscript = None
    try:
        manuscript = write_book_manuscript(concept)
    except Exception as e:
        logger.warning(f"brand_factory: book manuscript step failed, continuing without a book ({e})")
        _write_progress(job_id, step="book_manuscript", status="failed", error=str(e))
    else:
        _write_progress(job_id, step="book_manuscript", status="done")

    if manuscript and manuscript.get("chapters"):
        _write_progress(job_id, step="book_pdf", status="running")
        try:
            pdf_path = render_book_pdf(
                manuscript, palette,
                ctx.root / "book" / f"{manuscript['title'].replace(' ', '_')}.pdf",
            )
            _write_progress(job_id, step="book_pdf", status="done", book_pdf=str(pdf_path))
        except Exception as e:
            logger.warning(f"brand_factory: book PDF render failed ({e})")
            _write_progress(job_id, step="book_pdf", status="failed", error=str(e))
    else:
        _write_progress(job_id, step="book_pdf", status="skipped")

    _write_progress(job_id, step="brand_art", status="running")
    try:
        art = render_brand_art(ctx.name, concept.get("niche_label", ""), palette, ctx.root / "output" / "brand")
        _write_progress(job_id, step="brand_art", status="done", **art)
    except Exception as e:
        logger.warning(f"brand_factory: brand art render failed ({e})")
        _write_progress(job_id, step="brand_art", status="failed", error=str(e))

    _write_progress(
        job_id, step="done", status="done", brand=ctx.name,
        next_step=(
            f"Brand '{ctx.name}' is ready to generate videos with every existing format. "
            f"Connect its TikTok/YouTube/Instagram accounts in the Zernio panel, fill account_id in "
            f"users/{ctx.name}/profiles.yaml, and (optionally) list the book on Gumroad and set "
            f"GUMROAD_LINK_{ctx.name.upper()} in .env to start promoting it."
        ),
    )


def run_async(hint: str = "") -> str:
    """Kicks off run() in a background thread, returns the job_id immediately."""
    job_id = uuid.uuid4().hex[:12]
    _write_progress(job_id, step="research", status="queued", hint=hint, error=None)
    t = threading.Thread(target=run, args=(job_id, hint), daemon=True)
    t.start()
    return job_id
