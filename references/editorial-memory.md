# Editorial memory

This is the default editorial knowledge for Video Use. It supplies options and decision criteria, not presets to apply blindly. Read it at the beginning of every edit; choose only the sections relevant to the material and user brief.

## Edit archetypes

| Style | Use when | Rhythm | Avoid |
|---|---|---|---|
| Clean authority | Expert talking head, education, B2B | Let key ideas land; cuts hide stumbles and dead air | Constant zooms, meme SFX, over-captioning |
| Native short-form | Vertical social, direct-to-camera hook | Fast first 3 seconds, then varied breath and visual resets | A cut every sentence with no hierarchy |
| Documentary / story | Testimonial, founder story, human moment | Holds, J/L cuts, reactions, contextual B-roll | Cutting emotion or pauses just to reduce runtime |
| Product demo | UI or physical demonstration | Cut to outcome, then proof; screen detail is the hero | Cropping interface details or adding unrelated B-roll |
| Cinematic montage | Travel, event, brand mood | Cut to movement, music phrasing, and visual match | Dialogue-style jump cuts or information-dense captions |

Before choosing a style, identify the primary job: persuade, explain, entertain, document, or demonstrate. A video can change style across beats, but it needs one dominant visual language.

## Pacing and cut decisions

- Cut for a reason: remove a mistake, enter a new beat, reveal a reaction, match an action, or improve clarity. Silence alone is not a sufficient reason when the silence carries emotion.
- Open short-form videos with a clear outcome, tension, or surprising claim. Delay logos, introductions, and explanations until the viewer has a reason to care.
- Preserve the setup before a punchline and the reaction after it. Protect demonstrations, laughs, emotional turns, and evidence as hero moments.
- Use hard cuts as the default for continuity. Prefer J/L cuts when picture and sound can bridge a change naturally. Use dissolves for genuine passage of time or memory, not as camouflage for weak continuity.
- Do not stack fast cuts, punch-ins, captions, B-roll, and SFX on the same beat unless the creative direction explicitly calls for a maximal style.

## Transition memory

| Transition | Good use | Minimum condition | Avoid when |
|---|---|---|---|
| Hard cut | New thought, comic timing, energy | Audio boundary is clean | It visibly jumps pose or background without purpose |
| J-cut / L-cut | Interviews, explanation, scene bridges | Incoming/outgoing audio adds continuity | It obscures who is speaking or changes meaning |
| Match cut | Similar shape, motion, color, or idea | The match is perceptible at normal speed | The comparison is arbitrary |
| Cut on action | Movement, demo, entering/exiting frame | Action continues across both shots | The action is incomplete or mismatched |
| Whip / motion cut | High-energy travel or social edit | Motion direction and exposure are compatible | Talking-head education or low-motion footage |
| Dip to black | Chapter end, deliberate pause, emotional reset | At least enough hold to read as intentional | Between ordinary consecutive sentences |
| Cross dissolve | Time passage, nostalgia, memory | The overlap creates a meaningful blend | The goal is only to hide a bad cut |

Default to no transition effect. The viewer should feel the story progressing, not notice a transition inventory.

## Framing, punch-ins, and B-roll

- A punch-in is an emphasis mark. Use one at a change in claim, proof, or reaction; 1.03–1.12× is usually sufficient. Use larger only after verifying face/headroom and graphics.
- Return to base framing on the next beat. Do not punch in repeatedly on adjacent short ranges.
- Do not crop eyes, top of head, hands demonstrating a product, lower-thirds, or UI controls. Review the intended frame before approval.
- B-roll must clarify, demonstrate, contextualize, or refresh attention. It should remain long enough to understand; do not use it as decorative wallpaper.
- Keep original dialogue/audio under explanatory B-roll unless the user asks for a new audio structure. Mark every B-roll/SFX item as proposed until the user supplies or approves a licensed asset.

## Subtitle memory

- Captions make speech easier to follow; they must not become the dominant visual layer.
- Match chunk length to pace: 2–3 words for fast emphasis, 4–7 for educational or narrative content. Break at meaning, never inside a name, number, fixed phrase, or grammatical unit.
- For Arabic, preserve RTL source text, dialect, punctuation, Latin terms, and numbers. Use ASS with an approved Arabic-capable font, 3–5 words per chunk where practical, and a safe-area placement checked on the rendered output.
- Highlight at most one meaningful word/phrase per caption. Ensure contrast before adding animation.

## Sound memory

- Dialogue clarity is first. Music supports pace and mood but should not compete with intelligibility.
- Cut SFX are accents: one action, one payoff. Do not add whooshes to every edit.
- Treat silence as a tool. A short intentional quiet moment can make a subsequent line or impact sound stronger.
- Keep the delivery target at -14 LUFS integrated with a -1 dBTP ceiling unless the destination specifies another requirement.

## Decision report

For each unusual choice, record the chosen moment, effect or transition, viewer-facing purpose, and risk checked. If the reason is only “to make it dynamic,” leave the decision out or refine it.


## Field lessons from client work

These came from real feedback on testimonial and promo reels. Each is a craft rule, not a client style.

- **Hook:**
  - A question on screen works if it leaves before the next line is captioned.
  - Stock "?" pings behind the speaker sell confusion, as long as they appear once and never on the face.
- **Zoom fatigue:** a push on every line reads as nervous. One gentle move about every 5 s, at sentence starts, reads as directed.
- **Caption discipline:**
  - Readers lose time hunting for captions that move, so keep them in one spot.
  - Never caption words that a graphic already shows.
  - Punctuation on screen is noise in short-form.
- **Text behind people:** only what sticks out reads. Scale the number until it frames the head, or put the word above the head.
- **Sound:** an effect without a sound feels unfinished, and a sound on every gentle zoom feels cheap. Pick from the editor's own library before synthesising.
- **Brand assets:** use the full logo with the wordmark, cut out cleanly. Never just the mark when the name is the point.

- **Denial beat:** when the speaker negates who they are or what they are ("I'm not X"), give the negation its own short beat (13 frames). Use a colour strobe behind the person (2–3 flashes) with the person in front via the local matte, and an "error" buzzer 0.2 s early. It turns a spoken joke into a visual one.
- **Text timing:** a graphic is timed to be *readable* on its word, so start it early by its entrance's readable lead. Don't simply start it before the word, and don't start it on the word.
- **Outros:** the outro covers everything once it shows: graphics and captions end before it. It fades in over the last shot's picture (about 0.3 s, never via black) and needs no whoosh.
- **Text behind people** wants a hard drop shadow (no softness) so it separates from the background. Put a spoken number beside the hand that counts it.
- **Subtitles that enter:** each cue comes in over about 10 frames, blurred, a little low and transparent, then settles. That's a soft entrance on every caption, not a hard cut. In the caption skin, set `entrance` and give the shadow in Premiere numbers (`shadow`: distance / softness / direction).
- **Plain subtitles beside styled text:** when bubbles and big text carry the emphasis, the subtitles stay plain white with a black shadow at chest height. Use no keyword colour and no glow. Emphasis lives in one place.
- **Build tension into the punch:** a riser across the hook, ending on the beat it sets up (the denial), makes the joke land.
- **Logos get a soft shadow** (Premiere distance 5, softness 40) so they lift off a busy frame.
- **Hand-finished edits are lessons:** when the editor finishes the tool's build by hand, diff it, learn each change, and reuse their finished sequences as the exemplars to copy-paste from next time.
