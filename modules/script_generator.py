"""
Step 1 - Script Generator
Input  : format (format1-format5) + subject string
Output : structured JSON saved to output/scripts/ and returned as dict

JSON schema:
{
  "format": str,
  "subject": str,
  "hook": str,               # <= 3 sec spoken (1-2 punchy sentences)
  "body": [str, ...],        # 3-6 lines, each a distinct beat
  "cta": str | null,         # spoken CTA at the end, null if no book CTA
  "include_book_cta": bool,  # whether to show book card overlay
  "visual_keywords": [str],  # 5-8 terms for B-roll / image search
  "music_style": str,        # e.g. "cinematic", "lo-fi", "motivational"
  "duration_target": int,    # target video duration in seconds
  "description": str,        # caption for posting
  "hashtags": [str]          # 10-15 relevant hashtags
}
"""

import json
import random
import re
import logging
from datetime import datetime
from pathlib import Path

from config import CLAUDE_MODEL, OUTPUT_DIR, FORMATS
from modules.claude_cli import call_claude
from modules.cta_counter import should_include_book_cta, increment
from modules import users

logger = logging.getLogger(__name__)

_SCRIPTS_DIR = OUTPUT_DIR / "scripts"

_FORMAT_INSTRUCTIONS = {
    "format1": """Format: Clip Edit.
The video is a real clip (film scene, CEO speech, anime, documentary) that the creator supplies.
Whisper will transcribe the clip dialogue — the text overlays shown in the final video come from the clip itself.
Your script provides the THEME and METADATA for this clip, not the dialogue.
Hook: single bold statement capturing the clip's core message (max 10 words). Used for title/caption, NOT displayed in video.
Body: 3-5 punchy lines expanding the theme (max 10 words each). Used for caption only.
visual_keywords: describe the visual mood of the clip (e.g. "cinematic dark CEO luxury"). Used as search fallback.
Music style: phonk, dark cinematic, or emotional instrumental — no lyrics.
Duration: 20-50 seconds.""",

    "format3": """Format: Luxury Aesthetic + ONE Quote.
NO voice-over. ONE single short quote is the ENTIRE on-screen text for the whole video (~16s), displayed
center screen over dimmed luxury B-roll (cars, watches, skyline, penthouse) -- exactly like a viral
"@quotepage" TikTok: one bold line, nothing else, no sequence of cards. Think "Be the pressure.", "Do or
die.", "My time is coming.", "WIN OR WIN." -- a single, complete, standalone sentence.

Hook = that one quote. HARD CAP 10 words, and it must be ONE sentence (one clause, one period at the very
end) -- never chain 2-3 short sentences together into one hook, that reads as rambling, not a quote. If it
doesn't work as a single short sentence, cut words until it does.

MANDATORY, and this applies to the HOOK SPECIFICALLY, not the body: violent OR all-or-nothing OR
mortality-stakes imagery, used figuratively (mindset metaphor, NEVER a literal threat at a real named
person) -- this is the actual point of the format, not optional flavor. Reach for words/stakes like: die,
death, dying, kill/killing, blood, bleed, pain, suffer, war, betray, venom, no mercy, destroy -- DEATH and
PAIN as the stakes land harder here than "war/hunt/predator" did before, lean into those. "Win or die
trying" energy, not just "I go to war" energy. A hook that's merely cocky/pleasant about money/effort, with
no real violence or mortality stakes in it, is a FAILED draft -- rewrite it.

Do NOT hold back your best, hardest-hitting line as "backup material" for the body -- write the single most
brutal line you can think of for this subject FIRST, and that becomes the hook. The body lines are
leftovers, written AFTER and FROM WHAT'S LEFT OVER once the hook is locked in -- never the reverse. If, while
drafting, a body line ends up more brutal/punchier than the hook, that line IS the hook -- swap them.

Body: still return 2-3 short backup lines (<= 8 words each, same voice) for variety/reuse elsewhere, but
they will NOT be shown on screen -- the hook is the only line that airs, so it must be the strongest one.

Writing style: RAW, genuinely controversial, first-person, cocky, take-it-or-leave-it mentality -- NOT soft
aspirational-poetic, not "a little edgy", not a clever reframe. Write the line that makes half the comments
mad and the other half quote-tweet it. This is broader than just money: ego, reputation, revenge, betrayal,
being underestimated, cutting people off, not caring what anyone thinks, going quiet then coming back
stronger, grinding through heartbreak/loss -- money is ONE angle among several, not the only one every time.
Actively dismiss and look down on the things people romanticize (love, sleep, friends, "balance", loyalty,
other people's feelings, being liked, asking for permission) -- say the quiet part, don't soften it with a
caveat or silver lining at the end. Commit to the take.

Censored profanity/insults ("f*ck", "f*ck it", "sh*t", "p*ssy" as in "don't be a p*ssy") fit this voice and
are welcome, but are NOT required in every hook -- use judgment, plenty of hard-hitting lines have zero
profanity in them. Always censor with an asterisk in place of the vowel(s) (f*ck, sh*t, p*ssy, a**) -- never
spell it out in full.

Lean into actual modern internet/gym-bro/gen-Z voice AND raw quote-page philosophy, NOT literary metaphor.
BANNED as a crutch: the "X is/kills/bleeds/is bait Y" templated metaphor sentence (e.g. "your paycheck is
killing your future", "my salary was the enemy disguised as a plan") -- if your first instinct is a clever
noun-metaphor sentence like that, throw it out. Also BANNED as a crutch: framing the line around an
arbitrary day-count ("in 90 days...", "in N days I'll be...").

Instead, the exact SHAPE to use for this hook is given below as "Hook SHAPE" -- follow it precisely, and hit
the quality bar of a real viral quote-page post, e.g. (do not copy these, they're calibration only): "Win or
WIN.", "Even at my lowest I'm better than you.", "Enjoy the pain, it's the way to gain.", "Comfort kills
your potential.", "Winners are the biggest losers.", "My ego never dies.", "F*ck your feelings, go get the
bag." These are SHORT, ABSOLUTE, and either violent, mortality-stakes, or a dark twisted-wisdom paradox --
never a tidy little finance metaphor.
Adapt the ENERGY of the assigned shape to the subject rather than reusing any reference line verbatim.

Vocabulary must stay simple, street-level everyday language -- NO finance/business jargon (no "ROI",
"leverage", "assets", "equity", "compound interest", "portfolio", "liquidity", "cash flow", "net worth" as a
term, etc.). Say it the way someone would actually talk out loud, not the way a finance textbook or LinkedIn
post would phrase it -- "money", "rich", "broke", "the bag", "pay" instead of jargon.
Also BANNED: "paycheck" and "salary" -- too narrow, only lands for someone with a salaried job, most people
watching don't clock in anywhere. Use "money", "broke", "poor", "the bag", "what you're making" instead --
something that reaches anyone regardless of how they make money.

Vary the angle run to run -- money, ego, revenge, betrayal, loneliness-by-choice, being doubted, going dark
before a comeback, cutting people off, etc. -- don't lock onto the same recurring theme every time, and
don't reuse the same setup ("they slept, I hunted") across different runs.
Music style: dark luxury, ambient, or phonk/cinematic instrumental.
Duration: ~16 seconds (fixed by the renderer -- duration_target is ignored for this format).""",

    "format6": """Format: Ranking List (dark B-roll).
A countdown/ranking listicle, e.g. "Stay away from these 5 types of people, the last one is very dangerous",
"The 6 types of negative people you should avoid", "Beware these 8 dream killers holding you back".
Pick a fresh, specific ranking angle for THIS subject — do not always use the same item count or the same
framing as prior examples. Item count should vary run to run, typically between 4 and 8.

Hook (2 sentences): sentence 1 states the ranking premise ("Stay away from these {N} types of {category}.").
Sentence 2 is a retention hook that gives viewers a reason to stay to the end — vary the exact wording and
the reason each time (it doesn't have to always be "the last one is the most dangerous"; it can be "the last
one will surprise you", "number one is the one nobody talks about", etc.) — never reuse the same retention
line verbatim across videos.

Body: exactly N lines, one per ranked item, each starting with "Number {i}: " followed by a short label for
the item, then 1-2 punchy sentences describing it. Number the items in DESCENDING order — start at N and
count DOWN to 1 (e.g. for a 6-item list, the lines are in this exact order: "Number 6: ...", "Number 5: ...",
"Number 4: ...", "Number 3: ...", "Number 2: ...", "Number 1: ..."). NEVER count upward. "Number 1" must be
the strongest/most dramatic/most extreme entry on the list — the best or the worst, whichever fits this
ranking's premise — and always comes last, as the payoff the retention hook promised.

cta: a short 1-sentence closing line tying the list together (e.g. "Protect your energy. Choose your circle
carefully."). Never a book/product mention — this format is unrelated to the 90-Day Advisor niche.

visual_keywords: 5-8 keywords for DARK-aesthetic B-roll (not necessarily nature) — dark storms, dark forests,
dark urban night streets, dark smoke/fog, dark ocean, dark abstract textures, silhouettes, moody low-light
interiors. Mix it up: don't repeat the same handful of keywords every time, vary within the dark-aesthetic
theme so consecutive videos don't look identical.

music_style: dark cinematic, ominous ambient, or tense instrumental — no lyrics.
Duration: 45-75 seconds depending on item count.""",

    "format8": """Format: POV Realite vs Illusion (Motion Graphics). NO voice-over, NO B-roll
footage — animated line-graph motion graphics on a dark background, no clips.

hook: ONE line framing the naive/common belief about this subject, phrased as "What you think
it takes to [X]:" or an equivalent "the assumption" framing. Displayed next to a plain straight
line graphic (drawn separately, not part of your text).

illusion_labels: EXACTLY 2 short words/phrases (1-2 words each, like "Luck", "One big break")
naming the naive shortcuts/myths implied by the hook — these appear next to the hook's own
straight-line graphic so that side of the screen isn't just bare text, mirroring the 4 stage
labels the reality side gets below.

body: EXACTLY 5 lines.
  - Line 1: the reality-framing line, e.g. "What it actually takes:" (contrasts directly with
    the hook).
  - Lines 2-5: exactly 4 short stage labels (1-2 words each, like "Sacrifice", "Failure",
    "Pivot", "Discipline" but tailored to THIS subject), representing the real, non-linear path
    — they will appear one at a time next to points on an animated curve. Keep them punchy
    nouns/short phrases, not full sentences.

cta: a short kinetic closing line driving a subscribe action, e.g. "Ready? Subscribe."

visual_keywords: not used for footage search here (no B-roll) — still return 3-5 words
describing the mood (e.g. "minimal dark data-driven contrast").
music_style: minimal, tense-to-uplifting instrumental (no lyrics) that can build across the
video — starts flat/muted under the naive-line hook, gains energy through the real-path stages.
Duration: 14-16 seconds.""",

    "format9": """Format: Dilemme du Choix (gamified two-choice dilemma). NO voice-over, NO B-roll —
opens on a red-pill/blue-pill photo, then cuts to a two-panel split screen (BLUE left, RED
right), then "the answer is in the description", pure motion graphics.

Subject can be financial, but doesn't have to be — mindset dilemmas work just as well (e.g.
"discipline vs motivation"), and so does pitting two well-known fictional characters against
each other on their mindset or money approach (e.g. anime/movie/show characters — "Naruto vs
Luffy: whose mindset actually builds wealth?"). Treat the given subject generically; don't force
a financial angle onto a subject that isn't one.

hook: ONE short, punchy provocative line framing this as a test — MAX 8 words, must read in
under 2 seconds (e.g. "99% choose the wrong button." / "Would you pick right?"). Must set up
that TWO options are coming without naming them yet. Do not write a full sentence with a
subordinate clause — this is a headline, not a paragraph.

body: EXACTLY 2 lines, one per option — structure each as "<label>: <outcome>". The outcome
must be SHORT: max 6-8 words, ONE concrete detail (a number, a timeframe, a stake) — NOT a full
sentence, NOT two clauses joined by "but"/"while". If it takes more than 8 words to say, you're
over-explaining — cut to the single sharpest detail. E.g. "Button A: $10K guaranteed today" /
"Button B: 50% shot at $1M in 5 years".
BODY LINE 1 = LEFT/BLUE = the safer, more certain option. BODY LINE 2 = RIGHT/RED = the riskier
option with the bigger potential upside. Plain everyday language, no jargon.

BANNED FRAMING — never write "give up / coast / indulge today" as one option against "stay
disciplined / keep grinding" as the other. This audience is already driven and has already
answered that question; it reads as a fake, one-sided dilemma with zero tension (everyone
obviously picks discipline). Instead, BOTH options must be ambitious, high-effort paths a
focused, driven person could genuinely take — the tension comes from RISK, TIMELINE, or
STRATEGY, not from effort vs laziness. Good axes: guaranteed big outcome vs risky bigger
outcome; fast-and-risky vs slow-and-certain; all-in on one skill/bet vs a diversified path;
build alone vs build with others; short brutal grind vs long sustainable grind. Both sides
should sound like something a serious, ambitious person would actually be torn on.

Pressure-test before finalizing: would 100 random people roughly split 40/60 or closer, not
90/10? If one side clearly wins on its face, or if either option reads as "quitting" while the
other reads as "staying strong", rewrite both with more evenly-matched, equally ambitious stakes.

cta: the comment ask ONLY — a short line pushing viewers to put THEIR pick in the comments,
e.g. "Comment your pick below." / "Which one? Say it in the comments." Do NOT mention the
description here (that's a separate on-screen beat already) and never a book/product mention.

visual_keywords: not used for footage search here — still return 3-5 mood words (e.g.
"tense minimal split-screen decision").
music_style: minimal tense instrumental with a clear rhythmic pulse suited to a countdown
(3-2-1), building tension, no lyrics.
Duration: 14-16 seconds.""",

    "format10": """Format: Fake Conversation (Chat Bubbles). NO voice-over, NO B-roll — a simulated
text conversation, bubbles appearing one at a time and staying on screen (building downward like
a real chat thread), zero footage needed.

hook: the OPENING message — a casual, curious question from a friend texting about this subject,
lowercase/casual texting tone is fine (e.g. "yo is real estate actually worth it or is that a
scam"). This is the LEFT-aligned received bubble.

body: EXACTLY 4 lines — the reply, split across 4 separate message bubbles (not one long text),
each building on the last: bubble 1 hooks/validates the question, bubbles 2-3 give the actual
facts/mechanism (specific, not vague), bubble 4 lands the core insight. Casual texting voice
throughout (can use "ngl", "tbh", lowercase, no need for perfect grammar) but every claim must be
substantively true and specific. These are RIGHT-aligned sent bubbles.

cta: the FINAL message bubble — a natural-sounding closer that invites saving/sharing without
sounding like an ad, e.g. "screenshotting this for later ngl" or "sending this to everyone I
know who's still scared of it". Never a book/product mention.

visual_keywords: not used for footage search here — still return 3-5 mood words (e.g. "casual
texting screenshot authentic").
music_style: minimal, low-key lo-fi or ambient instrumental, unobtrusive (it's a reading format,
music should not compete for attention), no lyrics.
Duration: 14-18 seconds.""",

    "format11": """Format: Dialogue Mentor/Eleve (two real TTS voices alternating -- a student and a
mentor). NO B-roll, NO music-carried mood — the two distinct voices themselves are what breaks the
monotony of a single narrator. Six alternating turns total: student, mentor, student, mentor,
student, mentor.

hook: the STUDENT's opening question — naive but genuine, casual spoken tone (this is read aloud by
TTS, so it must sound natural spoken, not written), 1 sentence, tied to the subject.

body: EXACTLY 4 lines, ALTERNATING starting with the mentor (position in the array is what marks
who's speaking — a rendering step maps body[0]/body[2]/cta to the mentor's voice and hook/body[1]/
body[3] to the student's voice, entirely outside this JSON. DO NOT write "Mentor:" / "Student:" or
any speaker label INSIDE the text itself — every line is fed directly to text-to-speech, so a
literal label would get spoken out loud):
  - body[0] (spoken by the mentor): answers with a concrete example or number, not a platitude.
  - body[1] (spoken by the student): a natural follow-up question or pushback — genuinely curious
    or skeptical, not just "wow really?"
  - body[2] (spoken by the mentor): goes one level deeper — the mechanism WHY, escalating
    specificity from body[0].
  - body[3] (spoken by the student): a final skeptical or personal question ("but what if I only
    have $500?" style) that sets up the closing synthesis.
Each line is ONE short sentence, under 18 words — this is spoken dialogue at TikTok pace, not an
essay (a prior draft averaged 2 dense sentences per line and the video ran 54 seconds; every line
this format's total spoken length must land in the 20-30s duration_target). Natural spoken rhythm
(contractions, short sentences) since every line is synthesized as real speech.

cta: spoken by the mentor — the closing synthesis that ties the exchange together with the core
lesson in one punchy spoken sentence. No speaker label in the text. Never a book/product mention.

visual_keywords: not used for footage search here — still return 3-5 mood words (e.g. "minimal
dialogue two voices contrast").
music_style: not used (voices carry the audio) — return "none".
Duration: 20-30 seconds (6 spoken turns).""",

    "format12": """Format: Audit Financier de Personnage (financial roast of a fictional character).
NO voice-over, NO B-roll, NO clips or images of the character — purely textual commentary
referencing the character BY NAME ONLY (nominative reference, like written commentary/parody,
not using any clip, image, or cloned voice of them). Report-card style motion graphics.

The "subject" for this format IS the character's name + a well-known fictional/pop-culture
context (e.g. "Bruce Wayne / Batman", "Walter White / Breaking Bad") — pick one if not already
specific, and it must be a character famous enough that the joke lands from the name alone.

hook: introduces the audit, e.g. "Today we're auditing [Character]'s finances. It's bad." One
punchy sentence, naming the character.

body: EXACTLY 3 lines, each ONE specific "financial sin" tied to something the character
actually does/owns in their own story (not generic finance advice wearing a costume) — funny,
specific, and only make sense if you know the character (e.g. for Batman: "Spent the GDP of a
small country on a car that's illegal to drive on public roads"). Each line max ~20 words.

cta: the verdict + a lesson pivot, e.g. "Don't be like [Character]. Subscribe for the videos that
actually build wealth." Never a book/product mention.

visual_keywords: not used for footage search here — still return 3-5 mood words (e.g. "report
card audit minimal dark comedic").
music_style: minimal instrumental with a slightly wry/playful edge, no lyrics.
Duration: 14-18 seconds.""",

    "format14": """Format: Interrogatoire (Interrogation Room, two real TTS voices --
a fixed interrogator persona and a fictional character being questioned). NO
B-roll, NO music-carried mood -- reuses format11's two-voice engine, but here
one voice is fixed (the interrogator) and the other is picked automatically
from an archetype voice bank based on the traits you provide below (age
register, tone, accent) -- NEVER the real actor/performer's own voice, just a
plausible archetype for the ROLE.

The "subject" for this format IS the character's name + a well-known
fictional/pop-culture context (same convention as format12), e.g. "Walter
White / Breaking Bad", "Thanos / Marvel" -- pick one if not already specific,
famous enough that the joke lands from the name alone.

suspect_name: the character's name ONLY (no context), e.g. "Walter White" --
used as the on-screen label for their turns.

suspect_voice: an object describing an ARCHETYPE for this character's voice,
never the real actor's specific timbre:
  gender: "male" or "female" (whichever the character is).
  age_register: one of "young", "adult", "mature" -- the character's
    apparent age, not the actor's real age.
  accent: one of "american", "british", "australian", "irish", "indian",
    "canadian", "south_african" -- whichever the CHARACTER's setting/origin
    plausibly implies, not the actor's own accent.
  tone: 2-4 short descriptive words for how this character generally comes
    across (e.g. ["gravelly", "weary", "intense"] for Walter White,
    ["menacing", "theatrical"] for a cartoonish villain) -- describe the
    ARCHETYPE/vibe, not an impression of the specific performer.

This is a MOTIVATION / WEALTH-MINDSET / FINANCIAL-ADVICE channel -- the
"crime" being interrogated MUST be a FINANCIAL blunder from the character's
own story (a wasted fortune, an insane purchase, a terrible investment,
money laundered through an absurd front, life savings blown on something
ridiculous), the exact same angle format12's "financial audit" already uses
successfully -- never a generic non-financial crime. This is a comedy
interrogation, not a procedural drama: prioritize being genuinely FUNNY --
sharp, witty comebacks, comic timing, absurd specificity -- over sounding
tense or serious. Think roast/stand-up energy in an interrogation-room
costume, not Law & Order. But underneath the joke, the whole bit must cash
out into ONE clear takeaway that fits the channel identity -- pick WHICHEVER
ONE of motivation, wealth-building mindset, or financial advice fits this
character's specific blunder best (not all three, one is enough): what a
viewer should actually DO or believe differently, landing through the
interrogator's lines, not a bolted-on moral at the very end.

pov_hook: a SEPARATE opening line, shown alone as its own title card (with
the character's photo) BEFORE the interrogation starts, read aloud by the
INTERROGATOR. Literal TikTok "POV:" format -- must start with "POV:" and
complete it as a scroll-stopping premise tied to the channel identity, e.g.
"POV: [Character] explains wealth building and it goes about how you'd
expect", "POV: you ask [Character] for financial advice." Punchy, one
sentence, sets up the bit that follows without giving away the jokes.

Six alternating interrogation turns after the pov_hook/hook: interrogator,
suspect, interrogator, suspect, interrogator, suspect -- but this format now
runs LONGER (TikTok's payout minimum is 60s), so body has EXACTLY 8 lines,
not 4 -- more back-and-forth, not longer individual lines.

hook: the INTERROGATOR's opening accusation (spoken right after the
pov_hook card), naming the character's specific FINANCIAL crime from their
own story, delivered dry/deadpan for comic effect, in one punchy spoken
sentence.

body: EXACTLY 8 lines, ALTERNATING starting with the suspect (position in
the array marks who's speaking -- a rendering step maps even indices
(body[0], body[2], body[4], body[6]) to the suspect's voice and odd indices
(body[1], body[3], body[5], body[7]) to the interrogator's voice, entirely
outside this JSON. DO NOT write speaker labels inside the text -- every line
goes straight to TTS):
  - body[0] (suspect): a funny, in-character deflection or excuse about the
    money.
  - body[1] (interrogator): a witty comeback that presses further, citing a
    SPECIFIC financial detail from the character's own story as evidence
    (not generic finance advice wearing a costume -- must only make sense if
    you know the character, and it should land as a joke, not just a fact).
  - body[2] (suspect): doubles down with another in-character excuse.
  - body[3] (interrogator): presses on a SECOND, different specific
    financial detail from the character's story -- escalating, not
    repeating body[1]'s beat.
  - body[4] (suspect): cracks a little, a reluctant partial admission,
    still funny.
  - body[5] (interrogator): the line that lands the channel's actual
    takeaway (motivation / mindset / financial advice, whichever fits) as
    a punchy, quotable insight -- still witty, not a lecture.
  - body[6] (suspect): comically weak final excuse, self-aware.
  - body[7] (interrogator): the final gotcha line or question, setting up
    the suspect's closing punchline.
Each line ONE short sentence, under 18 words, natural spoken rhythm
(contractions, short sentences) -- spoken dialogue pace, total spoken
length across pov_hook+hook+body+cta must land in the 60-75s
duration_target (TikTok monetization needs 60s minimum -- this is a hard
floor, not a suggestion).

cta: spoken by the suspect -- a final self-incriminating, comically resigned
one-liner about their finances, in character, that lands as the punchline.
No speaker label in the text. Never a book/product mention.

visual_keywords: not used for footage search here -- still return 3-5 mood
words (e.g. "interrogation room comedic financial").
music_style: not used (voices carry the audio) -- return "none".
Duration: 60-75 seconds (pov_hook + hook + 8 body turns + cta).""",

    "format15": """Format: Duel Financier (Financial Duel -- two DIFFERENT fictional
characters roast each other's money mistakes across several rounds, with a
fixed neutral "announcer" voice narrating the intro and declaring the
winner). Two archetype-matched voices (one per character, via voice_bank --
NEVER the real actor/performer's voice) plus a fixed announcer voice.
Generalizes format14's two-voice engine to three speaker roles.

The "subject" is BOTH characters + their contexts: "<CharacterA> / <ContextA>
vs <CharacterB> / <ContextB>", e.g. "Walter White / Breaking Bad vs Tony
Stark / Marvel Cinematic Universe" -- two DIFFERENT, famous, well-known
characters (anime, cartoon, movie, series, game, book, anything), picked for
a genuinely funny financial mismatch/contrast, not two random unrelated
names.

char_a_name / char_b_name: each character's name ONLY (no context) -- used
as their on-screen labels.

char_a_context / char_b_context: each character's context ONLY (e.g. "The
Office", "Breaking Bad", "Marvel Cinematic Universe") -- kept as SEPARATE
fields (not just parsed back out of "subject") so real-footage/image search
for character A never accidentally picks up character B's context or vice
versa. This context is used VERBATIM as a real search query against real
footage/photos, so it must be the character's ACTUAL, CORRECT source --
double check before answering: do not confuse a character with a
similarly-themed one from a different franchise (e.g. Krusty the Clown is
FROM THE SIMPSONS, not SpongeBob SquarePants -- a "clown" character concept
existing in another show is not the same character). If you are not
fully certain which franchise a character is from, pick a different, more
certain character instead.

char_a_voice / char_b_voice: same archetype-only shape as format14's
suspect_voice (an archetype for the ROLE, never an impression of the real
actor/performer):
  {"gender": "male|female", "age_register": "young|adult|mature",
   "accent": "american|british|australian|irish|indian|canadian|south_african",
   "tone": ["<tag>", "..."]}

pov_hook: the ANNOUNCER's opening title-card line, literal TikTok "POV:"
format, framing the duel and tied to the channel identity (motivation /
wealth mindset / financial advice -- pick whichever ONE fits this specific
matchup). VERY SHORT -- under 14 words total, e.g. "POV: Walter White and
Tony Stark settle who actually knows how to build wealth." This is read
aloud over a quick opening card, not a paragraph -- one short punchy
sentence, never two.

rounds: EXACTLY 3 objects, each an in-character financial roast exchange:
  {"a_line": "<Character A's line>", "b_line": "<Character B's clapback>",
   "round_winner": "a" or "b"}
  - a_line: Character A attacks a SPECIFIC financial blunder from Character
    B's own story (must only make sense if you know that character) --
    funny, in Character A's own voice/personality.
  - b_line: Character B's comeback -- either a witty deflection, or a
    counter-attack on a SPECIFIC financial blunder from Character A's own
    story. Whichever line actually lands harder is that round's winner.
  - round_winner: vary this across the 3 rounds -- don't make the same
    character win every single round, real back-and-forth, not a one-sided
    beatdown.
  Each line ONE short sentence, under 12 words -- this format has an
  animated "hit" cutaway after every single line eating into the runtime,
  so lines must be tight and fast, not the 18-word budget other formats
  get. Escalate specificity/stakes across the 3 rounds -- the last round
  (the "FINAL ROUND") should be the most devastating exchange, not a repeat
  of round 1's energy.

winner: "a" or "b" -- whichever character won the MAJORITY of the 3 rounds
(must be consistent with the round_winner tags above).

winner_line: spoken by the ANNOUNCER -- a VERY short "moral of the story"
line landing the channel's actual takeaway (motivation / wealth mindset /
financial advice, whichever fits) as a punchy, quotable insight, not a dry
recap. ONE short sentence, under 10 words -- genuinely brief, not a full
explanation.

loser_line: spoken by the LOSING character (whichever is NOT `winner`) --
a final comically defeated/self-aware one-liner about their finances, in
character, under 14 words. Never a book/product mention.

Every spoken line goes straight to TTS -- DO NOT write speaker labels
inside any text field.

visual_keywords: not used for footage search here -- still return 3-5 mood
words (e.g. "financial duel comedic showdown").
music_style: not used (voices carry the audio) -- return "none".
Duration: 60-75 seconds. This is a MULTI-SCENE format (VS card, 3 rounds
each opening with a "ROUND N / FIGHT!" card, real footage + an animated
arena hit cutaway per line, K.O. card, winner/loser cards) -- the fixed cutaways alone add real
seconds beyond the spoken lines, so keep every line SHORT and let the
scene count carry the runtime, don't write long lines to hit the target.""",

    "format16": """Format: Le Telephone Rouge (Red Phone -- a panicked character from ONE
fictional universe calls a totally different character from ANOTHER universe for urgent
financial/life advice, because they're sinking). Two archetype-matched voices (one per
character, via voice_bank -- NEVER the real actor/performer's voice). Reuses format14's
two-voice turn engine. Visual: ultra-fast alternating full-screen neon-toned cuts (red for
the caller, blue for the advisor) with an animated waveform -- no B-roll search needed.

The "subject" is BOTH characters + their contexts, same convention as format15:
"<CharacterA> / <ContextA> calls <CharacterB> / <ContextB>". CharacterA (char_a) is the
PANICKED CALLER, in a financial/life crisis specific to THEIR OWN story. CharacterB (char_b)
is the CALM ADVISOR, from a COMPLETELY DIFFERENT, unrelated universe, known for confidence,
competence, or arrogance. The bigger/weirder the gap between the two universes, the better --
the unexpected crossover itself is the hook that keeps viewers watching to see how the two
worlds collide (e.g. Jesse Pinkman calling Harvey Specter, SpongeBob calling Gordon Ramsay,
Michael Scott calling a Wall Street shark).

char_a_name / char_a_context: the panicked caller's name + context (e.g. "Jesse Pinkman" /
"Breaking Bad") -- in a mess specific to THEIR OWN story.
char_b_name / char_b_context: the calm advisor's name + context -- MUST be from a different
universe than char_a. Same franchise-accuracy warning as format15: double-check which
show/movie/game each character is actually from before answering -- if unsure, pick a
different, more certain character instead.

char_a_voice / char_b_voice: same archetype-only shape as format14/15's suspect_voice
(gender/age_register/accent/tone) -- an archetype for the ROLE, never an impression of the
real actor/performer. char_a's tone should read PANICKED (e.g. ["frantic", "anxious",
"rushed"]); char_b's tone should read CALM/CONFIDENT, even a little arrogant (e.g. ["cool",
"dismissive", "authoritative"]).

hook: char_a's opening line -- the panicked call itself, naming their SPECIFIC crisis from
their own story (must only make sense if you know that character), one breathless spoken
sentence, under 20 words, e.g. "Yo Mr. Specter, the IRS is onto me, I've got bags of cash,
what do I do?"

body: EXACTLY 8 lines, ALTERNATING starting with char_b (position marks speaker -- body[0],
body[2], body[4], body[6] = char_b; body[1], body[3], body[5], body[7] = char_a). DO NOT write
speaker labels inside the text -- every line goes straight to TTS:
  - body[0] (char_b): calm, confident, slightly arrogant SPECIFIC financial/life advice, in
    char_b's own voice/personality -- genuinely clever advice dressed as banter.
  - body[1] (char_a): a panicked follow-up question or objection, referencing another SPECIFIC
    detail from char_a's own story.
  - body[2] (char_b): a second piece of advice, escalating the confidence/arrogance.
  - body[3] (char_a): more panic, another specific detail, still unconvinced.
  - body[4] (char_b): the line that lands the channel's actual takeaway (motivation / wealth
    mindset / financial advice, whichever fits) as a punchy, quotable insight -- still witty.
  - body[5] (char_a): a beat of relief or disbelief that this might actually work.
  - body[6] (char_b): a final confidence flex, in character.
  - body[7] (char_a): a short, resigned/relieved reaction, setting up char_b's closer.
Each line ONE short spoken sentence, under 16 words, natural rhythm (contractions, short
sentences), fast rapid-fire pace.

cta: char_b's final dismissive/arrogant one-liner mic-drop, in character, under 14 words
(e.g. "You're an amateur, Jesse."). Never a book/product mention.

visual_keywords: not used for footage search here -- still return 3-5 mood words (e.g. "neon
crossover phone call urgent").
music_style: not used (voices carry the audio) -- return "none".
Duration: 60-75 seconds (hook + 8 body turns + cta -- fast, punchy, montage-driven).""",

    "mf1": """Format: MindFile POV Reenactment (voice-over + B-roll + synced captions).
This is the flagship format for MindFile, a channel about applied psychology and human behavior.
NOT a third-person report about a scenario -- a second-person, PRESENT-TENSE immersive reenactment
that puts the viewer directly inside the moment as it happens ("POV" framing). Never diagnose or
name a real, identifiable person; scenarios are always generic/composite ("your partner", "your
coworker", "a friend"), never a celebrity or public figure.

Structure is a DELAYED-REVEAL arc -- the psychological concept is NEVER named until the final 1-2
lines. Everything before that stays purely inside the lived moment: sensory, immediate, no
meta-commentary, no hint yet that this is "about psychology."

hook: ONE present-tense, second-person line dropping the viewer straight into the moment, ideally
literally starting with "POV:" or an equivalent immersive framing (e.g. "POV: you're mid-argument
and somehow you're the one apologizing."). Concrete and visual, zero explanation yet.

body: 3-5 lines forming a single continuous arc, not a list of separate facts:
  - Line 1-2: stay INSIDE the moment, no labels yet -- what you're doing, feeling, noticing,
    escalating the tension or confusion of the scene itself.
  - The pivot line: the turn where the video steps back and NAMES the real psychological concept
    at play for the first time (must be a REAL, established concept -- e.g. "the fawn response",
    "confirmation bias", "the mere exposure effect") -- this is the "aha", it should land like a
    reveal, not a definition.
  - Final line(s): the mechanism in plain language and the concrete implication for the viewer's
    own life -- keep to ONE clear takeaway, do not stack multiple ideas.

cta: a short self-recognition invite (not a generic comment prompt) -- e.g. "Tell me I'm not the
only one." / "If you know, you know." Never a book/product mention -- MindFile sells nothing.

visual_keywords: 5-8 REAL-LIFE B-roll search terms (people talking, office scene, couple
conversation, crowd walking, close-up facial expression, city street candid) -- never abstract
brain/neuron stock imagery, this format is about human behavior in everyday settings.
music_style: subtle, curious, low-key ambient or soft cinematic -- never lyrics, never
attention-competing.
Duration: 30-45 seconds.""",

    "mf2": """Format: MindFile Decode (no voice-over, text-on-video/image quote cards).
A "decode this text/line" format -- one of the highest-performing native sub-genres for
psychology/relationship content, because it hands the viewer a real, concrete artifact to analyze
instead of an abstract claim. Displayed over striking real-life visuals (a phone screen glow, a
lone figure, rain on a window, someone looking at their phone, night city lights) -- introspective
and striking, not luxury/aspirational.

hook: ONE short, real, verbatim, ambiguous line or text message people actually send/hear, shown
IN QUOTES exactly as said (e.g. "\\"we're not really talking anymore\\"" or "\\"it's fine, don't worry
about it\\""). Must be a genuinely common, realistic phrase, not an invented shock line -- the
whole hook is "here's a real thing someone said to you or you said to someone."

body: 2-3 text cards max, each <= 12 words, DECODING the line -- reading its tone, timing, effort,
or subtext to reveal what's actually being communicated underneath it. Escalate from the surface
reading to the sharper, more specific real meaning. Minimal, almost forensic. No filler.

cta: omit (null) -- this format ends on the sharpest decoded line, which itself functions as
organic share-bait (viewers tag/send it to whoever says that line to them). include_book_cta must
always be false.

visual_keywords: 5-8 moody, human-centric B-roll terms (phone screen close-up, texting in the
dark, solitary figure, rain window, night walk, staring at phone) -- not luxury cars/watches.
music_style: moody minimal ambient or slow cinematic, no lyrics.
Duration: 15-25 seconds.""",

    "mf3": """Format: MindFile Which One Are You (voice-over + dark B-roll + numbered countdown).
A self-diagnostic TYPOLOGY, not a "watch out for these people in your life" warning list --
research on this niche consistently shows first-person self-insertion framing ("which one are
you") outperforms third-person vigilance framing ("avoid these types") for engagement, because it
makes the viewer the subject of the analysis instead of a spectator cataloguing other people.
E.g. "5 ways people cope with rejection. Which one is you?", "6 attachment responses under
stress. Where do you land?". Vary item count (typically 4-8) and category run to run.

Hook (2 sentences): sentence 1 states the typology premise as a personal question, not a warning
("{N} ways people shut down after rejection. Which one is you?"). Sentence 2 is a retention hook
giving a reason to stay to the end -- vary wording each time (e.g. "most people are a mix of two",
"the last one is the one nobody admits to"), never reuse the same line verbatim across videos.

Body: exactly N lines, one per type, each starting with "Number {i}: " followed by a short label
for the TYPE (not a "sign to watch for"), then ONE tight sentence describing the pattern from the
INSIDE (how it feels to be this type, not how to spot it in someone else) that ALSO names the real
psychological mechanism where it fits naturally (attachment style, fawn/fight/flight/freeze,
cognitive dissonance, etc.) -- fold the mechanism into the same sentence as the feeling, don't add
a separate explanatory sentence for it. HARD CAP: 25 words per line MAXIMUM, including "Number {i}:
{Label}." -- this is spoken TTS at short-form pace, not a paragraph; if a line needs more than 25
words to land, cut detail until it fits, don't run long. Every few items (not every single one),
a normalizing 3-6 word tag can close the line (e.g. "Not broken. Wired to survive.") ONLY if it
still fits inside the same 25-word cap -- skip it entirely on lines where it wouldn't fit rather
than exceeding the cap. Number in DESCENDING order, N down to 1 (e.g. 6-item list: "Number 6:",
"Number 5:", ..., "Number 1:"). NEVER count upward. "Number 1" is the type fewest people admit to
out loud, landing last as the retention hook's payoff.

cta: a direct self-diagnosis invite, e.g. "Which number is you? Say it in the comments." Never a
book/product mention -- MindFile sells nothing, pure value only.

DURATION MATH (this format is spoken TTS -- respect this or the video runs way too long): at
natural spoken pace, 45-75 seconds is roughly 105-190 words TOTAL across hook + every body line +
cta combined. Pick N (item count) so that N lines at up to 25 words each, plus the ~35-word hook
and ~12-word cta, stays inside that total -- e.g. 5-6 items at 20 words average fits easily, 8
items only works if most lines run closer to 12-15 words. Count words before finalizing; if the
total is over ~190 words, cut lines down, don't add more items instead.

visual_keywords: 5-8 keywords for DARK-aesthetic real-life B-roll (dim rooms, night streets,
silhouettes, moody low-light interiors, rain, empty corridors, solitary figure) -- mix it up
between videos so consecutive uploads don't look identical.
music_style: dark cinematic, tense ambient, or ominous instrumental, no lyrics.
Duration: 45-75 seconds depending on item count -- see DURATION MATH above, this is a hard target
not a suggestion.""",

    "mf5": """Format: MindFile Two Minds (two TTS voices -- IMPULSE, the fast/emotional reactive
voice, vs REASON, the slow/logical deliberate voice -- arguing out a real, relatable everyday
decision or reaction). NO B-roll, NO music -- the two contrasting voices carry the whole video,
illustrating System 1 vs System 2 thinking (fast/automatic vs slow/deliberate cognition) through
a concrete scene instead of explaining the theory abstractly. Six alternating turns total:
impulse, reason, impulse, reason, impulse, reason.

The "subject" is the everyday situation being argued out (e.g. "replying to a text that made you
angry", "buying something the moment you see a sale", "confronting a friend who was rude") --
pick a fresh, specific, universally relatable scenario if the subject given is vague or empty.

hook: IMPULSE's opening line -- the immediate, emotional, reactive urge in this situation,
one spoken sentence, under 16 words, natural spoken tone (contractions ok).

body: EXACTLY 4 lines, ALTERNATING starting with REASON (position in the array marks who's
speaking -- a rendering step maps body[0]/body[2] to REASON and body[1]/body[3] to IMPULSE,
entirely outside this JSON. DO NOT write "Impulse:"/"Reason:" or any speaker label INSIDE the
text -- every line is fed directly to TTS):
  - body[0] (REASON): pushes back with the calmer, slower read on the situation -- not
    preachy, genuinely reasonable, specific to the scenario.
  - body[1] (IMPULSE): doubles down or pushes back, still emotionally driven, specific detail.
  - body[2] (REASON): names the actual mechanism/cost of following the impulsive path here,
    escalating specificity from body[0].
  - body[3] (IMPULSE): a real moment of hesitation or grudging acknowledgment -- still in
    character, not a full surrender.
Each line ONE short sentence, under 18 words, natural spoken rhythm -- total spoken length must
land in the 20-30s duration_target.

cta: spoken by REASON -- the closing synthesis naming the real lesson (a genuine behavioral-
science insight, e.g. naming System 1 vs System 2 thinking, the pause-before-you-act principle)
in one punchy spoken sentence. No speaker label in the text. Never a book/product mention.

visual_keywords: 3-4 REAL-LIFE B-roll search terms for a moody backdrop behind the two voices
(e.g. "person hesitating phone in hand", "staring out window undecided", "pacing room at night")
-- concrete, real scenes, not abstract mood words.
music_style: a low-key tension-building instrumental bed under the dialogue, no lyrics -- the two
voices still carry the video, music is texture only, never competing for attention.
Duration: 20-30 seconds (6 spoken turns).""",

    "mf6": """Format: MindFile The Exchange (quick POV hook card, then a simulated text-message
thread, no voice, no B-roll). A REAL back-and-forth text conversation between two people, building
bubble by bubble like an actual chat thread, dramatizing ONE specific psychological/relational
tactic playing out live -- not a single message analyzed (that's mf2/"Decode"), a full escalating
EXCHANGE. Ends on a distinct reveal card (not a chat bubble) naming the real mechanism, the
"screenshot this" payoff.

Pick a real, common dynamic (gaslighting, guilt-tripping, breadcrumbing, DARVO, silent treatment,
love bombing, negging, the fawn response in a text argument, etc.) and write a natural, specific
exchange that only makes sense once you see the reveal -- not obviously villainous from message 1,
the kind of exchange that reads as normal-ish until it's named.

pov_hook: a SEPARATE opening line, shown alone as its own quick title card BEFORE the phone/thread
appears -- literal TikTok "POV:" format, one short punchy sentence framing the situation without
giving away the reveal, e.g. "POV: you finally notice the pattern in his texts." Under 14 words.

hook: THEIR opening message (left/received bubble, shown once the phone thread starts right after
the pov_hook card) -- casual, natural texting voice, the line that kicks off the exchange.
Lowercase/casual is fine, real texting cadence, not a full sentence with perfect grammar.

body: EXACTLY 6 lines, ALTERNATING starting with YOU (position marks the side -- a rendering step
maps body[0]/body[2]/body[4] to YOU/right/sent and body[1]/body[3]/body[5] to THEM/left/received,
entirely outside this JSON. Do not write "You:"/"Them:" or any label inside the text -- every line
renders as a raw chat bubble):
  - body[0] (YOU): your natural response to the hook.
  - body[1] (THEM): pushes the dynamic one step further -- specific, in-character, not yet
    obviously alarming.
  - body[2] (YOU): a natural reaction -- confusion, pushback, or accommodation, whichever fits
    this exchange's realistic flow.
  - body[3] (THEM): escalates the tactic again, still sounding plausible/deniable in isolation.
  - body[4] (YOU): the moment something feels off -- still natural texting voice, not yet naming
    the pattern.
  - body[5] (THEM): the line that, once you see the reveal, unmistakably shows the tactic.
Each line under 20 words, real texting voice (contractions, lowercase where natural, no
over-explaining) -- these are actual text messages, not narration.

cta: the REVEAL -- shown as a distinct on-screen card after the thread, not a chat bubble. Name the
real tactic (e.g. "This is called breadcrumbing.") then ONE sentence on the actual mechanism, under
20 words total. Never a book/product mention -- MindFile sells nothing.

visual_keywords: not used for footage search here -- still return 3-5 mood words (e.g. "phone
screen dark tension realistic").
music_style: a very low-key, quiet tension-building instrumental, no lyrics -- this is a reading
format, music must stay unobtrusive under the text, never competing for attention.
Duration: pacing is adaptive (each bubble holds as long as it takes to read, not a fixed timer) --
expect roughly 25-40 seconds total (pov_hook card + 7 thread beats + reveal card), this is NOT a
fixed target to write toward, keep every line's own word count tight per the caps above instead.""",
}

