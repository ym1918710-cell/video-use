# Brand identity

A brand identity is the set of tokens that dresses any [style recipe](style-library.md)
for one client. It is data the user's project owns. It never lives in the Video Use
repo, and no brand is ever the default.

## Where it lives

- Each project has `<edit>/brand_kit.json`. `session_state.py` creates it empty.
- A client's kit is reusable: the user can keep a master copy (for example
  `~/brands/<client>/brand_kit.json` next to its logo and font files) and copy it into
  each new project's edit folder. To find it, ask the user where it is. Never search
  their disk for other clients' material.
- If the kit is empty, the edit is brand-neutral: system fonts, white type with a soft
  shadow, no logo. Don't invent brand colours.

## Schema

```json
{
  "version": 1,
  "client": "",
  "personality": ["e.g. bold", "technical", "playful", "premium"],
  "colors": {"primary": null, "secondary": null, "accent": null, "text": "#FFFFFF", "background": null},
  "gradients": [],
  "fonts": {"headline": null, "body": null, "caption": null, "arabic": null},
  "logo": {"file": null, "placement": null, "persistent": false, "behind_subject_ok": true},
  "shapes": {"sticker": null, "corner_radius": null, "stroke": null, "shadow": null},
  "caption_skin": {"position": null, "box": false, "box_opacity": null, "size_ratio": null, "highlight": null},
  "motion": {"energy": null, "pop_in_ms": null, "transitions": []},
  "audio": {"loudness_lufs": -14, "true_peak_dbtp": -1, "music_mood": null, "sfx_ok": true},
  "end_card": {"file": null, "duration_s": null},
  "preferred_recipes": [],
  "do": [],
  "dont": [],
  "references": []
}
```

Every field is optional. A `null` means "not decided", not "use a default".

## Deriving a kit from reference exports

1. Run `helpers/analyze_references.py` on the client's finished videos. It reports
   dominant colours, loudness, and pacing. Treat these as **measurements, not
   decisions**.
2. Look at frames to read the fonts, sticker shapes, logo placement, and caption skin.
   Name the fonts only if the user confirms them or provides the files.
3. Fill in the kit, with `references` listing the analysed files. Show the user a
   short summary and get approval before using it in a render.
4. Bad habits in a reference (clipping at 0 dBTP, captions outside the safe area) are
   not part of the identity. Match the client's look, not their mistakes.

## Using a kit

Field reference for the graphics renderer (`helpers/graphics.py`):

| Field | Effect |
|---|---|
| `colors.primary` | Turns on branded styling: sticker text, number/headline extrusion, lower-third fill |
| `colors.accent` | Glow around numbers and headlines |
| `colors.text`, `colors.background` | Light type, end-card background |
| `gradients[0]` | Sticker outline (falls back to primary→secondary) |
| `fonts.headline` / `fonts.body` / `fonts.arabic` | Family name, face name, or a `.ttf`/`.otf` path relative to the kit. Arabic text uses `fonts.arabic` |
| `shapes.sticker` | `fill`, `text`, `stroke` (list of colours), `stroke_ratio`, `radius` (`"pill"` or a fraction of the short side), `tilt` |
| `shapes.number` / `shapes.headline` | `extrude` (bool), `color` |
| `shapes.lower_third` | `fill`, `text`, `radius_ratio` |
| `logo` | `file` (PNG with transparency; padding is trimmed), `placement`, `width_ratio`, `opacity` |
| `end_card.file` | An alternative logo for the end card |
| `motion` | `energy` (`calm` / `medium` / `high`) or an explicit `pop_in_ms` |


- Graphics, stickers, captions, and end cards take their colours, fonts, and shapes
  from the kit. A style recipe only decides **when** and **how much**.
- Flag conflicts. For example, a "premium/serious" personality and a request for the
  kinetic recipe usually means a restrained version of kinetic: fewer stickers, slower
  pops.
- When the user approves a new choice repeatedly ("always box the captions"), suggest
  adding it to the client kit so the next project starts there.
