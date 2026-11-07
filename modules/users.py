"""
Multi-brand ("user") support.

Each brand is an isolated workspace:
  - its own publish config     (profiles.yaml — Ayrshare/Zernio providers + platforms)
  - its own config / format    (generator id, default networks)
  - its own history/anti-dup   (scripts tagged with the brand name)

Layout:
  ResetWorth  → users/resetworth/  (the default brand -- same shape as any other)
  <other>     → users/<name>/  with config.json + profiles.yaml + state/

The active brand is stored in state/active_user.json (shared, cross-brand) and
chosen in the UI before any generation or scheduling.
"""

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from config import BASE_DIR, OUTPUT_DIR, RESETWORTH_DIR, RESETWORTH_FORMATS

logger = logging.getLogger(__name__)

DEFAULT_USER = "resetworth"
_USERS_DIR   = BASE_DIR / "users"
_ACTIVE_FILE = BASE_DIR / "state" / "active_user.json"
_SCRIPTS_DIR = OUTPUT_DIR / "scripts"   # shared; scripts carry a `_user` tag


@dataclass(frozen=True)
class UserContext:
    name: str
    is_default: bool
    root: Path
    profiles_file: Path
    generator: str
    default_networks: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    # Which format ids this brand can see/use anywhere in the dashboard (dropdown,
    # batch mode buttons, inline fields -- see app.py's index() route and
    # templates/index.html's {% if fid in formats %} guards). No "formats" key in
    # this brand's own config.json -> inherits RESETWORTH_FORMATS live (not a
    # frozen snapshot at brand-creation time -- see create_user()'s docstring).
    formats: list[str] = field(default_factory=lambda: list(RESETWORTH_FORMATS))


# ── Discovery ─────────────────────────────────────────────────────────────

def list_users() -> list[str]:
    users = [DEFAULT_USER]
    if _USERS_DIR.exists():
        for d in sorted(_USERS_DIR.iterdir()):
            if d.is_dir() and (d / "config.json").exists():
                users.append(d.name)
    return users


def get_user(name: str | None = None) -> UserContext:
    name = (name or active_user()).strip()
    if name == DEFAULT_USER:
        return _default_context()

    root = _USERS_DIR / name
    cfg_file = root / "config.json"
    if not cfg_file.exists():
        logger.warning(f"User '{name}' not found, falling back to {DEFAULT_USER}")
        return _default_context()

    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    return UserContext(
        name=name,
        is_default=False,
        root=root,
        profiles_file=root / "profiles.yaml",
        generator=cfg.get("generator", DEFAULT_USER),
        default_networks=list(cfg.get("default_networks", [])),
        config=cfg,
        formats=list(cfg.get("formats") or RESETWORTH_FORMATS),
    )


def _default_context() -> UserContext:
    return UserContext(
        name=DEFAULT_USER,
        is_default=True,
        root=RESETWORTH_DIR,
        profiles_file=RESETWORTH_DIR / "profiles.yaml",
        generator=DEFAULT_USER,
        # Snapchat dropped 2026-08-23 per user request (no longer posting
        # there) -- this list drives the batch-panel network checkboxes
        # (app.py's _batch_panel_config -> "networks"/"networkLabels"), so
        # removing it here removes the Snapchat button/logo everywhere too.
        default_networks=["instagram", "tiktok", "youtube", "facebook"],
        config={},
    )


# ── Active user ───────────────────────────────────────────────────────────

def active_user() -> str:
    if _ACTIVE_FILE.exists():
        try:
            return json.loads(_ACTIVE_FILE.read_text(encoding="utf-8")).get("active", DEFAULT_USER)
        except Exception:
            pass
    return DEFAULT_USER


def set_active_user(name: str) -> None:
    if name not in list_users():
        raise ValueError(f"Unknown user '{name}'")
    _ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_FILE.write_text(json.dumps({"active": name}), encoding="utf-8")
    logger.info(f"Active user -> {name}")


# ── Creation ──────────────────────────────────────────────────────────────