_SYSTEM_PROMPT_FORMAT6 = """You are a short-form video script writer for viral ranking/listicle videos
about people, habits, mindset, and self-improvement (NOT finance or wealth).
You write for TikTok, YouTube Shorts, and Instagram Reels (vertical 9:16 format).
IMPORTANT: Write ALL text (hook, body, cta, description, hashtags) in English only.

Core voice:
- Punchy, sharp, slightly ominous storytelling tone that builds tension toward the final item
- Specific and vivid over vague generic descriptions
- Never use em dashes (--) in output text. Use commas or short sentences instead.
- Vary structure, item count, and retention-hook phrasing between videos — never repeat the same
  wording, category, or item count as a prior video."""

_SYSTEM_PROMPT = """You are a short-form video script writer for the Money & Wealth niche.
Your content is punchy, specific, and motivational. You write for TikTok, YouTube Shorts, and Instagram Reels (vertical 9:16 format).
IMPORTANT: Write ALL text (hook, body, cta, description, hashtags) in English only.

Core brand voice:
- Punchy, no fluff, every word earns its place
- Specific over vague ("save 20% each month" not "save money")
- Reference the 90-Day Advisor method when it fits naturally
- Never use em dashes (--) in output text. Use commas or short sentences instead.
- Hooks must stop the scroll in under 2 seconds
- Vary hook structures: question / bold claim / shocking stat / POV / challenge

The 90-Day Advisor is an ebook sold on Gumroad. It gives financial advisors and wealth builders a 90-day implementation system to build a million-dollar practice. When including a book CTA, tie it organically to the video content."""

