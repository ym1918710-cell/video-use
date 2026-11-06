# Style library

General editing knowledge for choosing a style. A **style** is an editing grammar:
how cuts, framing, type, graphics, captions, and sound behave. It belongs to no
client. A **brand identity** is a set of tokens such as colours, fonts, logo, tone,
and caption skin (see [brand identity](brand-identity.md)). The final look is
**style × brand × material**. Never hard-code one client's look into a style, and
never pick a style only because the last project used it.

Read [editorial memory](editorial-memory.md) first. This file goes deeper on each
recipe and on deciding which one to propose.

## 1. Read the signals before choosing

| Signal | Where it comes from | What it pushes toward |
|---|---|---|
| Primary job | Brief, and the transcript's first 30 s | Persuade → promo; explain → explainer or authority; document → story; demonstrate → demo; entertain → native or comedic |
| Destination | Brief, aspect ratio | Vertical social tolerates denser pacing and on-screen type. Web/YouTube 16:9 wants longer holds. Paid ads need a hook in under 1.5 s |
| Footage | `ffprobe`, a sample `timeline_view` | One static angle → invisible cuts plus framing variation. Moving or multi-camera → cut on action. Screen capture → demo grammar |
| Speaker energy | Words per second, pauses in `takes_packed.md` | Above about 3 w/s: let the speech carry and add little. Slow and deliberate: authority or story, and don't over-tighten |
| Content density | Numbers, lists, tool names, steps in the transcript | Many concrete items → kinetic type and icon stickers have a job to do. Abstract ideas → B-roll or restraint |
| Emotion | Audio events, pauses, personal stories | Protect the holds. Prefer story grammar over speed |
| Brand personality | Brand identity file, reference exports, or the user | Playful or bold → kinetic. Premium or serious → restrained. Technical → clean authority with diagrams |
| Audience | Brief | Experts → less hand-holding. Beginners → more labelling and captions |
| Production budget | Approved assets: logo, icons, B-roll, music | A graphic-heavy style with no assets ends up as empty templates, so recommend a lighter recipe |

## 2. Style recipes

Each recipe lists the layer behaviour. Brand tokens fill in colour, font, and shapes.

### Kinetic promo / explainer (short-form, vertical)
- **Use when:** you need to persuade or explain fast, the presenter talks to camera, and the script is dense with concrete items (numbers, tools, steps).
- **Cutting:** tight jump cuts on speech. Change the frame size between beats (wide ↔ medium ↔ close) so each jump reads as a deliberate reset. Add short motion transitions (8–10 frames) only at section changes.
- **Hook (0–2 s):** a large headline on the first spoken claim, optionally partly behind the subject (`behind_subject_overlays`).
- **Type:** big numerals and short keyword titles that land on the spoken word. One idea per beat.
- **Graphics:** stickers (pills, labels) for key phrases, icons at lists, a cut-out person sticker for testimonials or credentials, and the brand mark placed behind the subject at the brand mention.
- **B-roll:** 4–8 s inserts over voice-over: real B-roll or motion-graphic cards.
- **Captions:** a small single line, kept secondary to the kinetic type. Placed mid-frame or low-mid.
- **Sound:** an energetic bed, ducked under speech. SFX only on graphic payoffs. Target −14 LUFS / −1 dBTP.
- **Ending:** a brand end card, 3–5 s.
- **Risk:** visual overload. Never stack a punch-in, sticker, caption highlight, and SFX on the same word.

### Static Q&A / podcast clip
- **Use when:** you have a seated interview or podcast on one angle, and the value is in the answer.
- **Cutting:** invisible. Remove filler and pauses with soft jump cuts. Alternate the framing only if the jumps show.
- **Opening:** the question as a title card for the first 3–4 s.
- **Brand:** a small persistent logo or watermark in a safe corner or at top centre.
- **Captions:** the main reading aid. 2–5 words on a semi-transparent box, at lap or lower-third height.
- **Sound:** speech clarity first. An optional low bed. Hold every clip to the same loudness target across a series.
- **Risk:** a monotonous look over 60 s. Use a punch-in at the key claim and one graphic for a key number.

### Clean authority
- **Use when:** the speaker is an expert, the content is education or B2B, and trust matters more than energy.
- **Cutting:** let the ideas land. Cuts hide stumbles, not breaths.
- **Framing:** restrained punch-ins (1.03–1.08×) only at turns in the argument.
- **Graphics:** lower-third name and title, a few diagrams or data callouts, generous negative space.
- **Captions:** 4–7 words, clean, no word pops.
- **Sound:** little or no music. Room tone stays continuous.
- **Risk:** feeling slow on social. Tighten the first 3 s only.

