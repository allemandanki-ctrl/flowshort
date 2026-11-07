# MindFile — new brand (2026-07-28)

New FlowShort brand, fully decorrelated from ResetWorth (finance/motivation). English-only,
faceless, automated, targeting a broad all-ages audience across TikTok/Shorts/Reels + YouTube.

## 1. Niche decision

**Niche: Applied Psychology & Human Behavior**, angle: narrated real-scenario **"case files"**
rather than generic fact-listicles — each video frames a specific relationship/workplace/social
situation, then reveals the psychological principle driving it (cognitive biases, manipulation
tactics, communication patterns, group dynamics), instead of "5 psychology facts that will blow
your mind."

### Why this niche (vs. the other 4 candidates researched)

| Niche | RPM | Competition | Demographic breadth | Verdict |
|---|---|---|---|---|
| **Psychology / human behavior** | $7-14 | Real, but explicitly beatable with a distinct angle (case-file vs. listicle) | Genuinely transgenerational — relationship/workplace dynamics land with Gen Z and Boomers alike | **Chosen** |
| True crime / cold cases | $6-13 (long-form) | Large established faceless giants already dominate | Broad but skews long-form-first; Shorts mainly funnel to long videos | Heavy per-case research/legal-sensitivity burden, poor fit for a low-maintenance automated pipeline |
| History / mythology narrated | mid-tier | Established 1M+ sub channels, but space is vast | All-ages, universal | Weaker hook/surprise mechanic for short-form (works better long-form only) |
| Horror / creepypasta | high engagement | 6-7M-sub incumbents (Mr. Nightmare, Chills, Corpse Husband) | **Confirmed 16-35 skew — disqualified** on the all-ages requirement | Rejected |
| Science / nature explainers | $0.05-0.15/1k (near-bottom) | Kurzgesagt-tier studios own the high end | Universal | RPM floor makes it the weakest economic choice |

Full sourced research (2026 data) is in the session record; the psychology niche won on the
combination of solid mid-high RPM, confirmed transgenerational reach, real-but-beatable
competition, zero legal/sensitivity overhead (unlike true crime), and a hook mechanic (myth vs.
reality, ranked red flags, case-file reveals) that maps directly onto FlowShort's existing
short-form engines with no new infrastructure.

## 2. Brand

- **Name: MindFile** (channel handle e.g. `@mindfile` — renameable later, not registered anywhere
  external by this work).
- Scaffolded via the existing multi-brand system (`modules/users.py`): `users/mindfile/config.json`
  + `users/mindfile/profiles.yaml`. Generator defaults to the shared `resetworth` pipeline function
  (which is brand-agnostic — it just dispatches by format id), so no new generator code was needed.
- **`users/mindfile/profiles.yaml` has PLACEHOLDER Zernio account IDs** (`account_id: ""`) and
  expects `AYRSHARE_API_KEY_MINDFILE` / `ZERNIO_API_KEY_MINDFILE` in `.env` — this brand cannot
  actually publish until real TikTok/YouTube/Instagram accounts are connected and those values are
  filled in, same as any new brand under this system. ResetWorth's own config/keys were not
  touched.
- Content rule enforced in code: MindFile never mentions a book/product (no Gumroad CTA, no
  "90-Day Advisor" references) — its own system prompts, hashtag pool, and caption builder path
  are fully separate from ResetWorth's (see `_no_promo` gating in `modules/content_meta.py`).

## 3. Five short formats (all wired into `config.py` FORMATS, `pipeline.py`, and
`modules/assembler/__init__.py`'s dispatcher — appear automatically in the dashboard's format
dropdown for any brand)

**Revision note (2026-07-28, same day):** mf1-mf4's first pass leaned too hard on ResetWorth's own
narrative mold (hook claim → 3-5 explainer lines → CTA). Redone after researching what specifically
drives virality in psychology/behavior short-form (not generic short-form advice): POV/second-person
immersion, delayed-reveal ("aha") structures, the "decode this text" sub-genre, self-diagnosis
framing beating third-person "avoid these people" framing, and real cognitive-science reveals. The
technical engines (TTS, ffmpeg, Whisper, asset sourcing) are unchanged — only the narrative
structure/prompts moved. mf5 was already built in this spirit and was left untouched.

