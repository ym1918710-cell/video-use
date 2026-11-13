---
name: video-use
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, burn subtitles — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **Equal-power audio crossfade at every cut** (default 30ms, `audio.crossfade_ms` 4–200, centered on the picture cut; `render.py` builds the audio as one PCM master and encodes AAC once). Never fade each side to silence — a dip to zero makes room tone and music beds audibly gate, and per-segment AAC adds gaps at every boundary.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the Scribe transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` project directory.
13. **Every render is guarded.** Initialize session state, validate the EDL, render, then write a QC report. Use `helpers/run_edit.py`; do not call `render.py` directly unless diagnosing the renderer itself.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── editorial_plan.json      ← protected moments + proposed B-roll/SFX + effect reasons
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key). Don't re-run it every session; on cold start just verify:

- `ELEVENLABS_API_KEY` resolves — either in the environment or in `.env` at the video-use repo root. If missing, ask the user to paste one and write it to `.env` (never to the user's `<videos_dir>`).
- `ffmpeg` + `ffprobe` on PATH.
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the video-use repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/video-use/` or `~/.codex/skills/video-use/`.

## Persistent editorial memory

Before proposing any edit, read [editorial memory](references/editorial-memory.md). It is the agent's durable base knowledge for pacing, styles, transitions, captions, sound, and visual emphasis.

- Read [delivery memory](references/delivery-memory.md) whenever the destination, aspect ratio, safe areas, frame rate, or export format matters.
- Read [color memory](references/color-memory.md) whenever correction, matching, HDR/SDR conversion, or a creative grade is requested.
- Read the [style library](references/style-library.md) before proposing a strategy. It lists the signals that decide a style and the layer-by-layer recipes (kinetic promo, static Q&A, clean authority, native short-form, story, demo, montage). Styles belong to no client.
- Read [brand identity](references/brand-identity.md) when a client, logo, colours, fonts, or reference exports are involved. Each project's `brand_kit.json` holds the client's tokens. Leave it empty for a brand-neutral edit, and never carry one client's identity into another project.
- When the user shares finished videos as a quality or style reference, run `analyze_references.py` and follow "Learning a new style from references" in the style library. Put reusable craft in the library and client look in the brand kit.
- Treat `style_profile.json` as creator-specific overrides. It begins intentionally empty; add a preference there only when the user explicitly approves it or repeated feedback demonstrates it. User preference wins over these references.
- Do not dump the memory catalog into a response. Select a fitting approach, say what it will achieve, and document non-obvious choices in `editorial_plan.json`.

## Helpers

- **`transcribe.py <video>`** — single-file Scribe call. `--num-speakers N` optional. Cached.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.
- **`session_state.py <edit_dir>`** — creates the persistent brief, style profile, and lifecycle state without overwriting existing preferences.
- **`validate_edl.py <edl.json>`** — rejects invalid sources, ranges, and overlay windows before a render; writes `validation_report.json`.
- **`verify_render.py <edl.json> <output>`** — checks the rendered media against the EDL and writes `qc_report.json`. Pass `--visual` to produce cut-boundary filmstrips for the agent's visual readback.
- **`run_edit.py <edl.json> -o <out>`** — preferred guarded route: initialize → validate → render → QC. Pass `--visual-qc` for a final visual check.
- **`review_editorial_plan.py <edl.json>`** — checks protected moments and asset-approval status, then writes `editorial_review.json`. `run_edit.py` performs this advisory review automatically.
- **`graphics.py`** — renders the EDL's `graphics` (stickers, numbers, headlines, lower thirds, logo, end card) from `brand_kit.json` tokens; called by `render.py`.
- **`audio_mix.py`** — dialogue chain, speech-aware music ducking, SFX, stems; called by `render.py`.
- **`face_anchor.py`** — local Vision face centres per range (face framing, auto punch-in).
- **`export_timeline.py <edl.json> [--premiere]`** — CMX3600 + FCP7 XML hand-off for Premiere, Resolve, or Avid. `--premiere` rebuilds the *finished* edit from the last render's `render_manifest.json` as a layered Premiere project:
  - V1: the picture, cut at every edit.
  - V2: the layers behind the subject.
  - V3: a subject cut-out above them.
  - V4: graphics. V5: captions. V6: the sources, disabled.
  - A1: dialogue. A2: one clip per SFX, at its level. A3: the source audio, disabled.
  - Alongside it: an `.srt` and the `_media/` folder. Import it in Premiere with File → Import.
- **`compare_takes.py --edit-dir <dir>`** — ranks repeated lines across takes.
- **`camera.py`** — keyframed zoom / pan / face-track maths (`zoom`, `camera`, `track` on a range); called by `render.py`.
- **`multicam.py sync|switch <edl.json>`** — angle sync by audio; cuts to the speaker's angle.
- **`premiere.py install|ping|sequences|learn|build`** drives Premiere Pro through the Video Use Bridge panel (`premiere/video-use-bridge`, local only on 127.0.0.1:7788).
  - `learn "<sequence>" --edit <edit/>` reads an editor's own sequence into the brand kit's `premiere_house`: how they zoom (Adjustment Layer + Transform, peak, ramp, lift), their text entrance and shadow, their MOGRTs and text fields, their light-leak graphic, and their favourite SFX by role.
  - `build <edl.json>` makes a new sequence their way: source cuts, the zoom on Adjustment Layers, their MOGRTs with the new words, their attributes pasted from exemplars (blend modes, entrances), SFX on lanes, and a captions track.
  - Premiere can't script native text graphics, only MOGRT text, so use a MOGRT from the project.
  - Subtitles: `build` imports the SRT as a caption track. The editor runs Graphics → Upgrade Caption to Graphic, then `build --only caption_fx` pastes the learned subtitle clip (`premiere_house.captions`: entrance, shadow, Motion position) onto every one, skipping Text so each keeps its words.
  - Zoom: Transform with Uniform Scale on, keys on Scale.
  - **Two paths.** For "on the device", render with `run_edit.py`. For "in Premiere", run `build --prepare` straight away. It makes only the assets (graphics, matte, SFX list, manifest), with no mix, no final video and no QC render, and the result is checked in the sequence itself.
  - J/L cuts (`j_cut_ms` / `l_cut_ms`) are built in Premiere too: that range's sound is laid unlinked, offset from its picture.
  - The project is never saved for the editor.