_SYSTEM_PROMPT_MINDFILE = """You are a short-form video script writer for MindFile, a channel about applied
psychology and human behavior -- cognitive biases, relationship and workplace dynamics, manipulation
tactics, communication patterns, decision-making. You write for TikTok, YouTube Shorts, and Instagram
Reels (vertical 9:16), for a broad, ALL-AGES audience (teens through older adults), not a niche
subculture. IMPORTANT: Write ALL text (hook, body, cta, description, hashtags) in English only.

Core brand voice:
- Grounded in REAL, established psychological concepts and research -- never invent a fake study,
  statistic, or "scientists say" claim. If unsure a concept is real, use a more general but genuine one.
- Specific and concrete over vague ("your brain treats a text left on read as a small social
  rejection" not "texting affects your feelings")
- Never diagnose, mock, or reference a real identifiable public figure or private individual --
  scenarios are always generic/composite ("a coworker", "your partner", "a friend").
- MindFile sells no book or product -- never include any product/book/course CTA, ever.
- Never use em dashes (--) in output text. Use commas or short sentences instead.
- Hooks must stop the scroll in under 2 seconds by feeling uncomfortably specific/relatable.
- Vary hook structures across videos: scenario / question / bold claim / contrast."""

_MINDFILE_FORMATS = ("mf1", "mf2", "mf3", "mf5", "mf6")