def create_user(name: str, generator: str = DEFAULT_USER,
                default_networks: list[str] | None = None) -> UserContext:
    """Scaffold users/<name>/ with config.json + starter profiles.yaml + dirs."""
    name = name.strip().lower().replace(" ", "_")
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("Brand name must be alphanumeric (underscores allowed)")
    if name == DEFAULT_USER:
        raise ValueError(f"'{DEFAULT_USER}' is the default brand and already exists")

    root = _USERS_DIR / name
    if (root / "config.json").exists():
        raise ValueError(f"Brand '{name}' already exists")

    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "output" / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "output" / "videos").mkdir(parents=True, exist_ok=True)

    networks = default_networks or ["instagram", "tiktok", "youtube"]
    cfg = {
        "generator": generator,
        "default_networks": networks,
    }
    (root / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    # Starter profiles.yaml — this brand needs its OWN Ayrshare/Zernio keys
    # (env var names below are placeholders scoped to the brand name; add the
    # matching values to .env, e.g. AYRSHARE_API_KEY_{NAME}=...).
    env_prefix = name.upper()
    starter = (
        "# Direct-to-SaaS publishing for this brand. NO SECRETS HERE — only\n"
        "# env-var NAMES (add matching keys to .env) + Zernio accountIds.\n"
        "providers:\n"
        "  ayrshare_main:\n"
        "    type: ayrshare\n"
        f"    api_key_env: AYRSHARE_API_KEY_{env_prefix}\n"
        "  zernio_main:\n"
        "    type: zernio\n"
        f"    api_key_env: ZERNIO_API_KEY_{env_prefix}\n"
        "\n"
        "platforms:\n"
        + "".join(
            f'  {net}:\n    provider: zernio_main\n    account_id: ""   # fill via --list-accounts zernio_main\n'
            if net != "snapchat" else
            "  snapchat:\n    provider: ayrshare_main\n    format: spotlight\n"
            for net in networks
        )
    )
    (root / "profiles.yaml").write_text(starter, encoding="utf-8")

    logger.info(f"Created brand '{name}' at {root}")
    return get_user(name)


# ── Generation (pluggable per brand) ──────────────────────────────────────

def run_generator(ctx: UserContext, fmt: str, subject: str, **kwargs) -> Path:
    """
    Run the brand's video generator and tag the produced script with the brand.
    Registry maps generator id -> callable(fmt, subject, **kwargs) -> Path.
    New brands default to the ResetWorth pipeline until a custom one is defined.
    kwargs pass through to pipeline.run (e.g. use_hook, hook_transition, pov_text).

    Every generator (ResetWorth's included) writes its video + script into the
    SAME shared root output/videos + output/scripts trees -- that's fine for
    ResetWorth (the default brand, unchanged), but a second brand's content
    physically mixed in there is what the 2026-07-28 "separate folders" ask
    was about. For any non-default brand, relocate the finished pair into
    this brand's own users/<name>/output/ tree (already scaffolded by
    create_user()) right after tagging.
    """
    gen = _GENERATORS.get(ctx.generator, _GENERATORS[DEFAULT_USER])
    video_path = gen(fmt, subject, **kwargs)
    return tag_and_relocate(video_path, ctx)


def tag_and_relocate(video_path: Path, ctx: UserContext) -> Path:
    """Same tag+relocate step run_generator applies to every pipeline-produced
    video, exposed standalone (2026-08-09) for callers that build a video via
    modules.assembler.assemble() directly instead of going through
    run_generator -- namely the format15/16 "Duel Financier"/"Téléphone Rouge"
    dedicated-panel wizard (app.py's /api/format15|16/generate + /build),
    which used to leave every brand's videos in the shared default-brand
    tree regardless of which brand's batch launched it."""
    _tag_script(video_path, ctx.name)
    if not ctx.is_default:
        video_path = _relocate_to_brand(video_path, ctx)
    return video_path


def _resetworth_generator(fmt: str, subject: str, **kwargs) -> Path:
    if fmt == "youtube_long":
        from modules.long_format.long_format_generator import generate as long_format_generate
        return long_format_generate(subject, assembly_mode=kwargs.get("assembly_mode", "random"))
    from pipeline import run as pipeline_run
    return pipeline_run(fmt, subject, **kwargs)


_GENERATORS = {
    DEFAULT_USER: _resetworth_generator,
    # "utilisateur2": _utilisateur2_generator,   # add when its format is defined
}


def _relocate_to_brand(video_path: Path, ctx: UserContext) -> Path:
    """Move a freshly generated video + its script JSON out of the shared
    root output/ tree into this brand's own users/<name>/output/ folder, so
    different brands' generated files never physically mix on disk. Never
    called for the default brand -- ResetWorth keeps its existing flat
    output/videos + output/scripts layout completely unchanged."""
    videos_dir  = ctx.root / "output" / "videos"
    scripts_dir = ctx.root / "output" / "scripts"
    videos_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    ts = video_path.stem[:15]
    for f in _SCRIPTS_DIR.glob(f"{ts}*.json"):
        try:
            shutil.move(str(f), str(scripts_dir / f.name))
        except Exception as e:
            logger.warning(f"_relocate_to_brand: couldn't move script {f}: {e}")

    dest = videos_dir / video_path.name
    try:
        shutil.move(str(video_path), str(dest))
        return dest
    except Exception as e:
        logger.warning(f"_relocate_to_brand: couldn't move video {video_path} ({e}), leaving it in place")
        return video_path


def _tag_script(video_path: Path, user: str) -> None:
    """Write `_user` into the script JSON produced for this video (for anti-dup scoping)."""
    ts = video_path.stem[:15]
    for f in _SCRIPTS_DIR.glob(f"{ts}*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_user"] = user
            f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


# ── Per-brand history (anti-dup) ──────────────────────────────────────────

def past_subjects(ctx: UserContext) -> set[str]:
    """All past subjects generated for THIS brand (lowercased), from tagged scripts."""
    out: set[str] = set()
    if not _SCRIPTS_DIR.exists():
        return out
    for f in _SCRIPTS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        tag = data.get("_user", DEFAULT_USER)   # untagged legacy scripts = default brand
        if tag != ctx.name:
            continue
        s = data.get("subject", "").strip().lower()
        if s:
            out.add(s)
    return out