- **`beats.py suggest <edl.json>`** finds lines that earn a beat in the transcripts, such as the **denial** ("أنا مش ممدوح", "I'm not…").
  - Put them in `edl["beats"]` and the render adds a colour strobe behind the subject (local matte keeps them in front) and an error buzzer 0.2 s early.
  - The beat lasts 13 frames.
  - The colour and sound come from `brand_kit.beats.denial`.
  - A riser builds into the last beat of a run and ends as the buzzer starts (`riser_sfx`, else the library's riser role; `"riser": false` turns it off).
- **`listen.py <audio> [--from s --to s] [--png out.png]`** — "hears" any sound for the agent. It measures peak, RMS, attack, decay and hit point, plus brightness, noisy vs tonal, pitch, onsets, silences and speech-likeness. It also names the shape: hit, pop, whoosh, riser, chime, beep, sustained or speech. `--png` draws the waveform over the spectrogram so the agent can look at it.
  - **Placement:** SFX are placed by their hit point, so they land on the event; this also works when a file starts with silence.
  - **Splits** (cinematic scenes, not reels): `listen.py splits <edl.json>` proposes J-cuts only where both sides of the moved cut are quiet. That means no clipped last syllable and no director's voice. Where the cut is tight, it proposes holding the previous shot longer.
  - **Before choosing a sound,** listen to it. Never pick a sound by its name alone.
- **`reel_review.py <edl.json>`** — checks a reel from the EDL before any render (both paths). It confirms the hook moves within 3 s, and it lists every stretch longer than 3 s with no visual change (cut, zoom, graphic, overlay, B-roll). Run it on the plan and fill each stretch it reports.
- **`material.py add|scan|search|summary`** — indexes the user's material folder (SFX by role, music, fonts with Arabic coverage, alpha / green-screen graphics, backgrounds, transitions, LUTs, templates). Run `summary` at the start of every session.
- **`overlay_fx.py`** — keys a green/blue-screen or alpha clip, recolours it to the brand, adds glow, fits it to the canvas, and keeps the speaker's head clear. In an EDL, use `"prepare"` on any overlay.
- **`library.py scan|tag|search|suggest`** — the user's licensed music & SFX library.
- **`text_render.py`** — Pillow + libraqm text engine (Linux twin of CoreText); `VIDEO_USE_TEXT_ENGINE=pil` forces it.
- **`analyze_references.py <files|dir> -o reference_analysis.json`** — measures finished reference videos (format, shot length, loudness, silence, dominant colours) so a style or brand kit can be learned from examples.
- **`person_mask.py <video> -o <mask.mkv>`** — uses local macOS Vision to create a frame-accurate person matte. No footage is uploaded.

For animations, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process

1. **Initialize + recall + inventory.** Read the persistent editorial memory relevant to the request, then run `session_state.py <edit_dir>` once; it never overwrites an existing brief or style profile. Read any existing `style_profile.json` and `editorial_plan.json` before choosing an approach. `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** Shortlist 2–3 style recipes from the style library's signals and recommend one, giving a reason tied to this material. Name the brand identity it will wear (or say it is brand-neutral). Then give 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.** Record the chosen recipe in `style_profile.json` → `style`.
5. **Execute.** Update `edit_brief.json` and `style_profile.json` with approved choices, then produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Use `validate_edl.py` to repair factual EDL errors before rendering.
6. **First cut → approval → finish.**
   1. Render `run_edit.py edl.json -o first_cut.mp4 --preview --first-cut`. This is the story, cuts, framing, B-roll, transitions, and dialogue only, with no graphics, captions, music, or SFX.
   2. Self-check it, then show it and **wait for the user to approve the cut**.
   3. Only then add the finishing layers and run `run_edit.py edl.json -o preview.mp4 --preview --visual-qc`. This is the required render path.
7. **Self-eval (before showing the user).** Run `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike or dip at the boundary (pop or gating the crossfade did not hide)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Read `qc_report.json` first. It records:
   - output duration, stream presence, audio peak, and validation results;
   - **integrated LUFS and true peak** against −14 / −1;
   - **frozen or black stretches** across the whole timeline;
   - **caption reading speed** (over 20 characters/second, or under 0.5 s on screen);
   - a **pacing** block (shots, median shot length, words per minute, graphics per minute, longest stretch with no visual change);
   - conservative nearly-black-frame signals at the start, end, and each cut. A nearly-black signal is a review prompt, not an automatic rejection—fades and intentional black scenes are valid. Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. The QC report is factual evidence; the visual read remains an editorial judgment.

   Measure the audio, don't assume it: `ffmpeg -i out.mp4 -af ebur128=peak=true -f null -` for integrated loudness and true peak, plus RMS per section (dialogue, music-only, end card). An end card 15 dB under the dialogue, or effects louder than speech, is a bug. You cannot listen: say so, and report the numbers.

   For anything the user will publish (launch, promo, ad), also spawn one **critic sub-agent** with the rendered file, the EDL, and any reference videos the user gave. Brief it to roast, not to praise: a verdict, ranked problems with timecodes and evidence (frames, levels), and the 5 fixes to do first. Fresh eyes catch what the author stopped seeing — cut-off payoff lines, 0.5s memes, unreadable 28px text at phone size.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## Editorial decision contract

For every non-trivial decision, record the viewer-facing reason in the EDL or `editorial_plan.json`: what the viewer should notice, why this exact moment earns attention, and what visual or audio risk was checked. Do not add an effect merely because a keyword occurred.

- **Punch-in:** Use only to emphasize a turn in meaning, a key proof, or a strong reaction. Add `"punch_in": {"scale": 1.08, "reason": "..."}` to a selected range. The renderer supports a restrained 1.03–1.20× punch-in cropped from the source pixels; add `anchor_x` / `anchor_y` (0–1) to keep the face, not the frame centre, in the crop. Inspect the source first for headroom and on-screen graphics. On a jump cut inside one take, alternating framing (e.g. 1.0 → 1.1) makes the cut read as intentional. Return to normal framing at the next editorial beat.
- **Hero moments:** Flag demonstrations, emotional reactions, jokes, and key evidence as protected. Do not use silence removal or jump cuts that damage them; use a hold, a reaction, a J/L cut, or B-roll cover instead.
- **B-roll / SFX:** Write proposed items to `editorial_plan.json` with `start_in_output`, `duration`, `purpose`, `source_or_query`, and `status: "proposed"`. Only turn an item into an overlay after the user provides or approves a licensed asset. Never silently fetch stock footage or use sound effects to disguise a weak cut.
- **Never put text on a face.** `render.py` detects faces on the assembled picture and moves every graphic and caption to the nearest face-free spot. Still check the QC strips, especially on crowd B-roll where faces fill the frame.
- **Decision limit:** Prefer one clear visual idea per beat. If an effect does not clarify, pace, or emotionally support the story, leave it out.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation. `slope` → highlights, `offset` → shadows, `power` → midtones.

**Example filter chains** (`grade.py` has `--list-presets`; use them as starting points or mix your own):

- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Shipped in a real launch video. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else — portraiture, nature, product, music video, documentary — invent your own chain. `grade.py --filter '<raw ffmpeg>'` accepts any filter string.

Hard rules: apply **per-segment during extraction** (not post-concat, which re-encodes twice). Never go aggressive without testing skin tones.

## Subtitles (when requested)

Subtitles have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), and **placement** (margin from bottom). The right combo depends on content.