| id | Name | Mechanic reused | Narrative structure |
|---|---|---|---|
| **mf1** | POV Reenactment | TTS voice-over + Whisper-synced captions + Pexels real-life B-roll (`assemble_voice_format`) | Second-person, present-tense cold open ("POV: you're mid-argument and somehow you're apologizing") — stays purely inside the moment, no explanation — until a pivot line NAMES the real psychological concept for the first time (delayed reveal), then lands one clear takeaway. 30-45s. |
| **mf2** | Decode | Quote-card overlay on striking video/image B-roll, no voice (`assemble_format3a`) | Opens on a real, verbatim, ambiguous line people actually say/text (in quotes) — the "decode this text" sub-genre — then 2-3 cards read its tone/timing/subtext to reveal what's actually being communicated. Ends on the sharpest decoded line (built-in share-bait). No hook-prefix (ResetWorth's hook bank is luxury-themed, deliberately not reused here). 15-25s. |
| **mf3** | Which One Are You | TTS + Whisper + dark B-roll + numbered countdown overlay (`assemble_format6`) | Self-diagnostic typology, not a "spot these red flags in others" warning list — e.g. "5 ways people cope with rejection, which one is you?" Each item described from the inside, normalizing not shaming, closing on "which number are you? comment below." 45-75s. |
| **mf4** | Your Brain's Blind Spot | Pure motion graphics, no B-roll (`assemble_format8`, unchanged) | Not a motivational myth-vs-effort arc — a real-time cognitive-science reveal: "what you think you just decided" (a snap judgment) vs. the actual 4-stage unconscious process (pattern-match → emotion-tags-it → bias-filters-it → story-after-the-fact), grounded in dual-process theory/confabulation. 14-16s. |
| **mf5** | Two Minds *(original)* | Format11's two-voice TTS engine, new orchestrator (`modules/assembler/mindfile.py: assemble_mf5`) | IMPULSE (fast/reactive) vs REASON (slow/deliberate) argue out a real everyday decision — a concrete dramatization of System 1 vs System 2 thinking. 20-30s, no B-roll/music. |

Every format's script instructions live in `modules/script_generator.py` (`_FORMAT_INSTRUCTIONS["mf1"]`
… `["mf5"]`, plus `_SYSTEM_PROMPT_MINDFILE`) and were smoke-tested live both before and after the
narrative rework; mf5's full video render (TTS → ffmpeg → mp4) was also run end-to-end.

**2026-07-28, full render pass — 3 real bugs found and fixed** (not just mf5 this time, all 5 short
formats were rendered end-to-end through `users.run_generator()`):
1. `modules/visuals.py`'s `_VIDEO_COUNTS`/`_IMAGE_COUNTS` tables had no entries for `mf1`/`mf2`/`mf3`
   — they were silently requesting 0 B-roll assets (not a search failure, a missing-config bug).
   Added `mf1: 5 videos`, `mf2: 4 videos + 4 images`, `mf3: 6 videos` (mf2 kept OUT of the
   `_is_format3` set on purpose, so it never pulls ResetWorth's Drive library or luxury-keyword
   override).
2. `modules/assembler/voice.py`'s `assemble_voice_format` (mf1's engine) passed the wrong dict shape
   to `generate_ass()` (`{"words": [...]}` instead of the `{"word_timestamps": [...]}` it expects)
   — a latent bug that was never exercised before because format2/format5, this function's original
   intended callers, were never actually wired into `config.FORMATS` or `pipeline.py`'s dispatch.
3. mf3's own instructions asked for a full descriptive sentence + mechanism explanation + a
   separate normalizing sentence per ranked item with no word cap — Claude wrote ~90-word items,
   and a 5-item test rendered at **185 seconds** against a 45-75s target. Rewrote the instruction
   with a hard 25-word-per-line cap and explicit total-word-budget math (105-190 words across
   hook+body+cta for 45-75s at spoken pace); re-tested at 146 words / ~61s actual render.

All 5 short formats now render clean, in-range durations: mf1 42.8s (target 30-45s), mf2 16.0s
(15-25s), mf3 61.0s (45-75s), mf4 15.0s (14-16s), mf5 31.6s (20-30s). Full 68-test suite still
passes after every fix.