# ── Generic brand-factory niche support (2026-08-12) ───────────────────────
# ResetWorth and MindFile each got their own hand-written system prompt
# above -- fine for two brands, not something worth repeating by hand for
# every future one. Any brand whose config.json carries a "niche" block
# (written by modules/brand_factory.py) gets a system prompt built from that
# data instead, for every format id it shares with ResetWorth (format1,
# format3, format9...). This is purely additive: ResetWorth/MindFile's own
# config.json never has a "niche" key, so _active_niche_config() returns
# None for them and every line below is a no-op -- their prompts, CTA
# gating, and hook history stay byte-identical to before this change.

def _active_niche_config() -> dict | None:
    """The active brand's own "niche" block, if any. See module note above."""
    try:
        ctx = users.get_user()
    except Exception:
        return None
    return (ctx.config or {}).get("niche") or None


def _build_generic_niche_prompt(niche: dict) -> str:
    label = niche.get("label", "this channel's niche")
    voice = niche.get("voice", "").strip() or (
        "- Punchy, specific, no fluff -- every word earns its place.\n"
        "- Vary hook structures between videos: question / bold claim / POV / contrast."
    )
    book = niche.get("book")
    book_line = (
        f'\n\n"{book.get("title", "")}" is an ebook sold on Gumroad, about '
        f'{book.get("angle", "")}. When including a book CTA, tie it organically to the '
        "video content."
        if book else
        "\nThis channel sells no book or product -- never include any product/book/course CTA, ever."
    )
    return (
        f"You are a short-form video script writer for a channel about {label}. "
        "You write for TikTok, YouTube Shorts, and Instagram Reels (vertical 9:16 format).\n"
        "IMPORTANT: Write ALL text (hook, body, cta, description, hashtags) in English only.\n\n"
        f"Core brand voice:\n{voice}\n"
        "- Never use em dashes (--) in output text. Use commas or short sentences instead.\n"
        "- Hooks must stop the scroll in under 2 seconds.\n"
        "- Vary hook structures and phrasing between videos -- never repeat the same wording twice."
        f"{book_line}"
    )

_HOOK_ANGLES = [
    "Open with a counterintuitive claim that challenges mainstream money advice.",
    "Open from the POV of someone who already achieved wealth, looking back at a past mistake.",
    "Lead with a specific number, dollar amount, or percentage as the very first element.",
    "Open with a sharp contrast between two types of people and two different outcomes.",
    "Directly provoke the viewer ('You've been doing this wrong.' / 'Stop doing X.').",
    "Open with a single vivid scene ('Imagine waking up to...' / 'Picture this...').",
    "State the conclusion first, then make the viewer want the explanation.",
    "Open with a time-bound claim ('In 90 days...' / 'By the time you're 35...').",
    "Open with a loss framing ('Every day you don't do X, you lose Y.').",
    "Open with a simple question that reframes how the viewer sees money entirely.",
]

# format3's own shape rotation -- picked ONE per call and injected as a
# mandatory instruction (same mechanism as _HOOK_ANGLES above), because each
# generate_script call is stateless: telling the model in prose to "rotate
# shapes run to run" does nothing when it has no memory of what shape the
# previous call used. Forcing the shape externally is the only way a batch
# actually comes out varied instead of defaulting to the same declarative
# metaphor sentence every time.
_FORMAT3_SHAPES = [
    "A short bare imperative/motto, 2-5 words, no metaphor, ABSOLUTE and final -- "
    "not a hedge, not tied to a specific excuse or condition (BANNED as a crutch: "
    "\"___ or stay broke\", it's already overused). Real examples of the bar to hit: "
    "\"Win or WIN.\", \"Do or die.\", \"No mercy. No days off.\", \"Dominate.\", "
    "\"Never flinch. Never fold.\", \"All gas, no brakes.\" -- short like a chant, "
    "something you'd shout, not a clever little rule.",
    "\"Me and bro\" loyalty-duo framing -- two people grinding together, not "
    "solo (like \"Me and bro aren't stopping till we get this.\", \"Don't quit "
    "bro, you already know too much.\").",
    "Hype-man energy directed at a training partner/bro, second person "
    "(like \"One more rep, bro, I got you.\", \"We're not quitting today, bro.\").",
    "A blunt insult/put-down aimed at softness, or a direct dismissal of what "
    "other people think of you (like \"Don't be a p*ssy, lock in.\", \"Let "
    "them talk behind your back.\", \"F*ck your feelings, go get the bag.\").",
    "A first-person violent OR mortality-stakes declaration "
    "(like \"I don't save money, I unleash it like a weapon.\", \"Try again or "
    "die.\", \"Be a winner until you die.\").",
    "Pain-as-fuel philosophy -- suffering framed as the actual price of winning, "
    "not something to avoid (like \"Enjoy the pain, it's the way to gain.\", "
    "\"If pain is the price for becoming the best, then suffer.\", \"I need "
    "more pain for victory.\").",
    "Absolute ego / never-lose declaration -- pure unshakeable self-belief, no "
    "hedge (like \"Even at my lowest I'm better than you.\", \"I never lose.\", "
    "\"My ego never dies.\", \"If I lose, it won't stop me.\").",
    "A dark twisted-wisdom paradox or proverb about winning, losing, or "
    "betrayal -- reads like an old saying with a brutal edge, not a hype line "
    "(like \"Winners are the biggest losers.\", \"Comfort kills your "
    "potential.\", \"Being 2nd means being the first to lose.\", \"Even "
    "paradise demands death.\").",
]