**Worked styles** — pick, adapt, or invent:

**`bold-overlay`** — short-form tech launch, fast-paced social. ~2-word chunks, UPPERCASE, break on punctuation and pauses ≥ 0.3s, grow to 3 words rather than flash a cue < 0.35s (`chunk_words` in `render.py`), Helvetica 18 Bold, white-on-outline, `MarginV=90` (clears the vertical-app UI). `render.py` ships with this as `SUB_FORCE_STYLE`.

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=90
```

**`natural-sentence`** (if you invent this mode) — narrative, documentary, education. 4–7 word chunks, sentence case, break on natural pauses, `MarginV=60–80`, larger font for readability, slightly wider max-width. No shipped force_style — design one if you need it.

Invent a third style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

### Arabic captions

For Arabic, do **not** use the generic `--build-subtitles` output: it forces two-word uppercase SRT intended for fast English overlays. Build an ASS file instead:

```bash
python helpers/build_captions.py edit/edl.json -o edit/master_ar.ass --font "<approved Arabic font>"
```

Set the EDL `subtitles` field to that `.ass` file, then render through `run_edit.py`. The renderer preserves ASS typography and lets libass handle RTL shaping. Keep chunks to 3–5 words, preserve the original dialect and Unicode text, and check mixed Arabic/Latin words, numbers, punctuation, and safe-area placement in the visual QC filmstrips. Do not uppercase Arabic or manually reverse its characters.

### Brand-styled captions (`"captions": true`)

Set `"captions": true`, or an object of overrides, and the renderer builds captions from the transcripts. They are dressed by the brand kit's `caption_skin` and kept clear of graphics.

- **Skin fields** (from the brand kit, overridable in the EDL): `position` (vertical centre, 0–1), `size_ratio`, `box` + `box_color` + `box_opacity`, `text_color`, `outline_color`, `highlight` (the colour of the word being spoken), `max_words`, `font`.
- **Clearance:** captions move above or below any graphic that overlaps their band, and hide under an `end_card`.
- **Bidi:** mixed Arabic/Latin lines get RTL ordering. On macOS, Arabic with `highlight` is rendered as a CoreText caption track, because libass lays out bidi per style run and a coloured word would break the line's word order.
- **Precedence:** an explicit `subtitles` file wins over `captions`.

## Subject in front of graphs or text

Use `behind_subject_overlays` when a graph, animated label, or visual must appear behind the person. Video Use creates a local macOS Vision person matte from the assembled base video, draws these layers, restores the person as the foreground, then applies ordinary `overlays` and subtitles on top.

```json
"subject_mask": {"mode": "person", "quality": "balanced"},
"behind_subject_overlays": [
  {"file": "animations/chart/render.mov", "start_in_output": 3.2, "duration": 4.0, "x": "0", "y": "0"}
]
```

The mask inherits the assembled video's frame rate by default; set `fps` only for an intentional override. Use `fast` only for previews and `accurate` for difficult hair, hands, or movement. Inspect the output around every entrance/exit and moving arm; masking is never assumed perfect. Keep the graph/text clear of the face and use a conventional top overlay instead if the visual does not need to pass behind the person.

## Brand graphics (`graphics` in the EDL)

For stickers, kinetic numbers, headlines, lower thirds, a logo bug, and end cards, describe **what and when** in the EDL. Leave **how it looks** to the project's `brand_kit.json` (see [brand identity](references/brand-identity.md)). If the kit is empty, everything renders brand-neutral. Re-dressing the same edit for another client only means swapping the kit, not touching the EDL.

```json
"graphics": [
  {"type": "headline", "text": "إزاي بتفهم\nالـKPIs؟", "start_in_output": 0.3, "duration": 2.9, "position": {"x": 0.5, "y": 0.2}, "behind_subject": true},
  {"type": "sticker", "text": "Free Bootcamp", "at": {"source": "C0103", "time": 41.30}, "duration": 1.6, "position": "upper_third"},
  {"type": "number", "text": "5", "label": "أيام", "start_in_output": 15.05, "duration": 2.0, "position": {"x": 0.74, "y": 0.42}},
  {"type": "lower_third", "title": "محمد", "subtitle": "Data Analyst", "start_in_output": 7.1, "duration": 2.0},
  {"type": "title_card", "text": "السؤال؟", "start_in_output": 0, "duration": 3.5},
  {"type": "logo", "start_in_output": 0, "duration": 27.9},
  {"type": "end_card", "text": "اكتب data في الكومنتس", "start_in_output": 27.9, "duration": 1.0}
]
```

- **Types:** `sticker`, `title_card`, `number` (+`label`), `headline` (`\n` for two lines), `lower_third` (`title`, `subtitle`), `logo` (from the kit, or `file`), and `end_card` (full frame, with the kit's logo + `text`).
- **Timing:** use `start_in_output`, or `at: {source, time}` to land on a spoken word from the transcript. The renderer maps it through the kept ranges, so it stays correct after re-cuts.
- **Position:** `top`, `top_center`, `upper_third`, `center`, `lower_third`, `bottom`, or `{x, y}` (the element's centre, 0–1). Positions are clamped to the safe area, and vertical canvases keep clear of the platform UI at the bottom.
- **Motion:** `animation` is one of these. Defaults depend on the type, and timing follows the kit's `motion.energy` (`calm` / `medium` / `high`). Optional `tilt` (degrees) and `scale`.
  - `pop`: overshoot scale. Light and friendly; the default for stickers.
  - `rise`: drift up. Calm; the default for headlines.
  - `slide`: from the reading side.
  - `fade`: soft.
  - `drop`: falls in with a bounce. Playful.
  - `stamp`: lands from 1.6× with impact. For a hero word or a number.
  - `zoom`: gentle scale-up. Premium or calm.
  - `wipe`: reveals in reading direction (RTL from the right).
  - `blur`: focuses in. Cinematic or authority.
  - `typewriter`: one word at a time, with a tick each.
  - `none`.
- **Composition:** set `style` on headlines and cards.
  - `stacked`: the default.
  - `two_tone`: the last line in the accent colour, 12% bigger. Good for a question with a payoff.
  - `boxed`: text on a brand-colour block. Reads on busy footage.
  - `kicker`: a small pill (`kicker` text) above the headline, then the headline.
  - `underline`: an accent bar under the text.
  - `highlight: ["word"]` colours chosen words in the accent colour, in any style. The accent comes from `shapes.headline.accent`, else `colors.accent`.
  - Layered styles animate part by part (kicker → text → bar), not as one block.
  - Vary styles across a video: a different composition or entrance for each kind of beat (question, proof, name, CTA), kept consistent within a kind.
- **Drop shadow:** every text card and pill gets a soft drop shadow cast from its own shape, so it reads on any picture. Tune it with the kit's `text_shadow` (`color`, `opacity`, `blur_ratio`, `offset_ratio`), turn it off for the brand with `"text_shadow": false`, or per element with `"shadow": false`.
- **Collisions:** an element moved off a face also avoids every graphic already on screen, so moved elements never stack on each other.
- **Sound for every effect:** every graphic entrance, transition, and camera move gets a matching sound automatically.
  - The sounds are `pop`, `impact`, `whoosh`, `swoosh`, `soft`, `shimmer` (light flash), `tick` (per typewriter word), and `riser` (end card). They are synthesised into `<edit>/sfx/auto_*.wav`, so no licence is needed.
  - Map any kind to a licensed file with `sfx_map`, in the brand kit or `edl.audio`.
  - A hand-placed SFX within 0.15 s of an effect counts as its sound.
  - Opt out with `"sfx": false` on an element, or `audio.auto_sfx: false` for the edit. QC then lists every effect left silent, in `effect_sound`.
  - The first cut has no SFX.
- **Behind the subject:** `"behind_subject": true` puts the element behind the person. The person mask is created automatically. Check that the face stays clear.
- **Rendering:** text goes through macOS CoreText, so Arabic, mixed Arabic/Latin, and any installed or brand-supplied font (`.ttf`/`.otf` path in the kit) shape correctly. Pick an Arabic font that has bold weights. Elements are cached in `<edit>/graphics/`.
- The style recipe decides **how many** graphics to use and where (see the style library). Give each one a job: one idea per beat, and land it on the word it supports. Don't stack a sticker, number, and punch-in on the same beat.

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation before building anything.

**Tool options:**

Pick the engine per animation slot. Do not default to Remotion just because the animation is web-adjacent.

- **HyperFrames** — Browser-native HTML/CSS/GSAP video compositions: product UI motion, website-to-video or mockup-to-video captures, kinetic typography, landing-page/storyboard promos, data-driven UI states, transparent WebM overlays, and clips that need deterministic frame capture plus HyperFrames lint/validate/render checks. Best when the animation should be authored and verified like a web composition instead of a React component tree.
- **Remotion** — React/CSS compositions with component state, reusable React primitives, or an existing Remotion brand system. Best when the user specifically asks for React/Remotion or when React composition is the simpler authoring model.
- **Manim** — formal diagrams, state machines, equation derivations, graph morphs. Read `skills/manim-video/SKILL.md` and its references for depth.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, single bar reveals, progressive draws. Fast to iterate, any aesthetic you want. The launch video used this.

For HyperFrames slots, scaffold the slot inside `edit/animations/slot_<id>/` with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, build the HTML composition there, run the HyperFrames checks that fit the slot (`lint`, `validate`, and a draft render when practical), then produce the final overlay video with `npx --yes hyperframes render . -o render.mp4` or `--format webm -o render.webm` when alpha is required. Point the EDL overlay `file` at the actual rendered path.

For Remotion slots, keep the Remotion project isolated inside the same slot directory, scaffold with `npx create-video@latest` or install Remotion locally there, render the composition to `render.mp4` with the project-local `remotion render` command, and verify duration and dimensions with `ffprobe`.

None is mandatory. Invent hybrids if useful (e.g., PIL background with a HyperFrames or Remotion layer on top).

**Duration rules of thumb, context-dependent:**

- **Sync-to-narration explanations.** A viewer needs to parse the content at 1×. Rough floor 3s, typical 5–7s for simple cards, 8–14s for complex diagrams. The launch video shipped at 5–7s per simple card.
- **Beat-synced accents** (music video, fast montage). 0.5–2s is fine — they're visual accents, not information. The "readable at 1×" rule becomes *"recognizable at 1×"*, not *"fully parseable."*
- **Hold the final frame ≥ 1s** before the cut (universal).
- **Over voiceover:** total duration ≥ `narration_length + 1s` (universal).
- **Never parallel-reveal independent elements** — the eye can't track two new things at once. One thing, pause, next thing.

**Animation payoff timing (rule for sync-to-narration):** get the payoff word's timestamp. Start the overlay `reveal_duration` seconds earlier so the landing frame coincides with the spoken payoff word. Without this sync the animation feels disconnected.

**Easing** (universal — never `linear`, it looks robotic):

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws.

**Typing text anchor trick:** center on the FULL string's width, not the partial-string width — otherwise text slides left during reveal.

**Example palette** (the launch video — one aesthetic among infinite):
- Background `(10, 10, 10)` near-black
- Accent `#FF5A00` / `(255, 90, 0)` orange
- Labels `(110, 110, 110)` dim gray
- Font: Menlo Bold at `/System/Library/Fonts/Menlo.ttc` (index 1)
- ≤ 2 accent colors, ~40% empty space, minimal chrome
- Result: terminal / retro tech feel