### Native short-form / creator
- **Use when:** the video is personal, direct to camera, and trend-aware.
- **Cutting:** a fast first 3 s, then varied breathing. Zoom resets are allowed.
- **Captions:** big 2–3 word chunks, one highlighted word, centred low-mid.
- **Graphics:** reaction stickers or emoji only if they fit the brand personality.
- **Sound:** a trend-style bed and the occasional SFX accent.
- **Risk:** a cut on every sentence with no hierarchy.

### Documentary / story
- **Use when:** the video is a testimonial, founder story, or emotional turn.
- **Cutting:** holds, J/L cuts, reactions. Pauses are content.
- **Graphics:** almost none. Contextual B-roll, and maybe a date or place card.
- **Captions:** a soft, narrative chunk length.
- **Sound:** a score that follows the emotional arc, plus room tone and ambience.
- **Risk:** over-tightening kills emotion.

### Product / screen demo
- **Use when:** you are showing a UI, a physical product, or a tutorial.
- **Cutting:** cut to the outcome, then to the proof. Speed-ramp or skip waiting time.
- **Framing:** zoom into the UI region that matters. Never crop away the controls being used.
- **Graphics:** callouts, cursor highlights, step numbers.
- **Captions:** optional. On-screen labels often do the job better.
- **Risk:** unrelated B-roll and decorative motion.

### Cinematic montage
- **Use when:** the footage is travel, an event, or a brand mood film.
- **Cutting:** to music phrasing, movement, and visual matches.
- **Graphics:** minimal titles.
- **Captions:** usually none.
- **Sound:** music leads. Natural sound in spots.
- **Risk:** dialogue-style jump cuts.

Styles can be mixed across beats: a kinetic hook can open into a clean-authority
body. Keep one dominant language, and switch only at a real beat change.

## 3. How to propose

1. From the signals, shortlist **2–3 recipes**. Give each a one-line reason that is
   tied to this material ("the script lists 6 tools in 20 s, so kinetic type has a job
   here").
2. Recommend one. Say what it needs that is missing: assets, brand tokens, licensed
   music.
3. Name the brand identity it will be dressed in. If none exists, ask for references
   or offer to derive one (see [brand identity](brand-identity.md)).
4. After approval, record the chosen recipe and any adaptations in
   `style_profile.json` (`style.recipe`, `style.adaptations`). Repeated approvals of
   the same adaptation for the same client go into that client's brand identity, not
   into this file.

## 4. Learning a new style from references

When the user shares finished videos as "the level I want":

1. Run `helpers/analyze_references.py <files or dir> -o <edit>/reference_analysis.json`.
   It measures format, shot length, loudness, silence, and dominant colours.
2. Look at 2–3 `timeline_view` strips per reference: hook, middle, ending.
3. Describe each reference as **recipe + adaptations + brand tokens**. Decide which
   parts are craft (worth reusing anywhere) and which are client identity (stays in
   that client's brand file).
4. Write any new, generally useful craft insight into this library as a recipe or
   adaptation, with no client names, colours, or logos. Write client identity only to
   the project or client brand identity file.


## Transition vocabulary: when to suggest which

| Transition | Reads as | Suggest when | Avoid when |
|---|---|---|---|
| Hard cut | Invisible, honest | The default, especially for dialogue and jump cuts | — |
| `light` (coloured flash) | Energy, a brand "beat", a switch to illustration | Cutaways and B-roll in promo or testimonial reels, and section changes in kinetic styles. Use the brand colour (measured from references) or a warm leak. | Calm authority or documentary styles, or more than about 1 per 3 s |
| `dissolve` | Time passing, softness | Story and montage styles, slow music | Fast talking heads, where it reads as a mistake |
| `dip_black` / `dip_white` | Chapter break | Start or end of a section, before an end card | Mid-sentence |
| `whip` / `slide` / `zoom` | Kinetic, playful | Native short-form and kinetic promo, on a beat | Serious or medical claims |

Always land the cutaway on the word it shows. A house rule ("light in and out of every B-roll") belongs in the client's brand kit `transitions`, never in the tool.


## Text vocabulary: composition × entrance per style

| Style recipe | Questions / hooks | Names / labels | Proof / numbers | CTA |
|---|---|---|---|---|
| Kinetic promo | `two_tone` + `drop` | sticker `pop` | number `stamp` | `boxed` + `stamp` |
| Static Q&A / testimonial | `stacked` or `two_tone` + `rise` | sticker `pop` | `highlight` the key word | `kicker` + `rise` |
| Clean authority | `underline` + `wipe` | lower third `slide` | `blur` | `boxed` + `fade` |
| Native short-form | `boxed` + `pop` | sticker `pop` | `typewriter` | `stamp` |
| Story / documentary | `stacked` + `fade` / `blur` | lower third `fade` | `zoom` | `fade` |

Keep one pairing per kind of beat inside a video. Every entrance makes a sound (auto SFX), so fewer, better-placed graphics beat many.