_JSON_SCHEMA = """{
  "format": "<format id>",
  "subject": "<subject>",
  "hook": "<1-2 sentences max, punchy, stops scroll>",
  "body": ["<line 1>", "<line 2>", "..."],
  "cta": "<spoken CTA or null>",
  "include_book_cta": <true|false>,
  "visual_keywords": ["<keyword 1>", "..."],
  "music_style": "<style>",
  "duration_target": <seconds as integer>,
  "description": "<posting caption, 1-3 sentences, no em dashes>",
  "hashtags": ["#tag1", "..."],
  "illusion_labels": ["<word 1>", "<word 2>"],
  "suspect_name": "<format14 ONLY -- character's name; omit entirely for every other format>",
  "suspect_voice": {"gender": "male|female", "age_register": "young|adult|mature", "accent": "american|british|australian|irish|indian|canadian|south_african", "tone": ["<tag>", "..."]},
  "pov_hook": "<format14/format15/mf6 ONLY -- 'POV: ...' opening card line; omit entirely for every other format>",
  "char_a_name": "<format15 ONLY -- character A's name>",
  "char_a_context": "<format15 ONLY -- character A's context ONLY, e.g. 'The Office'>",
  "char_a_voice": {"gender": "male|female", "age_register": "young|adult|mature", "accent": "american|british|australian|irish|indian|canadian|south_african", "tone": ["<tag>", "..."]},
  "char_b_name": "<format15 ONLY -- character B's name>",
  "char_b_context": "<format15 ONLY -- character B's context ONLY, e.g. 'Breaking Bad'>",
  "char_b_voice": {"gender": "male|female", "age_register": "young|adult|mature", "accent": "american|british|australian|irish|indian|canadian|south_african", "tone": ["<tag>", "..."]},
  "rounds": [{"a_line": "<...>", "b_line": "<...>", "round_winner": "a|b"}],
  "winner": "<format15 ONLY -- 'a' or 'b'>",
  "winner_line": "<format15 ONLY -- announcer's winner declaration>",
  "loser_line": "<format15 ONLY -- losing character's final line>",
  "recut_quote": "<format3 hook-RECUT mode ONLY, requested explicitly below when active -- a second, DISTINCT quote for the video's second content segment; omit entirely otherwise>"
}
(illusion_labels: format8 ONLY. suspect_name/suspect_voice: format14 ONLY. char_a_name/char_a_context/char_a_voice/char_b_name/char_b_context/char_b_voice:
format15 AND format16. rounds/winner/winner_line/loser_line: format15 ONLY. pov_hook: format14 AND format15 (NOT format16 -- it opens straight on the call)
AND mf6 (a quick "POV: ..." card shown BEFORE the text thread starts, NOT the thread's own opening message -- that's still the separate "hook" field).
Omit every key not tagged for the CURRENT format entirely -- never include as null/empty.)"""