This is one style. If the brand is warm and serif, use that. If it's colorful and playful, use that. If the user handed you a style guide, follow it. If they didn't, propose one and confirm.

**Fonts fail silently.** A web font that didn't load renders in a fallback face with no error — the video ships in "almost Arial". In HyperFrames/Remotion, await the font load and then assert it: `if (!document.fonts.check('700 76px "Inter"')) throw new Error(...)`. In PIL, pass an explicit font path; never rely on the default.

**Worked example — "show the edit" hook** (a launch video for this tool). Instead of a title card, the first 3s visualize the editing itself: each transcript word pops in on its Scribe timestamp, with bars under it drawn from the real audio envelope; a filler ("ummm") grows letter by letter while it is spoken, turns orange and is cut out on screen at the same frame the audio cuts, and the next line lands immediately. It works because the picture is *driven by the same data as the sound* — one composition (Remotion) reads frame-exact word/envelope JSON produced in Python, so nothing can drift. Use the idea whenever the story is "we removed something": make the removal visible.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via the `Agent` tool. Each prompt is self-contained (sub-agents have no parent context). Include:

1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or reference to a design system)
5. Font path with index
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames, parallel agents don't overwrite each other).

## The material folder (check it before inventing anything)

The user keeps licensed material on this machine: sound effects, music, fonts, graphics (alpha MOVs and green-screen clips), backgrounds, transitions, LUTs and templates. Register it once with `material.py add <folder>`; it is remembered in `~/video-use-library/config.json`. Then:

1. **Start every session** with `material.py summary`. If the folder changed, run `material.py scan`, which only re-reads changed files.
2. **Search before you build.** For any graphic idea, sound, or font, run `material.py search <type> "<words>"` first. Use the user's asset when one fits. Generate (synth SFX, Three.js, PIL) only when nothing does, and say which one you chose.
3. **Sound effects:** `auto_sfx` picks from the library by role, loudness-matched, and rotates so repeats don't sound canned. An element may name a file directly with `"sfx": "/path/to/shine.mp3"`.
4. **Fonts:** a brand kit or caption skin may name a font ("Cairo Black"). It resolves to the file in the material folder. Arabic text only ever gets a font that covers Arabic.
5. **Overlays:** put the raw file, or `material:<id>`, in an overlay and add `"prepare": {"recolor": [...], "glow": "#...", "keep_out": "subject", "play": "once"}`. Green and blue screens are keyed automatically, and alpha MOVs pass straight through.
6. **LUTs and templates** (.mogrt / .aep) are listed but not applied: grading waits until the user trains it, and templates need Premiere or After Effects.

## Field lessons (defaults learned from real client edits)

Unless a brief says otherwise, these hold for every edit:

- **Shot report before work.** Pull start, middle, and end frames of every chosen take, and measure each take's level, peak, and the director's voice before and after it. Report anything off-frame, clipped, or mis-transcribed before cutting.
- **Fix the transcript's brand words** (product names, "KPIs", the client's name) in `transcripts/*.json` before captions are built, not after.
- **B-roll lands on the word it shows.** Brand-coloured light transitions go in and out of B-roll when the client uses them.
- **Zoom:** smooth keyframes only, never a cut-in zoom. Make one gentle move about every 5 s, at sentence starts, and keep the scale continuous across cuts. Above source resolution, cap the zoom at about 1.4×. Only a strong, fast move gets a sound.
- **Captions:** `fixed: true` keeps them in one spot (about 0.66 down the frame), `skip_repeats: true` drops a cue that an on-screen graphic or the logo already says, and `strip_punctuation: true` removes punctuation. With fixed captions, a text graphic in the caption area leaves as the next caption starts.
- **Text:**
  - Never on a face.
  - Never wider than the frame.
  - Highlight words in the brand accent.
  - The client's signature shapes live in `brand_kit.json → text_styles`.
