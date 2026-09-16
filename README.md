# chantey

**Sheet music → sound & views.** chantey turns written music into things you
can hear and compare: PDF or MusicXML in; audio in two voices, notation as
PDF/SVG, measure-level diffs, and a side-by-side comparison page out.

Chanteys kept a ship's crew working in unison while making the work lighter.
So does this one: human and AI work from one canonical score, and "change
the chord in measures 12–16" comes back as new sound, new notation, and a
page showing exactly what changed.

## The one thing that matters

> **The PDF is a rendering, not the source.** One canonical `.musicxml` file
> per piece is the single source of truth; audio and notation are renders of
> it. Edit the source and everything regenerates consistently.

## Install the prerequisites

chantey wraps existing engines (checked at use, with actionable errors):

```bash
sudo apt install musescore3 fluidsynth fluid-soundfont-gm   # PDF export + piano voice
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install -y --user flathub org.audiveris.audiveris   # OMR (PDF → MusicXML)
uv tool install git+https://github.com/mr-kelley/hornpipe   # deterministic synth voice
```

chantey itself carries heavy Python dependencies (`music21`, `verovio`).
Install it with `uv tool install git+https://github.com/mr-kelley/chantey`,
or run it from a clone: `uv sync`, then `uv run chantey …`.

## The workflow

```bash
chantey ingest tune.mxl                  # → tune.musicxml (canonical; OMR for PDFs)
chantey info tune.musicxml               # parts, measures, keys, tempos, repeats
#   ... the role applies the requested edit as a music21 script,
#       writing revised.musicxml ...
chantey diff tune.musicxml revised.musicxml      # → "changed measures: 7, 15, 56..."
chantey render tune.musicxml                     # → .piano.wav  .synth.toml/.wav  .pdf
chantey render revised.musicxml
chantey compare tune.musicxml revised.musicxml   # → compare.html (open in a browser)
```

The compare page shows both versions engraved side by side with **every
measure numbered**, the changed measures (found by `diff`, never
hand-listed) highlighted in crimson in both columns, and players for all
rendered audio.

## Commands

| Command | Does |
|---------|------|
| `info <score>` | Structural summary: parts, measures, keys/times/tempos, note count, repeats. |
| `ingest <pdf\|mxl\|musicxml> [-o out]` | Canonical `.musicxml`. PDFs go through Audiveris OMR (headless; chantey handles the flatpak `--filesystem` plumbing) and get a proofing reminder — born-digital MusicXML is always preferred. |
| `render <score> [--voice piano\|synth\|both] [--no-pdf] [-o dir]` | `<stem>.piano.wav` (FluidSynth), `<stem>.synth.toml` + `.wav` (hornpipe), `<stem>.pdf` (MuseScore). |
| `diff <a> <b> [-v]` | Measures whose notes differ, per part and overall. |
| `compare <a> <b> [-o out.html]` | The side-by-side page. Renders nothing itself — run `render` first for audio players. |

All outputs are **non-clobbering** (`--force` to overwrite). Exit codes:
0 success, 1 error, 2 usage.

## Two behaviors you can rely on (and why)

- **Both audio voices always agree about form.** Repeats/voltas are expanded
  once and the same expansion feeds the MIDI and synth paths. (The walking
  skeleton caught these disagreeing: ~180 s vs ~142 s on the same piece.)
- **Tempo changes just work in the synth voice.** hornpipe has a single
  tempo, so the converter resolves every metronome mark itself and emits a
  score at `tempo = 60` — one beat = one second; all timing is baked in.

## Editing (deliberately not a command)

Musical edits are music21 scripts written per request — the skeleton showed
every real request reduces to *match a shape, transform it, report the
measures*. Worked example (the request was: "the bass figure B♭, B♭, C, D in
eighths — split into twinned sixteenths, except the first B♭"):

```python
from music21 import converter, beam
import copy

s = converter.parse("tune.musicxml")
for meas in s.parts[1].getElementsByClass("Measure"):
    notes = [el for el in meas.notesAndRests if el.isNote]
    for i in range(len(notes) - 3):
        run = notes[i : i + 4]
        if any(float(n.duration.quarterLength) != 0.5 for n in run):
            continue
        ps = [n.pitch.midi for n in run]
        if ps[1] - ps[0] == 12 and ps[2] - ps[1] in (1, 2) and ps[3] - ps[2] in (1, 2):
            for el in run[1:]:                      # first note keeps its eighth
                el.duration.quarterLength = 0.25
                el.beams = beam.Beams()
                meas.insert(float(el.offset) + 0.25, copy.deepcopy(el))
s.write("musicxml", fp="revised.musicxml")
```

Then `chantey diff` confirms which measures changed before anything renders.

## Licensed material — the rules

Much sheet music is copyrighted (including modern arrangements of
public-domain tunes — check the fine print). chantey is built for **private,
local work on lawfully obtained copies**:

- Keep working scores and derived artifacts in a directory your version
  control ignores, so licensed material never enters repository history.
- chantey never publishes, uploads, or hosts anything; the compare page is a
  local file. Sharing decisions are the operator's alone.

## OMR proofing (when you must start from PDF)

Audiveris is good on clean, born-digital engravings and worse on scans.
After `ingest` from PDF, always proof: render the result back
(`chantey render --no-pdf --voice piano` + the PDF, or open the page) and
compare against the original by eye and ear. Known loss classes: tempo and
expression text, fingerings (occasionally misread as dynamics), articulation
glyphs. Fix what matters in the canonical MusicXML — it is the source now.

## Manual pages (man + info)

Full reference docs ship in `docs/`: a man(1) page (`docs/man/chantey.1`)
and a Texinfo manual (`docs/info/chantey.texi`) — the manual covers the
workflow and concepts in tutorial form, including the worked edit example.
Wheels can't usefully carry man/info pages, so install them alongside the
tool (run from this directory):

**User-level (no sudo)** — `man` finds `~/.local/share/man` automatically
once it exists (man-db derives it from `~/.local/bin` on `PATH`):

```bash
install -Dm0644 docs/man/chantey.1 ~/.local/share/man/man1/chantey.1
makeinfo --no-split -o /tmp/chantey.info docs/info/chantey.texi
install -Dm0644 /tmp/chantey.info ~/.local/share/info/chantey.info
install-info ~/.local/share/info/chantey.info ~/.local/share/info/dir
```

Usually no `INFOPATH` is needed: like man-db, modern texinfo derives its
search path from `PATH` (`~/.local/bin` → `~/.local/share/info`). On an
older system where `info` comes up empty,
`export INFOPATH="$HOME/.local/share/info:"` in your profile fixes it.

**System-wide (sudo):**

```bash
sudo install -Dm0644 docs/man/chantey.1 /usr/local/share/man/man1/chantey.1 && sudo mandb -q
sudo makeinfo --no-split -o /usr/local/share/info/chantey.info docs/info/chantey.texi
sudo install-info /usr/local/share/info/chantey.info /usr/local/share/info/dir
```

Keep `docs/` in sync with the CLI when the command surface changes.

## Develop

```bash
uv sync
uv run pytest          # externals mocked; music21/verovio real on tiny fixtures
uv run ruff check . && uv run ruff format --check .
```