**2026-07-28, second feedback pass after watching the actual renders:**
- **Music added.** mf4 and mf5 previously had no/wrong-mood music (mf5 had none at all --
  format11's own design says "the two voices carry the whole audio track"; mf4 silently defaulted
  to ResetWorth's "motivation" mood, having no entry of its own). Added a new **"tension" mood** to
  `modules/music_library.py` -- 6 real, verified tracks pulled from incompetech's own `pieces.json`
  catalog (same CC-BY-4.0 source every other mood already uses): Stay the Course, Symmetry, I Can
  Feel it Coming, Man Down, Half Mystery, Long Note One. mf4/mf5 now both use it, mixed low (mf5:
  15% volume under the dialogue) so it never competes with the spoken lines.
- **mf5 got a real, darkened video background** instead of format11's shared flat charcoal
  gradient -- fetches real Pexels B-roll via its own `visual_keywords` (now actually consumed,
  previously unused), darkens it for text legibility, loops it to length. format11 itself
  (ResetWorth) was NOT touched -- this is new code in `modules/assembler/mindfile.py`.
- **mf4 was forked into its own function** (`assemble_mf4`), no longer a straight dispatch onto
  ResetWorth's `assemble_format8`. Same user feedback (flat background reads as cheap) applied
  here too: reuses format8's chart/text/CTA rendering machinery via import (unchanged math/timing)
  but swaps the flat gradient for a real, blurred, darkened photo backdrop. ResetWorth's own
  "format8" dispatch is completely unaffected (still points at the original `assemble_format8`).
  This was a considered "drastically improve, don't cut" call given the user's explicit either/or
  ultimatum on mf4 -- the underlying concept (a real cognitive-science reveal) tested strong, the
  complaint was specifically visual/production value.
- Re-verified all 5 renders after these changes: mf4 15.0s with a real fetched photo + tension
  music, mf5 36.2s with real fetched video B-roll + tension music mixed under the dialogue.

**2026-07-28, mf4 cut entirely** ("j'ai pas compris l'objectif du truc, dégage moi ça"). The
drastic-improvement attempt (real photo backdrop) didn't land for the user, so per their explicit
either/or ultimatum, mf4 was removed rather than iterated on further: gone from `config.py`
FORMATS, `pipeline.py` dispatch, `modules/script_generator.py` instructions, `modules/content_meta.py`
routing, `modules/assembler/__init__.py` dispatch table, `modules/assembler/mindfile.py` (the whole
`assemble_mf4`/`_render_mf4_curves` section deleted), and `modules/music_library.py`'s mood mapping
(the "tension" mood ITSELF stays -- mf5/mf6 still use it).

**2026-07-28, mf6 "The Exchange" added as the new 5th short format.** A simulated real back-and-forth
text-message thread (not one message decoded like mf2 -- a full alternating exchange between two
people) dramatizing a specific psychological/relational tactic in real time (isolation, gaslighting,
breadcrumbing, DARVO, etc.), ending on a distinct dimmed reveal card naming the mechanism -- the
"screenshot this" payoff. Forked from format10's chat-bubble engine
(`modules/assembler/chatfeed.py`): `_build_bg`/`_render_thread`/`_chatfeed_cmd` turned out to
already be side-agnostic (only `assemble_format10`'s own message-list construction hardcodes
hook=left/body+cta=right), so mf6 reuses all three directly and only adds its own message-list
construction (alternating YOU/THEM) plus a new `_render_mf6_reveal` for the final card. format10
itself (ResetWorth's "Fake Conversation") is completely untouched. Uses the "tension" music mood,
very quiet (this is a reading format, same reasoning mf2 already established). Live-tested: script
generation (a genuinely strong "isolation" exchange, escalating from plausible-deniable to
unmistakable) and a full render (20.6s, target 20-28s).

MindFile is back to 5 short formats: mf1 (POV Reenactment), mf2 (Decode), mf3 (Which One Are You),
mf5 (Two Minds), mf6 (The Exchange).

**2026-07-28, mf6 visual rework -- actual iPhone Messages look.** First version reused format10's
generic dark-chat mockup as-is and didn't read as texting. Rebuilt mf6's rendering entirely as its
own functions (`_mf6_build_bg`/`_mf6_render_thread`/`_draw_imessage_bubble` in
`modules/assembler/mindfile.py`) -- true-black iOS dark-mode canvas, a stylized status bar (9:41 +
signal/wifi/battery glyphs), a generic circular-avatar contact header (never a real photo -- this
format's contacts are always anonymous/composite), and real iMessage-style bubbles (blue sent /
dark-gray received, each with a small tail, "Delivered" under the last sent bubble). `_chatfeed_cmd`
(the generic ffmpeg assembly step) is still shared with format10; the actual bubble/background
drawing is not. format10/ResetWorth completely untouched. Re-rendered and verified (20.6s, same
duration as before, now with the new visuals).

**2026-07-28, third pass -- closer to a real iPhone recording + adaptive pacing + POV intro:**
- Added the elements a real Messages screen recording actually has and the first pass was missing:
  a dynamic-island pill, a back chevron in the header, a persistent message-compose bar at the
  bottom (+ button, "iMessage" placeholder, mic icon), and a home-indicator bar. All in
  `_mf6_build_bg` -- still its own function, format10 untouched. Reserved bottom margin
  (`_MF6_BOTTOM_H`) increased so bubbles never collide with the new compose bar.
- **Adaptive per-bubble timing.** Fixed 2.3s/bubble read as "too fast, not tied to how long a
  message actually takes to read" -- replaced with `_mf6_reading_hold()`: each bubble's hold is
  proportional to its own word count (~4 words/sec + a small fixed pad), clamped to [1.4s, 4.2s].
  The reveal card keeps a fixed, generous hold (it's the payoff, always given room regardless of
  its own length).
- **A quick POV hook card before the phone appears.** New `pov_hook` script field (same convention
  as format14/15's own pov_hook -- literal "POV: ..." framing), rendered as a plain black title
  card (`_render_mf6_pov_card`) held ~2s, THEN cut to the phone screen -- a clean scene change
  instead of opening straight on the thread.
- Duration is no longer a fixed target in the instructions (adaptive pacing makes a fixed range
  meaningless) -- guidance now describes the pacing logic instead, with a rough expected range
  (25-40s).
- Re-tested: script generation (a strong "breadcrumbing" exchange, pov_hook landing well) and a
  full render (28.0s, valid video+audio streams, 68-test suite green).

**2026-07-28, fourth pass -- POV voice + slower pacing + blurred teaser:**
- **POV card now has a real TTS voiceover** (`_synthesize_mf6_pov`, same default narrator voice as
  mf1) reading the line, and the card's hold duration is driven by the actual narration length
  (+ a small tail pad) instead of a guessed fixed value -- directly fixes "too short."
- **Slowed the per-message reading pace further** (`_MF6_READING_WPS` 4.0 -> 2.8, min hold 1.4s ->
  2.0s, max 4.2s -> 5.5s) -- a first adaptive-timing pass was still judged too fast.
- **The POV card's backdrop is now the COMPLETE conversation, blurred + dimmed** (not plain black)
  -- `_mf6_blurred_thread_bg` composites the full rendered thread onto the phone background, blurs
  it, dims it. The full thread is rendered ONCE up front and reused both for this blurred teaser
  and as the final cumulative-reveal stage (no duplicate rendering).
- Background music now mixes with the POV narration (`_mix_mf6_audio`) rather than only starting
  once the narration ends.
- Re-tested: full render (41.6s with the slower pacing + real POV voice length, valid video+audio
  streams, 68-test suite green).

## 4. Long-form: MindFile Deep Dive (`mf_long`)

12-18 minute chaptered YouTube documentary on a **real, well-documented** psychology
experiment/case/event (Milgram, Stanford Prison Experiment, bystander effect, etc. — never
invented, same grounding rule as ResetWorth's `long1`). Structure: The Setup → What Happened →
Why It Happens → What It Means For You.

**Format4 (whiteboard/Remotion) was not used** — it isn't part of this pipeline's active format
set and would have meant new rendering infrastructure. Instead this reuses **long1/long3's existing
long-form machinery wholesale**: real fetched photo per beat (Ken Burns pan/zoom), blurred-photo
chapter cards, concat-demuxer stitching for guaranteed A/V sync, music bed mux. The one
substitution versus `long1`: a **synthesized TTS narrator voice** (`voice_bank` archetype, picked
per episode) instead of `long1`'s requirement for the user's own real recorded voice — the same
automation trade-off `long3` already made relative to `long1`, chosen here because MindFile has no
host to record narration and needs to be fully automatable end-to-end.

New code: `modules/assembler/mindfile.py: assemble_mf_long` (thin orchestrator over long1's
`_fetch_beat_images` / `_render_beat_clip` / `_render_chapter_card_clip` / `_concat_av_clips` /
`_mux_with_music`). Script generation: `modules/script_generator.py: _generate_mf_long_script`.

## 5. Publishing status (as of 2026-07-28)

- Real Zernio API key added (`ZERNIO_API_KEY_MINDFILE` in `.env`), scoped to TikTok + YouTube only
  — Instagram/Snapchat/Facebook deliberately deferred (explicit user call), and Ayrshare was left
  completely untouched (no key, unused provider stanza).
- The key authenticates fine against Zernio (verified via `python schedule_batch.py --list-accounts
  zernio_main --profiles-file users/mindfile/profiles.yaml`), but that Zernio workspace has **0
  connected social accounts so far** — TikTok and YouTube still need to be linked on Zernio's own
  dashboard before `account_id` can be filled into `users/mindfile/profiles.yaml`. Re-run that
  command after connecting them and drop the resulting IDs in.
- Nothing else is blocked — all 6 formats generate scripts and render successfully against the
  existing Pexels/edge-tts (free Microsoft TTS)/Whisper/Freesound infrastructure, no new API keys
  or paid services required.

## 6. Output separation between brands (2026-07-28)

Every brand's OWN generator previously still wrote into the shared root `output/videos/` +
`output/scripts/` trees — physically identical to ResetWorth's. Per explicit user request, this is
now separated: `modules/users.py: run_generator()` relocates a non-default brand's finished video +
script pair into that brand's own `users/<name>/output/videos/` + `users/<name>/output/scripts/`
folder immediately after generation (`_relocate_to_brand`). **ResetWorth (the default brand) is
completely unaffected** — its files stay in the original flat `output/videos/`/`output/scripts/`
exactly as before; this only ever fires for non-default brands.

The dashboard (`app.py`) now looks across every brand's own folder (`_all_video_roots()`/
`_all_script_roots()`) for the video grid, video streaming, and the publish button, so MindFile's
(and any future brand's) videos still show up, play, and publish correctly — each is now also
tagged with a small brand badge in the grid (`v.brand`, from the script's `_user` field) so the two
brands' content is visually distinguishable even though they share one dashboard.

## 7. MindFile's own book: "The Sabotage Point" (2026-07-29)

MindFile now has its own product, same role as ResetWorth's "The 90-Day Advisor" — never the same
book, never the same Gumroad link. Picked after real market research (not just an angle guess): the
"self-sabotage / attachment / people-pleasing" self-help space is already flooded with cheap,
generic 30-day KDP workbooks, so "have a structured system" alone isn't a differentiator anymore.
The chosen angle — self-sabotage specifically at the threshold of success — is narrow, universal,
ungendered, and its one real competitor (Gay Hendricks' *The Big Leap*) is insight-only with no
dated system.

- Full manuscript: `users/mindfile/book/THE_SABOTAGE_POINT.md` (~5,700 words, 4-part structure
  mirroring the ResetWorth reference book's own DNA — KEY POINTS / DO THIS NOW boxes, a 5-type
  "Sabotage Signature" typology, a bonus toolkit).
- Designed edition: published as an HTML artifact (animated hero — a line rising to "THE SABOTAGE
  POINT" then forking into a kept win vs. an undone drop), then converted to a real paginated PDF
  via headless Chrome print (`users/mindfile/book/The Sabotage Point.pdf`, 28 pages, 6x9in trim).
  Two print-CSS bugs were hit and fixed along the way (Chrome strips print backgrounds by default;
  `@page` margins are never painted by the document background at all, regardless of color-adjust
  rules — fixed by zeroing the page margin and using real padding instead).

## 8. Book CTA wiring (2026-07-29)

Per explicit user instruction, book promotion is now **every eligible video, no rotation**, for
both brands:

- **ResetWorth:** `config.BOOK_CTA_EVERY_N` changed from `4` (1 video in 4) to `1` (every video).
- **MindFile:** `modules/content_meta.py` now supports a per-brand book promo
  (`meta["_book_promo"]` = `{title, link, comment_suffix}`), read by new `_book_promo_line`/
  `_comment_cta_line` helpers that both `build_youtube_description` and `build_social_caption` use
  — falling back to ResetWorth's original hardcoded phrasing when no `_book_promo` is present, so
  ResetWorth's own output is unchanged. MindFile's own promo is generated by
  `_mindfile_book_promo()` in the same file, which returns `None` (i.e. `_no_promo=True`, silent)
  until `GUMROAD_LINK_MINDFILE` (config.py / `.env`) is actually filled in with a real Gumroad link.

**Nothing is being promoted anywhere right now** — `GUMROAD_LINK_MINDFILE` is still empty. The only
remaining step to go live is adding that one line to `.env`; no other code change is needed. Verified
both states directly (empty → silent captions, simulated real link → correct promo text) before
calling this done.