- **Text behind the subject** must be bigger than the body (a huge numeral) or sit beside or above the head. Otherwise the person hides it all. Behind layers may bleed off-frame.
- **Stock overlays** play once, around the speaker, never on the face. Use `keep_out: "subject"` as well as the matte, and render the person matte at `quality: "accurate"`.
- **Text is readable on its word.**
  - A graphic anchored with `at` starts early by its own animation's readable lead (pop ≈0.1 s, rise ≈0.09 s, wipe ≈0.15 s…), so the words can be read as they are spoken.
  - Its sound lands when it appears.
  - Opt out with `"anticipate": false`.
- **The outro covers everything.** The outro range (`"outro": true`, or a last range with no speech) always dissolves in over the picture, never through black.
  - If the last shot has no frames after its out point, the dissolve leans before the cut.
  - Every graphic, caption and behind-layer ends as it starts to show.
  - Its dissolve gets no whoosh.
  - In Premiere it sits on the top track with an Opacity fade-in.
- **Every visible effect has a sound**, from the material library first.
- **No colour grade** until the user asks for one or has trained the look. Colour matching between cameras is a separate, explicit step.
- **The outro dissolves over the last shot.** Never cut to black first.
- **First cut first:** picture, framing, B-roll and dialogue only, with no SFX, graphics, or captions. Wait for approval.
- **Verify by eye.** After every render, pull exact frames (with `select=eq(n,N)`, never input `-ss` on the output) at each graphic, each transition, and the hook. Fix what you see before sending.

## Music and sound effects (when requested)

Sound is where generated videos sound cheap. Worked rules from launch edits:

- **Fewer effects.** Every effect is tied to something visible (a cut, a landing, a click). ~20 stock whooshes/risers/impacts in 18s reads as generic; ~8 reads as designed.
- **Hit on the frame.** Most effects have an attack (silence or a build before the transient). Measure it (first sample above ~-30 dBFS of the peak) and start the file `attack` seconds *before* the visible contact frame.
- **Duck music under speech** (roughly -12 to -15 dB relative to its music-only level; `audio.music` does this automatically from `bed_lu` / `gap_lu`), and ramp it out before a stinger or end card instead of letting its own tail decay under your CTA.
- **Master once:** mix to PCM, then two-pass loudnorm (-14 LUFS, true peak ≤ -1 dBTP) on the final mix. Then measure per section (see Self-eval).
- **Music taste is the user's call.** Generated music defaults to "hype"; offer two contrasting beds and let the user listen. Don't claim a mix sounds good — you can only measure it.

## Audio post (`audio` in the EDL)

The dialogue master (equal-power crossfades) can be finished like a mix engineer would. All levels are relative to the measured dialogue loudness; the final mix is then normalized to −14 LUFS / −1 dBTP. Stems go to `<edit>/stems/` (`dialogue.wav`, `music.wav`, `sfx.wav`, `mix.wav`).

```json
"audio": {
  "crossfade_ms": 30,
  "dialogue": {"preset": "clean", "denoise": false},
  "music": [{"file": "music/bed.mp3", "source_start": 12.0, "start_in_output": 0, "end_in_output": null,
             "bed_lu": -20, "gap_lu": -9, "fade_in_ms": 800, "fade_out_ms": 2000, "loop": true}],
  "sfx": [{"file": "sfx/pop.wav", "at": {"source": "C0103", "time": 41.30}, "gain_db": -8}]
}
```

- **Dialogue presets:**
  - `clean`: highpass plus a gentle compressor.
  - `broadcast`: highpass, de-esser, and firmer compression.
  - `none`.
  - `denoise: true` adds FFT denoise. Use it only when there is audible hiss or hum, and listen afterwards.