def _load_past_hooks(n: int = 25, fmt: str | None = None) -> list[str]:
    """
    Return hook strings from the N most recently generated scripts.
    fmt, if given, restricts to that format only (e.g. format6's own past
    hooks -- mixing in wealth-niche hooks from other formats as "avoid
    repeating" context is noise, not signal).
    """
    scripts_dir = _SCRIPTS_DIR
    if not scripts_dir.exists():
        return []
    pattern = f"*_{fmt}_*.json" if fmt else "*.json"
    files = sorted(scripts_dir.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    hooks: list[str] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            h = data.get("hook", "").strip()
            if h:
                hooks.append(h)
        except Exception:
            continue
    return hooks


def _load_past_characters(fmt: str, fields: list[str], n: int = 15) -> list[str]:
    """Character name field(s) (e.g. suspect_name, or char_a_name/char_b_name)
    from the N most recent scripts of `fmt` -- used so an auto-picked
    character (empty subject) doesn't repeat what the last few videos
    already used."""
    scripts_dir = _SCRIPTS_DIR
    if not scripts_dir.exists():
        return []
    files = sorted(scripts_dir.glob(f"*_{fmt}_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    names: list[str] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for field in fields:
                name = (data.get(field) or "").strip()
                if name:
                    names.append(name)
        except Exception:
            continue
    return names


def _build_user_prompt(
    fmt: str, subject: str, include_book_cta: bool,
    past_hooks: list[str] | None = None, past_characters: list[str] | None = None,
    recut: bool = False,
) -> str:
    fmt_name         = FORMATS.get(fmt, fmt)
    fmt_instructions = _FORMAT_INSTRUCTIONS.get(fmt, "")
    # format6's own instructions already fully specify the hook structure
    # (premise + varying retention line) — the money-niche hook angles below
    # don't apply to its people/habits subject matter. format8's hook has a
    # fixed job (the "naive belief" framing paired with the naive-line
    # graphic) that a generic "lead with a stat" angle would fight against.
    # format3 has its OWN dedicated shape rotation (_FORMAT3_SHAPES below) --
    # the generic angles here (one of which is literally "In 90 days...", a
    # pattern the user explicitly flagged as tired) were silently overriding
    # it since both are injected as "mandatory".
    no_generic_hook  = fmt in ("format3", "format3a", "format3b", "format6", "format8", "format9", "format10", "format11", "format12", "format14", "format15", "format16") + _MINDFILE_FORMATS
    hook_angle       = "" if no_generic_hook else random.choice(_HOOK_ANGLES)
    hook_angle_line  = "" if no_generic_hook else f"\nHook angle (mandatory — must shape the opening): {hook_angle}"
    if fmt in ("format3", "format3a", "format3b"):
        hook_shape = random.choice(_FORMAT3_SHAPES)
        hook_angle_line = (
            f"\nHook SHAPE (mandatory — the hook must use this exact shape): {hook_shape}\n"
            "The line must be a real, coherent sentence a person would actually say out loud — read it back "
            "to yourself literally, not just for vibe. Do NOT blend in a second shape's flavor on top of "
            "the one above (e.g. don't bolt a mortality-stakes twist onto a \"me and bro\" line) — that's "
            "what produces broken non-sequiturs like \"Me and bro die before we die broke.\", which doesn't "
            "parse (you can't die before you die). One shape, said cleanly, beats two shapes mashed together."
        )
        if recut:
            # format3 hook-RECUT structure only (see assemble_with_hook_recut) --
            # a second content segment needs its OWN quote, distinct in both
            # wording and SHAPE from the hook above (same variety principle
            # _FORMAT3_SHAPES already enforces run-to-run, just within one call).
            recut_shape = random.choice([s for s in _FORMAT3_SHAPES if s != hook_shape])
            hook_angle_line += (
                f"\n\nAlso generate a \"recut_quote\" field: a SECOND, DISTINCT quote for this video's "
                f"second content segment (shown later, after a cutaway back to the hook footage). "
                f"Different SHAPE from the hook — use this one: {recut_shape}\n"
                "Same rule applies: a real, coherent sentence, one shape only, no blending. It must NOT "
                "repeat the hook's wording, topic angle, or sentence structure."
            )
    hashtag_rule     = (
        "hashtags: mix of broad and niche tags relevant to this video's specific ranking topic"
        if fmt == "format6"
        else "hashtags: mix of broad and niche psychology/human-behavior tags relevant to this video"
        if fmt in _MINDFILE_FORMATS
        else "hashtags: mix of broad (#wealth #money) and niche (#90dayadvisor #financialfreedom)"
    )
    cta_instruction  = (
        "Include a book CTA at the END only (1-2 seconds spoken). "
        "Tie it organically to the video content. Example: "
        "'The full 90-day system is in bio.' or "
        "'I break this down step by step in The 90-Day Advisor, link in bio.'"
        if include_book_cta
        else "Do NOT include any book or product mention. Pure value only."
    )

    avoid_block = ""
    if past_hooks:
        lines = "\n".join(f"  - {h}" for h in past_hooks[:20])
        avoid_block = f"""
ALREADY USED — DO NOT REPEAT OR ECHO THESE HOOKS (different phrasing, angle, structure required):
{lines}

Use a UNIQUE angle/phrasing COMPLETELY DIFFERENT from every line above.
"""

    if subject.strip():
        subject_block = f"Subject: {subject}"
    else:
        # Empty subject on format14/format15 = auto-pick mode: Claude invents
        # the character(s) itself (FlowShort app's "random character" button).
        avoid_chars = ""
        if past_characters:
            avoid_chars = (
                "\nAlready used recently — pick someone DIFFERENT: "
                + ", ".join(past_characters[:15])
            )
        if fmt == "format15":
            subject_block = (
                "Subject: NOT GIVEN -- pick your own TWO DIFFERENT well-known fictional "
                "characters yourself (anime, cartoon, movie, TV series, video game, book, "
                "literally anything with a famous, specific, identifiable character), picked "
                "for a genuinely funny financial mismatch, varied every time. Set the JSON's "
                "\"subject\" field to \"<CharacterA> / <contextA> vs <CharacterB> / <contextB>\" "
                "and \"char_a_name\"/\"char_a_context\"/\"char_b_name\"/\"char_b_context\" to each "
                f"character's name and context separately -- these are NOT pre-filled for you here, you choose them.{avoid_chars}"
            )
        elif fmt == "format16":
            subject_block = (
                "Subject: NOT GIVEN -- pick your own TWO DIFFERENT well-known fictional "
                "characters yourself (anime, cartoon, movie, TV series, video game, book, "
                "literally anything with a famous, specific, identifiable character) from TWO "
                "UNRELATED universes, picked for the biggest/funniest crossover gap, varied "
                "every time. Set the JSON's \"subject\" field to \"<CharacterA> / <contextA> "
                "calls <CharacterB> / <contextB>\" and \"char_a_name\"/\"char_a_context\"/"
                "\"char_b_name\"/\"char_b_context\" to each character's name and context "
                "separately -- CharacterA is the panicked caller, CharacterB is the calm "
                f"advisor -- these are NOT pre-filled for you here, you choose them.{avoid_chars}"
            )
        else:
            subject_block = (
                "Subject: NOT GIVEN -- pick your own well-known fictional character yourself "
                "(anime, cartoon, movie, TV series, video game, book, literally anything with "
                "a famous, specific, identifiable character), varied every time. Set the JSON's "
                "\"subject\" field to \"<Character> / <context>\" (same convention as the examples "
                "above) and \"suspect_name\" to the character's name -- these are NOT pre-filled "
                f"for you here, you choose them.{avoid_chars}"
            )

    return f"""Generate a short-form video script.

Format: {fmt} ({fmt_name})
{fmt_instructions}

{subject_block}
{hook_angle_line}
{avoid_block}
CTA rule: {cta_instruction}

Output ONLY valid JSON matching this exact schema:
{_JSON_SCHEMA}

Rules:
- No em dashes anywhere in output text
- visual_keywords should be specific enough for stock footage search (e.g. "luxury sports car driving" not just "car")
- {hashtag_rule}
- description ends with a question or a strong statement to boost comments
- body lines are each independently readable as text cards on screen"""


_DUEL_CHAR_PICK_SCHEMA = (
    '{"char_a_name": "<name>", "char_a_context": "<show/movie/game/etc>", '
    '"char_b_name": "<name>", "char_b_context": "<show/movie/game/etc>"}'
)


def pick_duel_characters(
    fmt: str,
    past_characters: list[str] | None = None,
    fix_a: dict | None = None,
    fix_b: dict | None = None,
) -> dict:
    """Pick 2 fictional characters for format15 (Duel Financier) / format16
    (Téléphone Rouge) WITHOUT writing any dialogue yet -- a separate, cheaper
    step ahead of generate_script's full script generation, so the "Aléatoire"
    wizard can show the 2 names first and let the user reroll just one of
    them before committing to writing the actual script (2026-08-09 user ask:
    "je veux un bouton à côté du blase de chaque perso pour le reload").

    fix_a / fix_b: {"name":..., "context":...} -- pass exactly one (not both)
    to keep that side exactly as given and only have the model pick the
    other. Passing neither picks both fresh.

    Returns {char_a_name, char_a_context, char_b_name, char_b_context} --
    NOT a full script, no _script_path, nothing saved to disk yet.
    """
    if fmt not in ("format15", "format16"):
        raise ValueError(f"pick_duel_characters: unsupported format {fmt!r}")
    if fix_a and fix_b:
        raise ValueError("pick_duel_characters: pass at most one of fix_a/fix_b")

    avoid_block = ""
    if past_characters:
        avoid_block = "\nAlready used recently -- pick someone DIFFERENT: " + ", ".join(past_characters[:15])

    fixed_block = ""
    if fix_a:
        fixed_block = (f"\nCharacter A is ALREADY FIXED: {fix_a['name']} ({fix_a.get('context', '')}) "
                        "-- do NOT change them, only pick Character B.")
    elif fix_b:
        fixed_block = (f"\nCharacter B is ALREADY FIXED: {fix_b['name']} ({fix_b.get('context', '')}) "
                        "-- do NOT change them, only pick Character A.")

    pairing = (
        "a genuinely funny FINANCIAL mismatch (spending habits, net worth, money mindset)" if fmt == "format15"
        else "the biggest/funniest crossover gap for a panicked phone call (Character A panics, Character B stays calm)"
    )

    user_prompt = (
        "Pick TWO DIFFERENT well-known fictional characters (anime, cartoon, movie, TV "
        "series, video game, book -- literally anything with a famous, specific, "
        f"identifiable character), picked for {pairing}, varied every time."
        f"{fixed_block}{avoid_block}\n\n"
        f"Output ONLY valid JSON matching this exact schema:\n{_DUEL_CHAR_PICK_SCHEMA}"
    )
    raw, input_tokens, output_tokens = call_claude(
        "You invent character pairings for a short-form video format. Output ONLY the requested JSON, nothing else.",
        user_prompt, model=CLAUDE_MODEL, timeout=60,
    )
    picked = _parse_json(raw.strip())
    # A fixed side must survive verbatim -- don't trust the model to just echo it back correctly.
    if fix_a:
        picked["char_a_name"], picked["char_a_context"] = fix_a["name"], fix_a.get("context", "")
    if fix_b:
        picked["char_b_name"], picked["char_b_context"] = fix_b["name"], fix_b.get("context", "")
    return picked


def generate_script(fmt: str, subject: str, recut: bool = False) -> dict:
    """
    Generate a structured video script via Claude.

    Args:
        fmt: one of format1-format5
        subject: the topic/angle for this video
        recut: format3 hook-RECUT structure only (see assemble_with_hook_recut) --
            also requests a second "recut_quote" field for the video's second
            content segment. Ignored for every other format.

    Returns:
        parsed script dict, also saved to output/scripts/
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format '{fmt}'. Valid: {list(FORMATS.keys())}")

    # format17 has its own schema entirely (news items + bons plans + a
    # pre-formatted description, grounded in real fetched headlines, not the
    # generic hook/body/cta shape) -- fully separate generation path, same
    # idea as generate_meta_long being split out from generate_meta.
    if fmt == "format17":
        return _generate_format17_script()

    # long1 ("L'Autopsie Financiere") has its own schema entirely (4 chaptered
    # sections of narration beats + a random title-only narrator persona) --
    # this is a real-human-voice format, no TTS shape to fit.
    if fmt == "long1":
        return _generate_long1_script(subject)

    # long2 ("La Reunion Secrete") and long3 ("Le Simulateur de Ruine") are both
    # archetype-TTS long formats (never real human voice, never a real-actor
    # clone -- same voice_bank archetype approach as format14/15/16) -- fully
    # automatable end to end, no manual audio panel needed.
    if fmt == "long2":
        return _generate_long2_script(subject)
    if fmt == "long3":
        return _generate_long3_script(subject)

    # MindFile's own long-form ("Deep Dive") -- fully separate schema, same
    # reasoning as long1/long2/long3 above.
    if fmt == "mf_long":
        return _generate_mf_long_script(subject)

    # A generic brand-factory brand (see _active_niche_config note above) never
    # gets an in-video baked CTA -- its own book promo, if any, is applied at
    # the caption/description layer instead (modules/content_meta.py), same
    # approach as MindFile's; checked here, before the ResetWorth-only global
    # should_include_book_cta() rotation counter, so a niche brand never
    # consumes/perturbs that shared counter.
    niche_cfg = _active_niche_config()

    # format6 is a different niche (people/habits, not wealth); format8's CTA slot
    # is always the kinetic "subscribe" line -- neither ever gets a book CTA.
    # MindFile (mf1-mf5) sells no product at all -- never a book CTA either.
    include_cta = (
        False if niche_cfg or fmt in ("format6", "format8", "format9", "format10", "format11", "format12", "format14", "format15", "format16") + _MINDFILE_FORMATS
        else should_include_book_cta()
    )
    # format8's hooks are all "naive belief" framings -- isolate its own history
    # too (same reasoning as format6, see _load_past_hooks docstring).
    past_hooks  = _load_past_hooks(fmt=fmt if fmt in ("format6", "format8", "format9", "format10", "format11", "format12", "format14", "format15", "format16") + _MINDFILE_FORMATS else None)
    # format14 with an empty subject = "pick a random character" mode (the
    # FlowShort app's dedicated format14 panel) -- avoid repeating whichever
    # characters the last few auto-picks already used.
    past_characters = None
    if not subject.strip():
        if fmt == "format14":
            past_characters = _load_past_characters("format14", ["suspect_name"])
        elif fmt == "format15":
            past_characters = _load_past_characters("format15", ["char_a_name", "char_b_name"])
        elif fmt == "format16":
            past_characters = _load_past_characters("format16", ["char_a_name", "char_b_name"])
    system_prompt = (
        _SYSTEM_PROMPT_FORMAT6 if fmt == "format6"
        else _SYSTEM_PROMPT_MINDFILE if fmt in _MINDFILE_FORMATS
        else _build_generic_niche_prompt(niche_cfg) if niche_cfg
        else _SYSTEM_PROMPT
    )

    logger.info(f"Generating script | format={fmt} | subject={subject!r} | book_cta={include_cta} | past_hooks={len(past_hooks)}")

    user_prompt = _build_user_prompt(
        fmt, subject, include_cta, past_hooks=past_hooks, past_characters=past_characters, recut=recut
    )
    raw, input_tokens, output_tokens = call_claude(system_prompt, user_prompt, model=CLAUDE_MODEL, timeout=300)
    raw = raw.strip()

    script = _parse_json(raw)
    script["format"] = fmt
    # format14 auto-pick mode: trust Claude's own "subject" choice instead of
    # overwriting it with the (empty) caller-supplied subject.
    if subject.strip() or not script.get("subject"):
        script["subject"] = subject
    script["include_book_cta"] = include_cta

    _save_script(script)
    # format6 can never carry the book CTA (different niche) -- don't let it
    # consume a slot in the rotation counter, or "every N videos" effectively
    # never fires for the formats that actually can (1/4 of the auto-mix
    # schedule is format6, which would land the rotation on an ineligible
    # video roughly every other cycle).
    if fmt not in ("format6", "format8", "format9", "format10", "format11", "format12", "format14", "format15", "format16"):
        increment(included_cta=include_cta)

    logger.info(f"Script saved | tokens_used={input_tokens + output_tokens}")
    return script


_FORMAT17_SYSTEM_PROMPT = (
    "You are a script generator for a short-form video format called "
    "\"Weekly Finance & Markets Roundup\". You work ONLY from the real news "
    "digest provided in the user prompt -- never invent a headline, number, "
    "company, or fact that isn't grounded in that digest, and never invent a "
    "fake finance tool/app/platform for the Bons Plans section either (only "
    "genuinely real, well-known ones). Write in English. Never use em dashes."
)


def _generate_format17_script() -> dict:
    """
    format17 ("Weekly Finance & Markets Roundup") -- always covers the
    CURRENT week, so unlike every other format it ignores any subject the
    caller passes in and derives its own date range. Grounded in real
    headlines fetched by modules.news_fetcher (RSS, no paid API) -- refuses
    to generate rather than let Claude fabricate financial news if that
    fetch comes back empty.
    """
    from datetime import datetime, timedelta

    from modules.news_fetcher import fetch_weekly_digest

    today = datetime.now()
    week_start = today - timedelta(days=7)
    date_range = f"{week_start.strftime('%b %d')} - {today.strftime('%b %d, %Y')}"

    digest = fetch_weekly_digest()
    if not digest.strip():
        raise RuntimeError(
            "format17: no real financial news could be fetched this week -- "
            "refusing to generate rather than fabricate news content"
        )

    prompt = f"""TASK:
Generate content covering THIS WEEK'S financial news and stock market highlights
(the most relevant stories from the past 7 days: markets, major stocks, macro/economic
data, crypto if relevant, notable company news), structured as a sequence of
image + short caption pairs, then a full detailed description.

REAL NEWS DIGEST FOR THIS WEEK ({date_range}) -- use ONLY facts from here, never invent:
{digest[:6000]}

STRUCTURE:

1. NEWS SECTION (Finance & Markets)
For each news item (aim for 4-6 items, grounded in the digest above):
- One short, punchy on-screen phrase (max 8-10 words) summarizing the financial news
- One relevant image search query per item -- STRONGLY PREFER a clean, recognizable
  COMPANY/BRAND LOGO ("Apple logo", "Nvidia logo") or a PHOTO OF THE SPECIFIC PERSON the
  story is about ("Jerome Powell portrait", "Elon Musk"). AVOID generic "stock chart" /
  "market chart" / "ticker" queries -- those return cluttered, low-quality screenshot
  images that look bad on screen. If the story has no single company/person to anchor on
  (e.g. a broad macro story), pick the closest recognizable brand/institution logo
  involved (e.g. "Federal Reserve seal", "S&P 500 logo") over an abstract chart.
- Do NOT put details in the on-screen phrase itself

2. DESCRIPTION (caption below the video)
- Open with: "This week's finance & markets news you need to know 👇"
- For EVERY news item shown above, write one line in this format:
  [Short phrase] (detailed explanation: context, numbers, source, why it matters)
- Keep each parenthetical detail to 1-2 sentences, informative but scannable, grounded
  in the digest above
- End the description with: Comment "GUIDE" and I'll DM you mine.
- Include 4-6 relevant hashtags after the CTA (finance-focused, e.g. #stockmarket
  #investing #finance)

TONE: Direct, informative, no fluff. English only. Written for a finance/motivation
audience (ResetWorth).

Return ONLY valid JSON:
{{
  "hook_text": "This week's finance & markets news",
  "hook_image_query": "<image search query for the opening card's background -- a real, recognizable finance/markets scene, e.g. 'New York Stock Exchange trading floor' or 'Wall Street sign', NOT an abstract chart>",
  "news_items": [{{"phrase": "<max 8-10 words>", "image_query": "<image search query>"}}, ...],
  "description": "<the FULL description text exactly per the structure above, as one string with real newline characters between lines>"
}}"""

    raw, input_tokens, output_tokens = call_claude(
        _FORMAT17_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL, timeout=180
    )
    parsed = _parse_json(raw.strip())

    script = {
        "format": "format17",
        "subject": f"Weekly Finance Roundup {date_range}",
        "hook_text": parsed.get("hook_text") or "This week's finance & markets news",
        "hook_image_query": parsed.get("hook_image_query") or "New York Stock Exchange trading floor",
        "news_items": [i for i in (parsed.get("news_items") or []) if i.get("phrase")],
        "description": (parsed.get("description") or "").strip(),
        "include_book_cta": False,
        "music_style": "minimal informative ambient, no lyrics",
    }
    _save_script(script)
    logger.info(f"format17 script saved | items={len(script['news_items'])} "
                f"| tokens_used={input_tokens + output_tokens}")
    return script


_LONG1_SYSTEM_PROMPT = (
    "You are a scriptwriter for a long-form YouTube financial-documentary series "
    "called \"L'Autopsie Financiere\" (The Financial Autopsy). You analyze REAL, "
    "well-documented cases of financial ruin or financial success (real companies, "
    "real public figures, real historical events) to extract genuine lessons in "
    "finance and psychology. Never invent a case, a person, a number, or an event -- "
    "only use real, widely-reported history; if you are not confident a detail is "
    "accurate, leave it out rather than guessing. This script will be read aloud by "
    "a REAL HUMAN narrator (not text-to-speech), so write natural spoken prose: full "
    "sentences, varied rhythm, contractions where natural -- not the short punchy "
    "fragments used for TTS-driven short-form content elsewhere on this channel. "
    "Write in English. Never use em dashes."
)


def _generate_long1_script(subject: str) -> dict:
    """
    long1 ("L'Autopsie Financiere") -- a 12-15 minute narrated documentary
    analyzing a REAL financial ruin/success case. Unlike every other format in
    this pipeline, the narration audio is the USER'S OWN REAL VOICE (recorded
    per section, see modules.assembler.long1's per-section audio matching) --
    never TTS, per explicit user requirement (long-form TTS narration "rend le
    tout robotique"). "narrator_persona" is a TITLE-ONLY hook/gimmick (e.g.
    "Explained by Warren Buffett") -- it is NEVER used to select or imitate a
    voice, real or synthesized; it's purely a clickbait-style title device.
    """
    case_line = (
        f'The case to analyze: "{subject}" -- research and use the REAL, documented facts of this case.'
        if subject.strip() else
        "Pick your own REAL, well-documented financial ruin or financial success case "
        "yourself (a real company collapse, a real public figure's bankruptcy or "
        "fortune, a real historical financial event) -- varied every time, genuinely "
        "well-known enough that the facts are verifiable."
    )

    prompt = f"""Generate a script for a 12-15 minute "Financial Autopsy" documentary episode.

{case_line}

Also pick a random WELL-KNOWN public figure's name (any domain -- business, entertainment,
sports, history, anything) to use as a TITLE HOOK ONLY, e.g. "Explained by Warren Buffett" --
this name is NEVER actually used as a voice or persona in the video itself, it's purely a
title/thumbnail gimmick to earn clicks. Pick someone UNRELATED to the case itself (not a
person who was actually involved in the case being analyzed).

STRUCTURE -- 4 chaptered sections, each a list of narration "beats" (each beat = one paragraph
the human narrator will read in a single breath, roughly 40-70 words, natural spoken prose):

1. HOOK / THE DRAMA (0-2 min, 3-4 beats, ~250-300 words total): open on the exact moment
   everything fell apart (or the exact moment of triumph, if it's a success story) -- concrete,
   dramatic, specific. Do NOT explain the backstory yet, just the moment and why it matters.

2. THE DOWNFALL / THE CLIMB (2-7 min, 8-10 beats, ~700-800 words total): the chronological story
   of the mistakes (or the decisions that built the fortune) -- specific numbers, specific dates,
   specific decisions. Weave in the underlying financial CONCEPT each one illustrates (e.g.
   leverage, inflation, opportunity cost, lifestyle creep) so the audience learns the mechanism,
   not just the anecdote.

3. THE PSYCHOLOGY OF THE TRAP (7-12 min, 8-10 beats, ~700-800 words total): why the human brain
   falls for this specific pattern -- name the actual cognitive biases at play (loss aversion,
   FOMO, sunk cost fallacy, overconfidence, social proof, whichever genuinely apply here) and
   explain the MECHANISM, not just the label. Ground every claim in the specifics of THIS case.

4. THE RESETWORTH ACTION PLAN (12-15 min, 5-6 beats, ~400-450 words total): concrete, specific
   rules or a simple framework the viewer can apply to their OWN finances based on this case's
   lesson -- not vague advice, actual actionable steps. End the final beat with a natural spoken
   subscribe call-to-action tied to the channel's identity (motivation / wealth-building mindset).

For EACH beat, also provide a real-world image search query -- a real photo/screenshot that
would visually support that beat (the person involved, the company logo, a relevant real photo
of the event/place, a magazine cover, etc.), never an abstract/generic stock concept.

Return ONLY valid JSON:
{{
  "case_subject": "<short name of the case>",
  "narrator_persona": "<random well-known figure's name, title hook only>",
  "title": "<YouTube title, max 100 characters, format: '<Case>: What Really Happened (Explained by <Persona>)'>",
  "sections": {{
    "hook": {{"label": "The Hook", "beats": [{{"text": "<one paragraph, 40-70 words>", "visual_query": "<real image search query>"}}, ...]}},
    "engrenage": {{"label": "The Downfall", "beats": [...]}},
    "psychologie": {{"label": "The Psychology of the Trap", "beats": [...]}},
    "plan_action": {{"label": "The ResetWorth Action Plan", "beats": [...]}}
  }},
  "description": "<YouTube description, 2-4 sentences, no em dashes, ends with a question or strong statement>",
  "hashtags": ["#tag1", ...]
}}"""

    # ~2500 words of structured JSON across 4 sections -- needs much more headroom
    # than the default short-form timeout, or a real (non-stuck) generation gets
    # killed and reported as a false failure.
    raw, input_tokens, output_tokens = call_claude(_LONG1_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL, timeout=480)
    parsed = _parse_json(raw.strip())

    case_subject = (parsed.get("case_subject") or subject or "Untitled Case").strip()
    persona = (parsed.get("narrator_persona") or "").strip()
    title = (parsed.get("title") or f"{case_subject}: What Really Happened").strip()[:100]

    sections = {}
    for sec_id in ("hook", "engrenage", "psychologie", "plan_action"):
        sec = (parsed.get("sections") or {}).get(sec_id) or {}
        beats = [b for b in (sec.get("beats") or []) if (b.get("text") or "").strip()]
        sections[sec_id] = {"label": sec.get("label") or sec_id, "beats": beats}

    script = {
        "format": "long1",
        "subject": case_subject,
        "case_subject": case_subject,
        "narrator_persona": persona,
        "title": title,
        "sections": sections,
        "description": (parsed.get("description") or "").strip(),
        "hashtags": [h for h in (parsed.get("hashtags") or []) if h],
        "include_book_cta": False,
        "music_style": "subtle cinematic documentary underscore, no lyrics",
    }
    _save_script(script)

    n_beats = sum(len(s["beats"]) for s in sections.values())
    logger.info(f"long1 script saved | case={case_subject!r} persona={persona!r} "
                f"beats={n_beats} | tokens_used={input_tokens + output_tokens}")
    return script


_LONG2_SYSTEM_PROMPT = (
    "You are a scriptwriter for a long-form YouTube series called \"La Reunion "
    "Secrete\" (The Secret Meeting) -- a fictional crossover where several famous "
    "pop-culture characters (movies, TV, anime, comics, games -- anything) meet "
    "at a dark, cinematic table to debate business strategy and philosophies of "
    "success. Each character is voiced by an ARCHETYPE text-to-speech voice "
    "matched to their vibe (gender, age register, accent, tone) -- NEVER an "
    "impression of the real actor/performer's own voice, just a plausible "
    "archetype for the character. Write in English. Never use em dashes. This "
    "will be read aloud by TTS, so keep sentences natural to speak, not overly "
    "long or dense."
)


def _generate_long2_script(subject: str) -> dict:
    """
    long2 ("La Reunion Secrete") -- 4 well-known fictional characters (auto-picked
    if subject is empty, otherwise user-specified) debate a business/survival
    topic around a dark cinematic table, each in their own archetype TTS voice
    (see voice_bank -- never a real-actor clone), bookended by a fixed narrator
    voice. Fully automatable -- unlike long1, no real human voice is required.
    """
    if subject.strip():
        cast_line = (
            f"The cast: {subject}. Use these characters (fill in their correct "
            "canonical context/franchise yourself if not given)."
        )
    else:
        cast_line = (
            "Pick your own FOUR different well-known fictional characters yourself "
            "(movies, TV, anime, comics, games -- anything with a famous, specific, "
            "identifiable character), chosen for a genuinely interesting philosophical "
            "clash on business/success, varied every time."
        )

    prompt = f"""Generate a script for a 15-18 minute "Secret Meeting" mastermind-crossover episode.

{cast_line}

Same franchise-accuracy requirement as similar formats on this channel: double-check which
show/movie/game/book each character is actually from before answering -- if you are not fully
certain, pick a different, more certain character instead.

STRUCTURE:

1. THEME: pick ONE concrete business/survival problem the four characters must reach an
   answer on together (e.g. "how to dominate a market in 2026", "how to survive an economic
   collapse", "how to build power that outlasts you"). Make it specific, not generic.

2. CHARACTERS: exactly 4, each with:
   - name, context (their franchise ONLY, e.g. "Peaky Blinders", "Marvel Cinematic Universe")
   - voice: archetype-only shape (gender, age_register: young/adult/mature, accent:
     american/british/australian/irish/indian/canadian/south_african, tone: 2-4 descriptive
     words for the character's vibe, e.g. ["cold", "calculating", "quiet menace"]) -- an
     archetype for the ROLE, never an impression of the real actor/performer.
   - philosophy: ONE short phrase naming their specific approach to power/success/money
     (e.g. "control through fear and discipline", "scale everything, trust no human process").
     Each of the 4 must be a GENUINELY DIFFERENT philosophy -- the clash is the point.

3. NARRATOR_INTRO: 3-5 sentences, cinematic and moody (rain, weight, tension), spoken by a
   neutral narrator, sets the scene and states the stakes -- who these four are and what they
   must agree on tonight. Roughly 150-200 words (1.5-2 minutes spoken).

4. ROUNDS: the actual debate -- a sequence of turns, EACH an object {{"speaker": "<character
   name exactly as given above>", "text": "<their line>"}}. NOT simple round-robin -- real
   debate structure: characters interrupt, directly challenge another character BY NAME on a
   specific weakness in their philosophy, build alliances, concede points. Each character
   speaks at least 5 times across the debate and must directly clash with at least 2 of the
   other 3 by name at some point. Roughly 26-30 turns total, each turn 2-4 sentences (not a
   whole speech at once -- this is a heated roundtable, not 4 monologues). Total roughly
   1900-2100 words across all rounds combined (about 11-13 minutes spoken).

5. NARRATOR_CONCLUSION: the narrator returns to synthesize the "Mastermind formula" -- the
   one underlying principle that reconciles (or transcends) all four philosophies, landing the
   channel's actual takeaway (motivation / wealth mindset / financial advice, whichever fits
   best). Roughly 300-400 words (2.5-3 minutes spoken), quotable and punchy, not a dry recap.

Every spoken line goes straight to TTS -- DO NOT write speaker labels inside any text field.

Return ONLY valid JSON:
{{
  "theme": "<the specific problem they must agree on>",
  "characters": [
    {{"name": "...", "context": "...", "voice": {{"gender": "male|female", "age_register": "young|adult|mature", "accent": "american|british|australian|irish|indian|canadian|south_african", "tone": ["...", "..."]}}, "philosophy": "..."}},
    ... exactly 4 total
  ],
  "narrator_intro": "...",
  "rounds": [{{"speaker": "<exact character name>", "text": "..."}}, ...],
  "narrator_conclusion": "...",
  "title": "<YouTube title, max 100 characters, punchy, names at least 2 of the characters>",
  "description": "<YouTube description, 2-4 sentences, no em dashes>",
  "hashtags": ["#tag1", ...]
}}"""

    raw, input_tokens, output_tokens = call_claude(_LONG2_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL, timeout=600)
    parsed = _parse_json(raw.strip())

    characters = [c for c in (parsed.get("characters") or []) if c.get("name")][:4]
    rounds = [r for r in (parsed.get("rounds") or []) if r.get("text") and r.get("speaker")]
    subject_out = subject.strip() or " / ".join(c["name"] for c in characters)

    script = {
        "format": "long2",
        "subject": subject_out,
        "theme": parsed.get("theme") or "",
        "characters": characters,
        "narrator_intro": (parsed.get("narrator_intro") or "").strip(),
        "rounds": rounds,
        "narrator_conclusion": (parsed.get("narrator_conclusion") or "").strip(),
        "title": (parsed.get("title") or subject_out)[:100],
        "description": (parsed.get("description") or "").strip(),
        "hashtags": [h for h in (parsed.get("hashtags") or []) if h],
        "include_book_cta": False,
        "music_style": "dark cinematic tension, heavy low strings, no lyrics",
    }
    _save_script(script)

    logger.info(f"long2 script saved | theme={script['theme']!r} "
                f"characters={[c['name'] for c in characters]} rounds={len(rounds)} "
                f"| tokens_used={input_tokens + output_tokens}")
    return script


_LONG3_SYSTEM_PROMPT = (
    "You are a scriptwriter for a long-form YouTube series called \"Le Simulateur "
    "de Ruine\" (The Ruin Simulator) -- a fictional scenario where a famous, "
    "extremely wealthy or powerful pop-culture character loses everything and is "
    "dropped into the real world in 2026 with zero money, then has to rebuild "
    "from scratch. The character narrates their own comeback in first person, in "
    "character (their usual arrogance/confidence/wit), while staying grounded in "
    "REAL, actionable strategies a real person could actually use. This will be "
    "read aloud by an archetype text-to-speech voice matched to the character -- "
    "NEVER an impression of the real actor/performer's own voice. Write in "
    "English. Never use em dashes."
)


def _generate_long3_script(subject: str) -> dict:
    """
    long3 ("Le Simulateur de Ruine") -- one well-known fictional character
    (auto-picked if subject is empty) loses everything and rebuilds from 0
    starting in 2026, narrated in first person in their own archetype TTS voice
    (see voice_bank -- never a real-actor clone). Fully automatable, single
    continuous monologue -- no manual audio panel needed, unlike long1.
    """
    if subject.strip():
        char_line = f'The character: "{subject}" -- use their correct canonical context/franchise.'
    else:
        char_line = (
            "Pick your own well-known, extremely wealthy or powerful fictional character "
            "yourself (movies, TV, anime, comics, games -- anything with a famous, specific, "
            "identifiable character known for wealth or power), varied every time."
        )

    prompt = f"""Generate a script for a 10-11 minute "Ruin Simulator" episode.

{char_line}

Same franchise-accuracy requirement as similar formats on this channel: double-check which
show/movie/game/book this character is actually from -- if unsure, pick a different, more
certain character instead.

PREMISE: this character loses EVERYTHING specific to their own story (name the actual
asset/company/power they lose, tied to their real canon) and wakes up in the real world in
2026 with literally zero money. They narrate, in character, their own step-by-step plan to
rebuild -- first person, their usual voice/attitude, but the actual FINANCIAL/CAREER
STRATEGY must be genuinely real and actionable, not fantasy.

STRUCTURE -- 4 chaptered sections, each a list of narration "beats" (each beat = one
paragraph, roughly 40-70 words, natural spoken monologue in character, first person). Keep
this TIGHT -- total across all 4 sections must land around 1500-1650 words (roughly
10-11 minutes spoken). Do not pad with extra beats or run-on sentences; cut anything that
isn't either a specific plot beat or a specific actionable financial move:

1. THE FALL (~180-220 words, 3 beats): the exact moment they lose everything, named
   specifically and tied to their real story. End on them waking up in 2026 with 0 euros.

2. SURVIVAL MODE - DAYS 1-30 (~450-500 words, 5-6 beats): how they refuse to be broken --
   extreme but REAL discipline (sleep, cheap-but-optimized nutrition, cutting all
   discretionary spending), in-character jabs at modern distractions/mediocrity, and their
   first real move (a real minimum-wage-or-similar job, or a real low-cost hustle) to
   generate ANY income while they plan.

3. THE CLIMB - DAYS 30-180 (~500-550 words, 5-6 beats): how they leverage their own
   SPECIFIC, canon-accurate skills (strategic thinking, negotiation, network, whatever is
   true to their story) into real income streams -- specific, concrete moves (a side
   business, a high-value skill they monetize, smart negotiation, calculated risk-taking),
   each one grounded in an actual financial/career principle a real viewer could apply.

4. THE REAL LESSON (~280-320 words, 3-4 beats): the character's blunt, in-character
   conclusion -- true wealth is not the number in the bank account, it's what remains in
   your own skillset/mindset when everything else is stripped away. End the final beat with
   a natural in-character subscribe call-to-action.

Each beat also needs a "visual_query": a short (3-6 word) image search query for a REAL
photo to show behind that beat -- concrete and literal (e.g. "empty wallet on table",
"warehouse night shift worker", "Lyon France street", "stack of euro coins"), not the
character's name (they're a fictional character, searching their name returns fan art/movie
stills, not something that reads as a real financial-advice video).

VOICE: describe an archetype for this character's voice (gender, age_register, accent, tone
-- 2-4 words capturing their attitude, e.g. ["arrogant", "confident", "dry wit"]) -- an
archetype for the ROLE, never an impression of the real actor/performer.

NET WORTH MILESTONES: 7-9 data points, IN CHRONOLOGICAL ORDER, tracing their financial arc
from their starting fortune down to zero (or negative) and back up in realistic stages
matching the beats above. Each: {{"label": "<very short label, e.g. 'Day 1: $0'>", "value":
<net worth in dollars, can be negative, as a number>}}.

Every line goes straight to TTS -- DO NOT write speaker labels inside any text field.

Return ONLY valid JSON:
{{
  "character_name": "...",
  "character_context": "<the franchise/show/movie/game name ONLY, e.g. 'Fate/stay night' or 'Breaking Bad' -- 2-5 words, NOT a biography or description>",
  "voice": {{"gender": "male|female", "age_register": "young|adult|mature", "accent": "american|british|australian|irish|indian|canadian|south_african", "tone": ["...", "..."]}},
  "sections": {{
    "shock": {{"label": "The Fall", "beats": [{{"text": "...", "visual_query": "..."}}, ...]}},
    "survival": {{"label": "Survival Mode -- Days 1-30", "beats": [...]}},
    "escalation": {{"label": "The Climb -- Days 30-180", "beats": [...]}},
    "lesson": {{"label": "The Real Lesson", "beats": [...]}}
  }},
  "net_worth_milestones": [{{"label": "...", "value": <number>}}, ...],
  "title": "<YouTube title, max 100 characters, punchy>",
  "description": "<YouTube description, 2-4 sentences, no em dashes>",
  "hashtags": ["#tag1", ...]
}}"""

    raw, input_tokens, output_tokens = call_claude(_LONG3_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL, timeout=600)
    parsed = _parse_json(raw.strip())

    character_name = (parsed.get("character_name") or subject or "Unknown").strip()
    sections = {}
    for sec_id in ("shock", "survival", "escalation", "lesson"):
        sec = (parsed.get("sections") or {}).get(sec_id) or {}
        beats = [b for b in (sec.get("beats") or []) if (b.get("text") or "").strip()]
        sections[sec_id] = {"label": sec.get("label") or sec_id, "beats": beats}

    script = {
        "format": "long3",
        "subject": character_name,
        "character_name": character_name,
        "character_context": (parsed.get("character_context") or "").strip(),
        "voice": parsed.get("voice") or {},
        "sections": sections,
        "net_worth_milestones": [m for m in (parsed.get("net_worth_milestones") or []) if m.get("label") and "value" in m],
        "title": (parsed.get("title") or character_name)[:100],
        "description": (parsed.get("description") or "").strip(),
        "hashtags": [h for h in (parsed.get("hashtags") or []) if h],
        "include_book_cta": False,
        "music_style": "tense-to-triumphant cinematic build, no lyrics",
    }
    _save_script(script)

    n_beats = sum(len(s["beats"]) for s in sections.values())
    logger.info(f"long3 script saved | character={character_name!r} beats={n_beats} "
                f"milestones={len(script['net_worth_milestones'])} | tokens_used={input_tokens + output_tokens}")
    return script


_MF_LONG_SYSTEM_PROMPT = (
    "You are a scriptwriter for \"MindFile Deep Dive\", a long-form YouTube documentary series "
    "analyzing REAL, well-documented psychological experiments, historical events, or case studies "
    "to explain how human behavior actually works. Never invent a study, a person, a number, or an "
    "event -- only use real, widely-documented psychology (classic studies, well-known documented "
    "cases, established research); if you are not confident a detail is accurate, leave it out "
    "rather than guessing. Never diagnose or make claims about a real identifiable private "
    "individual beyond what is already public historical record. This will be read aloud by a "
    "single text-to-speech narrator voice (an archetype voice, never an impression of a real "
    "person), so write natural spoken prose: full sentences, varied rhythm, contractions where "
    "natural. Write in English. Never use em dashes."
)


def _generate_mf_long_script(subject: str) -> dict:
    """
    mf_long ("MindFile Deep Dive") -- a 12-18 minute narrated documentary on a
    REAL, well-documented psychology case/experiment/event. Fully automatable
    (archetype TTS narrator via voice_bank, same as long3 -- never a real
    human voice, unlike long1), single continuous narration chaptered into
    4 sections, one real fetched photo per beat. No animated chart (unlike
    long3 -- there's no numeric arc to plot here), see
    modules.assembler.mindfile.assemble_mf_long.
    """
    case_line = (
        f'The case to analyze: "{subject}" -- research and use the REAL, documented facts of '
        "this case/experiment/event."
        if subject.strip() else
        "Pick your own REAL, well-documented psychological experiment, case study, or historical "
        "event yourself (a classic study, a well-known documented case, a pivotal moment in the "
        "history of psychology) -- varied every time, genuinely well-known enough that the facts "
        "are verifiable."
    )

    prompt = f"""Generate a script for a 12-18 minute "MindFile Deep Dive" documentary episode.

{case_line}

Also decide a narrator voice archetype for this episode (gender, age_register: young/adult/mature,
accent: american/british/australian/irish/indian/canadian/south_african, tone: 2-4 words capturing
a credible documentary-narrator vibe, e.g. ["measured", "warm", "credible"]) -- an archetype for
the ROLE, never an impression of any real person.

STRUCTURE -- 4 chaptered sections, each a list of narration "beats" (each beat = one paragraph the
narrator reads in a single breath, roughly 40-70 words, natural spoken prose):

1. THE SETUP (0-2.5 min, 3-4 beats, ~250-300 words total): open on the exact moment or scenario
   that makes this case compelling -- concrete, vivid, specific. Do not explain the full backstory
   yet, just the hook and why it matters.

2. WHAT HAPPENED (2.5-8 min, 8-10 beats, ~700-800 words total): the chronological story of the
   case/experiment/event -- specific names (where public/historical), dates, numbers, decisions.
   Weave in the underlying psychological CONCEPT each part illustrates so the audience learns the
   mechanism, not just the anecdote.

3. WHY IT HAPPENS (8-14 min, 8-10 beats, ~700-800 words total): the actual psychological
   mechanism at play -- name the real concepts (cognitive biases, social/group dynamics, learned
   behavior, conditioning, whichever genuinely apply) and explain the MECHANISM, not just the
   label. Ground every claim in the specifics of THIS case.

4. WHAT IT MEANS FOR YOU (14-18 min, 5-6 beats, ~400-450 words total): concrete, specific,
   genuinely actionable takeaways a viewer can apply to their own life/relationships/decisions
   based on this case's lesson. End the final beat with a natural spoken subscribe call-to-action.

For EACH beat, also provide a real-world image search query -- a real photo that would visually
support that beat (a real historical photo if the case involves one, or a generic real-life scene
that fits the beat's content), never the name of a private individual as a search query, never an
abstract stock-psychology cliché (no brain/neuron graphics).

Return ONLY valid JSON:
{{
  "case_subject": "<short name of the case/experiment/event>",
  "voice": {{"gender": "male|female", "age_register": "young|adult|mature", "accent": "american|british|australian|irish|indian|canadian|south_african", "tone": ["...", "..."]}},
  "title": "<YouTube title, max 100 characters, format: '<Case>: What It Really Reveals About Human Behavior'>",
  "sections": {{
    "setup": {{"label": "The Setup", "beats": [{{"text": "<one paragraph, 40-70 words>", "visual_query": "<real image search query>"}}, ...]}},
    "what_happened": {{"label": "What Happened", "beats": [...]}},
    "why_it_happens": {{"label": "Why It Happens", "beats": [...]}},
    "what_it_means": {{"label": "What It Means For You", "beats": [...]}}
  }},
  "description": "<YouTube description, 2-4 sentences, no em dashes, ends with a question or strong statement>",
  "hashtags": ["#tag1", ...]
}}"""

    raw, input_tokens, output_tokens = call_claude(_MF_LONG_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL, timeout=480)
    parsed = _parse_json(raw.strip())

    case_subject = (parsed.get("case_subject") or subject or "Untitled Case").strip()
    title = (parsed.get("title") or f"{case_subject}: What It Really Reveals About Human Behavior").strip()[:100]

    sections = {}
    for sec_id in ("setup", "what_happened", "why_it_happens", "what_it_means"):
        sec = (parsed.get("sections") or {}).get(sec_id) or {}
        beats = [b for b in (sec.get("beats") or []) if (b.get("text") or "").strip()]
        sections[sec_id] = {"label": sec.get("label") or sec_id, "beats": beats}

    script = {
        "format": "mf_long",
        "subject": case_subject,
        "case_subject": case_subject,
        "voice": parsed.get("voice") or {},
        "title": title,
        "sections": sections,
        "description": (parsed.get("description") or "").strip(),
        "hashtags": [h for h in (parsed.get("hashtags") or []) if h],
        "include_book_cta": False,
        "music_style": "subtle cinematic documentary underscore, no lyrics",
    }
    _save_script(script)

    n_beats = sum(len(s["beats"]) for s in sections.values())
    logger.info(f"mf_long script saved | case={case_subject!r} beats={n_beats} "
                f"| tokens_used={input_tokens + output_tokens}")
    return script


def _parse_json(raw: str) -> dict:
    """Extract and parse JSON from model response (handles markdown fences)."""
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    json_str = fence_match.group(1).strip() if fence_match else raw
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON: {e}\n\nRaw:\n{raw}") from e


def _save_script(script: dict) -> Path:
    _SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{script['format']}_{_slugify(script['subject'])}.json"
    path = _SCRIPTS_DIR / filename
    path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Script saved to {path}")
    # Every later lookup of this script (app.py _find_script/_generate_batch,
    # modules/users._tag_script, run_daily.py) re-derives the filename by
    # glob'ing the VIDEO's own timestamp prefix against output/scripts/ --
    # that only works if generation and assembly finish within the same
    # ~second. A slow render (Drive downloads, OAuth wait, TTS/Whisper, ...)
    # breaks it silently. Stashing the real path here lets pipeline.run()
    # rename the file to the video's timestamp once assembly is done instead
    # of relying on that fragile glob (see pipeline.py's post-assembly block).
    script["_script_path"] = str(path)
    return path


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:40]
