# Video Use: plan to reach senior-level editing

Goal: a tool that edits any video, for any client and any identity, at a senior
editor's level. It reads the material, proposes a fitting style, dresses that
style in the client's identity, and checks its own work before showing it to
anyone.

Principles that hold for everything below:
- **General, not specific.** Craft lives in the repo. A client's identity lives in
  the project's `brand_kit.json`.
- **Frame-exact.** No feature may change the timeline length or move audio away
  from the picture.
- **Every new EDL key is optional.** An old EDL renders exactly as it did before.
- **Every feature has a validator rule, a test, and a paragraph in SKILL.md.**

## Done
| Phase | Content |
|---|---|
| P0 render quality | Equal-power crossfades, single AAC encode, frame-exact timing, one canvas + framing, per-source grade, canvas-aware Arabic captions, QC fix |
| Style system | `style-library.md`, `brand-identity.md`, `brand_kit.json`, `analyze_references.py` |
| P1 graphics | `graphics`: sticker, title_card, number, headline, lower_third, logo, end_card. CoreText rendering, behind-subject placement, timing by word |

## Round 2 — done (all items below shipped together)

### A. Audio (`helpers/audio_mix.py`)
| # | Feature | EDL | Approach |
|---|---|---|---|
| A1 | Music bed with speech-aware ducking | `audio.music[]` | Find speech from the dialogue master itself (RMS against an adaptive threshold). Build a gain envelope: duck under speech, rise in gaps of at least `min_gap_ms`, with attack/release ramps. Levels are relative to the dialogue's loudness (`bed_lu`, `gap_lu`) after measuring the track's own LUFS. Supports loop, fade in/out, `source_start`. Smooth envelope, no pumping. |
| A2 | SFX layer | `audio.sfx[]` | Placement by `start_in_output` or `at` (the word), with `gain_db`. |
| A3 | Dialogue chain | `audio.dialogue` | Presets `clean` / `broadcast` / `none`: highpass, optional denoise (afftdn), de-esser, gentle compressor. |
| A4 | J/L cuts | `ranges[i].j_cut_ms` / `l_cut_ms` | Moves the audio cut point at the boundary into range i, earlier (J) or later (L), without changing duration. |
| A5 | Stems | automatic | `dialogue.wav`, `music.wav`, `sfx.wav` next to the render, for the client or for remixing. |

### B. Visual
| # | Feature | EDL | Approach |
|---|---|---|---|
| B1 | Transitions | `ranges[i].transition` `{type, duration_ms}` | `dissolve`, `dip_black`, `dip_white`, `whip`, `zoom`, `slide`. Built as a short transition clip from handles on both sides (xfade) and laid over the boundary as an opaque overlay, so the timeline length doesn't change. |
| B2 | Face anchor | `source_framing.X.anchor: "face"` / `punch_in.anchor: "face"` | macOS Vision face detection (`face_anchor.swift`), cached per source, median per range. Gives a smart 16:9→9:16 reframe for each shot. |
| B3 | Automatic punch-in on jump cuts | `auto_punch: {scale, min_gap_s}` | Two consecutive ranges from one source with a jump between them get alternating framing (1.0 ↔ scale), anchored on the face. A punch-in set by hand always wins. |
| B4 | LUT + intensity | `grade: {"lut": "x.cube", "intensity": 0.7, "base": "auto"}` | lut3d (tetrahedral) blended with the original. |

### C. Captions
| # | Feature | Approach |
|---|---|---|
| C1 | Caption skin from the brand | `brand_kit.caption_skin`: position (0–1), size_ratio, box + opacity, colours, font, `max_words`. Default without a brand: the current style. |
| C2 | Word highlight | `caption_skin.highlight: "#HEX"`. Each word is coloured as it is spoken (one ASS event per word inside the cue). |
| C3 | Graphics yield | When a graphic is on screen in the same area as the captions, the captions move up or down automatically. |

### D. QC (`verify_render.py`)
| # | Check |
|---|---|
| D1 | Final integrated LUFS and true peak, with a tolerance around the target |
| D2 | freezedetect / blackdetect across the whole timeline (not just at the cuts) |
| D3 | Caption reading speed (CPS) and minimum cue duration |
| D4 | Pacing report: shot lengths, words per minute, longest stretch with no visual change, number of graphics per minute |

### E. Delivery and workflow
| # | Feature | Approach |
|---|---|---|
| E1 | Multiple versions | `deliverables: [{name, canvas, framing?}]`, plus `render.py --canvas WxH`. `run_edit.py --deliverables` renders them all. |
| E2 | Hand-off to an NLE | `export_timeline.py` → CMX3600 `.edl` and FCP7 `.xml`, for Premiere and Resolve with automatic relink. |
| E3 | Take comparison | `compare_takes.py`: finds repeated phrases across takes, then ranks them by length, fillers, and pace. |

## Later (needs assets or a bigger decision)
- Moving face tracking inside a single shot (dynamic crop).
- A licensed music/SFX library with mood tags. For now we only use files the user supplies.
- Automatic shot matching between two different cameras.
- Speaker diarization for automatic switching between multicam angles.

## Definition of done (met)
- All tests pass, plus new tests for every item.
- A real render on actual footage with music + ducking + a transition + a face-anchored punch-in + brand captions, and the QC passes.
- SKILL.md documents every key with an example.