- **Music ducking:** speech is detected from the edited dialogue itself. The bed sits `bed_lu` below the dialogue under speech and rises to `gap_lu` in pauses longer than `min_gap_ms` (default 700), with `attack_ms` / `release_ms` ramps. Breaths don't pump it.
- **Music choice:** pick the music for the style recipe (energy, mood, phrase length). Start it on a beat that supports the hook, and fade it under the end card.
- **SFX:** accents only, one per payoff. Land them with `at` on the word or graphic they support.
- **Licensing:** use only music and SFX that the user supplied or approved. Never fetch tracks.
- **Split edits:**
  - `"j_cut_ms": 250` on a range starts its sound under the previous picture (anticipation, interviews).
  - `"l_cut_ms"` lets the previous sound run on under this range's picture (reactions, B-roll).
  - Neither changes the duration. The validator rejects both on the same cut.

## Camera moves (smooth keyframed zoom in / out, pans, face tracking)

Any range can carry a virtual camera move. It is rendered with per-frame cubic resampling from source pixels: subpixel-smooth, with no zoompan shake and no loss of 4K detail.

```json
{"source": "C0103", "start": 2.0, "end": 6.5, "zoom": {"from": 1.0, "to": 1.15, "ease": "smooth", "start": 0.5, "duration": 2.5}}
{"source": "C0103", "start": 9.0, "end": 14.0, "camera": [
  {"t": 0, "scale": 1.0}, {"t": 0.9, "scale": 1.15, "ease": "out"},
  {"t": 3.2, "scale": 1.15}, {"t": 4.6, "scale": 1.0, "ease": "in_out"}]}
{"source": "WIDE", "start": 20.0, "end": 28.0, "track": "face"}
```

- **`zoom`** is a single push-in (`to > from`) or pull-out (`from > to`). `start` and `duration` are seconds within the range; leave them out to move over the whole range.
- **`camera`** takes free keyframes. `t` is seconds from the range start, `scale` is 1.0–2.0, and the optional `x` / `y` set the window centre (0–1) for a pan. Each key's `ease` shapes the segment that ends at it.
- **Eases:**
  - `smooth` (default): no velocity or acceleration at either end. Reads as a camera operator.
  - `in_out`: firmer in the middle.
  - `out`: fast start, soft landing. Good for punches.
  - `in`, `linear`.
- **`track: "face"`** follows the face inside the shot. Detection runs every 0.2 s, and the path is low-passed with no lag. If the face barely moves the shot stays locked off, because a drifting camera reads as amateur. It combines with `zoom` / `camera` scale keys.
- **Taste:**
  - One move per beat.
  - A slow push-in (1.0 → 1.08–1.15 over 2–4 s) builds tension or intimacy. A pull-out releases or reveals.
  - Land a fast `out` punch on the word it emphasises.
  - Don't move the camera on every range. Moves stand out against stillness.
  - The window centre defaults to the face (with `anchor: "face"`) or to the range's anchors.

## Colour match between cameras

