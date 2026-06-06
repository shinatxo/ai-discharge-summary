# Demo video — shot script (45–60s)

A short, silent-with-captions clip for the README and portfolio card (and a captioned
MP4 for LinkedIn). The goal is to show, fast, that this is a *real, deployed* system whose
defining trait is **restraint** — it surfaces gaps instead of inventing them.

Keep it under a minute. Cut or speed-up the generation wait (nobody watches a spinner).

## Before you record
- Use a clean browser window (~1280×800), no bookmarks bar, no extensions visible.
- Pick a scenario with **obvious gaps** so "Not documented" appears on screen — that's the
  money shot. A good choice from [`evals/scenarios/`](../evals/scenarios/): a note that
  omits allergies or discharge meds. (Optional bonus clip: the adversarial non-English case,
  which makes the leaflet lead with the translation flag — a vivid safety moment.)
- Have the notes already copied to your clipboard so the paste is instant.

## Shot list

| # | Time | On screen | Caption (bold overlay) |
|---|------|-----------|------------------------|
| 1 | 0–4s | Browser at `discharge.shinaoguntoye.dev`, Cognito sign-in, you log in | **A real, deployed clinical-AI tool — auth-gated, audited** |
| 2 | 4–9s | Paste the messy ward-round notes into the input box | **Messy ward-round notes in…** |
| 3 | 9–13s | Click **Generate**; brief "generating" state (trim the wait) | **…async generation (202 + poll, no 30s cap)** |
| 4 | 13–22s | Three output tabs appear — click through: Discharge summary → GP letter → Patient version | **Three drafts: summary · GP letter · plain-English patient version** |
| 5 | 22–32s | Scroll to a field the notes didn't cover; it reads **"Not documented"**. Hover/point. | **It writes "Not documented" instead of inventing — restraint is the point** |
| 6 | 32–40s | Show the patient tab's plain-English reading level + the documented safety-net line | **Patient version at reading-age ≤ 8, safety advice only if the clinician documented it** |
| 7 | 40–48s | Tick **"I have reviewed and edited this output"**, which unlocks download | **Every output is a draft — the clinician signs off and stays the author** |
| 8 | 48–55s | End card (static): logo/title + URL + repo | **discharge.shinaoguntoye.dev · synthetic data · not a medical device · github.com/shinatxo/ai-discharge-summary** |

## Optional 6-second bonus (great for LinkedIn)
Run the **adversarial non-English-speaker** scenario and show the patient leaflet opening with
*"FOR TRANSLATION — do not hand to the patient untranslated."* Caption: **Adversarial case: it
refuses to hand an English leaflet to a non-English speaker.** This single clip demonstrates the
safety behaviour better than any paragraph.

## Recording + export
- **macOS:** `Cmd + Shift + 5` → record the browser window. QuickTime also works.
- Trim dead air; speed up shot 3 (the generation wait) to ~1–2s.
- **For the README:** export a **muted, looping GIF**, ~1000px wide, target **< 5 MB** (GitHub
  inlines it). For **LinkedIn:** export **MP4 with burned-in captions** (most people watch muted).

## Hand it to Cowork for the GIF
Drop the screen recording (`.mov`/`.mp4`) into the project folder and I'll convert + optimise it
to a README-ready GIF with `ffmpeg` (palette-optimised, sized, under the size budget), then wire
it into the README and the portfolio card. The command I'll run is roughly:

```bash
ffmpeg -i demo.mov -vf "fps=12,scale=1000:-1:flags=lanczos,palettegen" palette.png
ffmpeg -i demo.mov -i palette.png -lavfi "fps=12,scale=1000:-1:flags=lanczos[x];[x][1:v]paletteuse" docs/demo.gif
```

Then it embeds at the top of the README as `![Demo](docs/demo.gif)`.