`"grade": {"base": "auto", "match": "A", "match_sources": ["B"], "skip": ["OUTRO"]}` matches the other cameras (or only `match_sources`) to source `A`; sources in `skip` (a client's finished outro, supplied B-roll) are left ungraded. It transfers per-channel levels and gamma (black point, white point, midtone, so the white balance matches too), and then applies one shared correction to all cameras. Use it when two angles, or an old and a new shoot, sit side by side. Check the skin tones in the QC strips.

## Multicam (sync + cut to the speaker)

1. Cut the story on the master source (the one with the diarized transcript) as usual.
2. Add a `multicam` block:

   ```json
   "multicam": {"master": "M", "angles": {"A": {"speaker": "speaker_0"}, "B": {"speaker": "speaker_1"}, "W": {"wide": true}},
                "min_shot_s": 1.5, "max_shot_s": 7.0, "switch_delay_s": 0.15}
   ```

3. Run `multicam.py sync edl.json`. It finds each angle's offset by audio cross-correlation, writes the offsets into the EDL, and prints a confidence; check low ones by eye.
4. Run `multicam.py switch edl.json -o edl_multicam.json`. Every master range becomes shots on the speaker's angle:
   - interjections shorter than `min_shot_s` don't get a cut;
   - a monologue longer than `max_shot_s` gets a wide break.
5. Each shot keeps the master's continuous sound (`audio_source`), so picture cuts never touch the dialogue.

`audio_source: {"source": "M", "start": 12.3}` can be set by hand on any range too, for example a picture from one take over the sound of another. Ranges whose sound is continuous in one source become one audio part, with no crossfade bump.

## Music & SFX library

`library.py` keeps the user's own licensed music and SFX:

- `scan <folder> --type music --license "<where it is licensed>"` measures tempo, energy, brightness, and loudness.
- `tag <id> --mood uplifting,tech` adds mood tags.
- `search` and `suggest --recipe kinetic_promo --duration 45` rank tracks for the chosen style recipe.

Reference a track in the EDL as `"file": "library:<id>"`. Offer the user two contrasting suggestions to listen to, and make sure every delivered track has a licence on record.

## Transitions, framing, and look

- **Transitions:** set `"transition": {"type": "dissolve", "duration_ms": 400}` on the range a transition leads *into*.
  - Types: `dissolve`, `dip_black`, `dip_white`, `whip`, `slide`, `zoom`, `wipe`, `light`.
  - The clip is built from handles on both sides and laid centred over the cut, so the length and audio sync never change.
  - `light` is a hard cut with a coloured light flash screened over it: it builds over ~6 frames, peaks on the first frame of the new shot, then leaves a soft after-glow (`duration_ms` default 470). It needs no handle media. The colour comes from `color`, else the brand kit's `transitions.light.color`, else a warm leak.
  - `fade` brings the incoming shot up from black (or `color`) over `in_ms` (default 500), and can first take the outgoing shot down over `out_ms`. It is picture only, leaves the sound alone, and needs no handles. Use it for an end card or outro that should "appear".
  - `"audio_gain_db": -3` on a range trims that range's sound before the mix. Use it to level-match a supplied asset, such as a client outro that is mastered louder than the dialogue.
  - **House rule for B-roll:** `"transitions": {"broll": {"type": "light"}}` in the EDL, or in the brand kit, puts that transition on every switch between a B-roll cutaway (a range whose `audio_source` is another source, or `"broll": true`) and the main footage. A range's own `transition` wins, and `"transition": "cut"` forces a hard cut.
  - `analyze_references.py` finds light flashes in reference videos by itself (`light_flashes` per file; `summary.light_transition` with count, colour, and a `brand_kit_suggestion`). When most of a client's references flash in and out of cutaways, copy that colour into the brand kit and set `transitions.broll`.
  - Land B-roll **on the word it illustrates**: start the cutaway at that word's start time in the transcript, not at the end of the phrase.
  - The default is still a hard cut. Use a transition only for a real change of time, place, or section, never to hide a weak cut.
- **Face anchor:** `source_framing.X.anchor: "face"` (or `punch_in.anchor: "face"`) centres every crop on the face. It uses local macOS Vision, takes a median per range, and caches results in `<edit>/faces/`. This is the right default for 16:9 → 9:16 / 4:5 reframes of talking heads.
- **Auto punch:** `"auto_punch": {"scale": 1.1}` (or `true`) alternates framing on jump cuts (consecutive ranges of one take), anchored on the face. A hand-set `punch_in` always wins.
- **LUT look:** `"grade": {"base": "auto", "lut": "looks/teal.cube", "intensity": 0.6}` applies the per-source auto correction first, then a creative LUT blended at `intensity`.

## Delivery versions and hand-off

- `"deliverables": [{"name": "feed_4x5", "canvas": {"width": 1080, "height": 1350}}]`, then run `run_edit.py ... --deliverables`. Every version reuses the same cut. Graphics and captions are relative, so they re-lay automatically, and each version gets its own QC report. A deliverable may override `source_framing` and `captions`.
- `render.py --canvas 1080x1080` renders a single other canvas.
- `export_timeline.py edl.json` writes a CMX3600 `.edl` and an FCP7 `.xml` (Premiere, Resolve, Avid) with source timecode and file paths, for a finishing editor.
- `compare_takes.py --edit-dir edit` finds lines spoken in several takes and ranks them by completeness, fillers, hesitations, and pace. Read `take_comparison.md` before choosing takes in a multi-take shoot.

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` conforms every segment to ONE canvas: the EDL `canvas` (`{"width": 1080, "height": 1920}`) or, by default, 1920×1080 / 1080×1920 following the first source's orientation. Per-source `source_framing` chooses `cover` (crop at `anchor_x`/`anchor_y`, default), `fit` (bars), or `blur_fit` (fit over a blurred fill). Mixed-orientation sources are allowed; the validator warns when a cover crop on a mismatched source has no chosen anchor. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."},
    {"source": "C0108", "start": 31.20, "end": 35.00,
     "beat": "PROOF", "quote": "...", "reason": "Key demonstration.",
     "punch_in": {"scale": 1.08, "anchor_y": 0.4, "reason": "Draws focus to the proof without cropping the UI."},
     "transition": {"type": "dip_black", "duration_ms": 500}, "j_cut_ms": 200}
  ],
  "canvas": {"width": 1080, "height": 1920},
  "source_framing": {"C0108": {"mode": "cover", "anchor": "face"}},
  "auto_punch": {"scale": 1.1},
  "audio": {"crossfade_ms": 30, "dialogue": {"preset": "clean"},
            "music": [{"file": "music/bed.mp3", "bed_lu": -20, "gap_lu": -9}]},
  "grade": "warm_cinematic",
  "graphics": [{"type": "number", "text": "5", "label": "days", "at": {"source": "C0108", "time": 15.1}, "duration": 1.8}],
  "captions": true,
  "deliverables": [{"name": "feed_4x5", "canvas": {"width": 1080, "height": 1350}}],
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "total_duration_s": 87.4
}
```

`grade` is a preset name, a raw ffmpeg filter, `auto` (a bounded correction measured once per source, so every range of one take shares it), or `{base, lut, intensity}`. Every key besides `sources` and `ranges` is optional. `overlays` are rendered animation clips. `subtitles` (an explicit file) or `captions` (brand-styled, built from transcripts) is applied LAST.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `session_state.json`, `edit_brief.json`, and `style_profile.json` first. Then read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue. Structured state is authoritative for current status and user preferences; prose explains past judgment.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Running Whisper locally on CPU.** Slow and it normalizes fillers. Use hosted Scribe.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Unverified web fonts.** A failed load silently falls back to a system face. Assert the font loaded before rendering.
- **Stock SFX on every transition.** Tie each effect to a visible event; cap the count.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
