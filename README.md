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

`--engine sfizz` also needs `sfizz_render` (the sfztools `sfizz` package)
and the SFZ libraries you map to your parts.

## Install

chantey itself carries heavy Python dependencies (`music21`, `verovio`).
Install it with `uv tool install git+https://github.com/mr-kelley/chantey`,
or run it from a clone: `uv sync`, then `uv run chantey …`.

## The workflow

```bash
chantey ingest tune.mxl                  # → tune.musicxml (canonical; OMR for PDFs)
chantey info tune.musicxml               # parts, measures, keys, tempos, repeats
#   ... apply the requested edit as a music21 script,
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
| `info <score> [--sfz path]` | Structural summary: parts (measures, and quarter lengths as rendered), keys/times/tempos, note count, repeats. Under each part with an SFZ (its `CHANTEY_SFZ_MAP` entry, else `--sfz`), that SFZ's keyswitch map — see [Keyswitches](#keyswitches-keyswitch-curves). |
| `ingest <pdf\|mxl\|musicxml> [-o out]` | Canonical `.musicxml`. PDFs go through Audiveris OMR (headless; chantey handles the flatpak `--filesystem` plumbing) and get a proofing reminder — born-digital MusicXML is always preferred. |
| `render <score> [--voice piano\|synth\|both] [--engine fluidsynth\|sfizz] [--sfz path] [--fx dry\|light\|default] [--expression file] [--no-pdf] [-o dir]` | `<stem>.piano.wav` (FluidSynth, dry by default — see [Piano-voice effects](#piano-voice-effects---fx); or sfizz, one SFZ per part — see [Piano-voice engine](#piano-voice-engine---engine); controller curves from a sidecar — see [Expression sidecar](#expression-sidecar---expression)), `<stem>.synth.toml` + `.wav` (hornpipe), `<stem>.pdf` (MuseScore). |
| `diff <a> <b> [-v]` | Measures whose notes differ, per part and overall. |
| `compare <a> <b> [-o out.html]` | The side-by-side page. Renders nothing itself — run `render` first for audio players. |

All outputs are **non-clobbering** (`--force` to overwrite). Exit codes:
0 success, 1 error, 2 usage, 128 + n stopped by signal n during an sfizz
render.

## Piano-voice effects (`--fx`)

The piano voice renders **dry** by default: FluidSynth's chorus and reverb
are off. Its own defaults (a three-voice chorus, reverb at level 0.9) bake
space into every render, and a stem with reverb in it cannot be dried out
again on the mix. Add space on the mix, or ask for it explicitly:

| `--fx` | FluidSynth arguments added | Use |
|--------|----------------------------|-----|
| `dry` (default) | `-C 0 -R 0` | Stems for mixing. |
| `light` | `-C 0 -o synth.reverb.active=1 -o synth.reverb.level=0.3 -o synth.reverb.room-size=0.3` | A listening render with a little room. |
| `default` | none | FluidSynth's defaults; how chantey 0.1 rendered. |

`CHANTEY_FLUIDSYNTH_OPTS` (optional) holds extra FluidSynth arguments. They
are split shell-style and appended after the preset, so they win where they
overlap. chantey forwards them unchecked; whether FluidSynth accepts them is
the caller's concern.

```bash
chantey render tune.musicxml --voice piano --fx light
CHANTEY_FLUIDSYNTH_OPTS='-o synth.reverb.level=0.2' chantey render tune.musicxml --fx light
```

The setting in effect is printed on the piano voice's output line, so a log
of the render records it:

```
Wrote tune.piano.wav  [fx: dry]
Wrote tune.piano.wav  [fx: light; CHANTEY_FLUIDSYNTH_OPTS: -o synth.reverb.level=0.2]
```

`--fx` affects the piano voice only; the synth voice and the PDF are
unchanged.

## Piano-voice engine (`--engine`)

A General MIDI SoundFont has one or two velocity layers and no articulation
variety, so its realism has a ceiling. `--engine sfizz` renders the piano
voice through [sfizz](https://sfz.tools/sfizz/) instead, with **one SFZ
instrument per part**. Free multi-sampled SFZ libraries, such as Virtual
Playing Orchestra, VSCO 2 CE and Salamander Grand Piano, are the point.
`--engine fluidsynth` is the default and is unchanged.

**Mapping.** Every part needs an SFZ:

- `CHANTEY_SFZ_MAP="<part-name>=<path>;<part-name>=<path>"` maps parts by
  their MusicXML `part-name`, matched exactly first, then case-insensitively.
  Whitespace around names and paths is stripped. Two map keys that match
  the same part only case-insensitively are an error, and so is a key
  mapped twice. An entry that matches no part is a warning.
- `--sfz PATH` is the SFZ for every part the map leaves unmapped. The map
  wins per part.
- A part with neither is an **error that names the part**. chantey never
  falls back to FluidSynth. A missing SFZ file is an error before anything
  renders. `--sfz` without `--engine sfizz` is an error.

```bash
chantey render saints.musicxml --voice piano --engine sfizz --sfz /lib/vpo/Piano.sfz
CHANTEY_SFZ_MAP='Trumpet=/lib/vpo/Trumpet.sfz;Sousaphone=/lib/vpo/Tuba.sfz' \
  chantey render saints.musicxml --voice piano --engine sfizz --no-pdf
```

**Rendering.** The score's MIDI is written once, exactly as the FluidSynth
path writes it, with the same repeat expansion. It is then split, so each
part's file holds the conductor track (tempo map, meters, keys) and that
part's track, byte for byte. Each part is rendered with:

```
sfizz_render --sfz <sfz> --midi <part.mid> --wav <part.wav> -s 44100 -b 256 -q 10 --use-eot
```

The parts are summed at unity gain into `<stem>.piano.wav`, 16-bit at
44.1 kHz. If the sum would exceed −0.1 dBFS, the whole mix is scaled down to
−0.1 dBFS and the factor is printed.

**Every render ends at the score's end plus 2 s (v0.4).** Without
`--use-eot`, sfizz_render keeps rendering until every voice is silent. In
sfizz 1.2.3 that can be never. A patch with `delay_random` over a looped
sample (Virtual Playing Orchestra uses both on every instrument) can lose
one note's note-off, and that voice then loops forever. On 2026-10-21 that
wrote a 37 GB stem and filled the disk. Which
note is lost depends on sfizz's random state, so editing the score cannot
avoid it. chantey therefore bounds each render twice:

- **Length.** Each part's MIDI ends at the score's notated end (trailing
  rests included) plus a 2 s tail, at the tempo in force there, so the last
  release is not cut. The longest `ampeg_release` in VPO and Salamander is
  1 s. sfizz_render 1.2.3's `--use-eot` stops at the last event *before* the
  End-of-Track and ignores the End-of-Track's own delta, so the tail is
  carried by an inert text event (`chantey: end of score + tail`) in the
  conductor track, with every End-of-Track moved to the same tick. Every
  part is therefore the same length, the score's.
- **Disk.** Each `sfizz_render` runs under a file-size limit
  (`RLIMIT_FSIZE`) of twice the bytes that length should take, plus 1 MiB.
  Reaching it kills the render, and chantey stops with an error that names
  the part and the engine. It does not truncate silently: a stem that
  reached the cap is wrong, with one note ringing to the end. It writes
  nothing.

A part whose voice was lost still ends on time under `--use-eot`, but that
one note rings to the end of the stem. That is an sfizz defect, and the
workaround is upstream of chantey: render from patch copies with
`delay_random` removed.

**Working files.** Per-part MIDI and stems go in a `chantey-sfizz-*`
directory under `$CHANTEY_TMPDIR`. If that is unset, `$TMPDIR` is used, and
otherwise the system default. Point `CHANTEY_TMPDIR` at a big filesystem for
long scores; a variable that names no directory is an error. The directory
is removed on success, on any error, and on SIGINT, SIGTERM or SIGHUP. A
signal exits 128 + its number, after the render in flight is killed. Only
SIGKILL or a power loss leaves it behind.

**The output line** records the engine, the mapping actually used and the
bound:

```
Wrote saints.piano.wav  [engine: sfizz; sfz: Trumpet=/lib/vpo/Trumpet.sfz, Sousaphone=/lib/vpo/Tuba.sfz; eot: +2.0 s; cap: 68.1 MB per part]
Wrote saints.piano.wav  [engine: sfizz; sfz: Piano=/lib/sgp/Piano.sfz; eot: +2.0 s; cap: 68.1 MB per part; --fx light ignored (FluidSynth only)]
Wrote saints.piano.wav  [engine: sfizz; sfz: …; eot: +2.0 s; cap: 68.1 MB per part; mix peaked at +2.31 dBFS, scaled by 0.7586 to -0.1 dBFS]
```

A capped render fails like this, and nothing is written:

```
chantey: sfizz_render (Clarinet) was stopped at chantey's 68,086,288-byte output cap: the score plus its 2 s tail is 190.0 s, but the engine kept rendering past it. The stem is wrong (most likely an sfizz voice that never released), so nothing was written; the cap only protects the disk
```

`--fx` and `CHANTEY_FLUIDSYNTH_OPTS` are FluidSynth settings: under
`sfizz` they are ignored, and the line says so when either was given.

**Quality setting: `-q 10` (flagged, to confirm).** `-q` sets sfizz's
sample (resampling) quality. sfizz documents the scale as 0–10, and 10 is
the top. An offline render has no realtime budget to protect, so chantey
asks for the best. The scale comes from sfizz's documentation: this
sfizz_render's `--help` says only "Resampling quality" (read in 0.4). What
the suite proves is that sfizz_render 1.2.3 accepts `-q 10` and renders
deterministically with it. It cannot tell whether the value is clamped
lower. This is a choice, not a measurement: no listening comparison
against sfizz_render's default has been made. The cost is render time on
resampled regions.

**Determinism: the block size is pinned at `-b 256`.** The same score,
chantey version, engine and SFZ files give a bit-identical WAV. The test
suite renders twice through the real `sfizz_render` (1.2.3) and compares
bytes. `--use-eot` and the tail keep that: a repeat render is
byte-identical. sfizz processes envelopes and modulation per block, so the block
size is part of the render. chantey therefore fixes it rather than
inheriting sfizz_render's default, which could change between sfizz
releases. 256 is a size confirmed byte-identical across repeat renders.

sfizz prints `Enabling freewheeling` and `Cannot set current thread
scheduling parameters` on stderr. Both are harmless and do not fail the
render.

## Expression sidecar (`--expression`)

chantey's MIDI is music21's export of the score: notes, tempo, program
changes, and no controllers, so every held note sounds at one flat
dynamic. Sampled instruments are built around controllers. VPO's
`*-mod-wheel` patches crossfade their soft and loud layers on CC1, and every
patch honours CC11 (expression) and CC64 (sustain). MusicXML has no place
for controller data, so the curves come from a JSON **sidecar** and are
merged into each part's MIDI at render time (v0.5). Deciding the curves
(hairpins to CC11, and so on) is the caller's interpretation. chantey only
applies them.

```bash
chantey render saints.musicxml --voice piano --engine sfizz --expression saints.expr.json
```

**Format.**

```json
{"parts": {"Trumpet": {"bend_range": 1200, "curves": [
    {"cc": 11, "interp": "linear", "points": [[0, 90], [16, 90], [20, 127], [24, 80]]},
    {"cc": 1,  "interp": "linear", "points": [[0, 40], [64, 40], [66, 110]]},
    {"cc": 64, "interp": "hold",   "points": [[0, 0], [4, 127], [7.5, 0]]},
    {"bend": true, "interp": "linear", "points": [[32, 0], [32.5, -1200]]}]}}}
```

- `parts` is keyed by MusicXML `part-name`, matched as `CHANTEY_SFZ_MAP`
  matches: exactly, then case-insensitively. A part the sidecar does not
  name renders as before.
- A curve names its kind with exactly one key: `"cc": <0-119>` (120–127 are
  channel-mode messages and are refused), `"bend": true`, or (v0.6, sfizz
  only) `"keyswitch": true` — see [Keyswitches](#keyswitches-keyswitch-curves) —
  or (v0.7) `"time": true` — see [Timing](#timing-time-curves).
  A new kind is a new key, so older files keep their meaning.
- `points` are `[offset, value]`. The offset is in quarter lengths on the
  **rendered** timeline, with repeats expanded, which is the timeline the
  tempo map applies to. The sidecar never needs to know a tempo.
  `chantey info` prints each part's length in quarters, as rendered and,
  when repeats change it, as written. A `cc` value is 0–127. A `bend` value
  is in cents, within ±`bend_range`. `bend_range` defaults to 200 and is
  set per part. It must match the bend range of the instrument that plays
  the part, because chantey sends no range message and only scales cents
  to the 14-bit bend value with it.
- `interp`: `hold` sends one event per point. `linear` sends one event per
  step between points. The step is 1/32 quarter or 10 ms at the tempo
  there, whichever is coarser. Before the first point, the first value
  holds from 0. After the last point, the last value holds. An event whose
  value repeats the previous one is dropped, so a flat stretch costs
  nothing. Two points at the same offset make a jump.

**Checks.** All of these are errors with exit 1, and nothing is written.
Each error names the place in the file.

- A part-name that matches no part.
- A value out of range.
- An offset past the part's length plus the render tail (2 s at the final
  tempo; `chantey info` prints it in quarters).
- Offsets out of order.
- Two curves on one controller (or two bend, keyswitch or time curves)
  for one part.
- Unknown keys, at any level.
- For a keyswitch curve: a switch key inside the part's played range, or
  any keyswitch curve under FluidSynth (see
  [Keyswitches](#keyswitches-keyswitch-curves)).
- For a time curve: a value past ±`time_max`, or a shared channel under
  FluidSynth (see [Timing](#timing-time-curves)).

**Merge.** Events go into the part's own track, on the channel or channels
its notes use, at `round(offset × ticks-per-quarter)`. At a tick they share
with the notes, they come after every existing event except note-ons, which
they precede. So a note sounds with the value set at its tick, and a pedal
change lands after the note-off it shares a tick with. Values at offset 0
therefore precede the first note. Notes, program changes, tempi and every
other event keep their ticks, bytes and order. music21's centre pitch-bend
at tick 0 stays where it is, and a bend curve follows it. A controller that
the export already drives is an error, not a fight.

- **Under `--engine sfizz`** each part renders from its own file, so the
  curves shape that part alone.
- **Under FluidSynth** all parts share one file. music21 gives parts with no
  instrument, or the same one, the same MIDI channel. A part with curves
  that shares a channel with another part is therefore an error, because
  its controllers would shape the other part too. Give the parts distinct
  instruments, or use `--engine sfizz`.

**Output line.** A second bracket follows the engine fields:

```
Wrote saints.piano.wav  [engine: sfizz; sfz: …; eot: +2.0 s; cap: 68.1 MB per part]  [expression: saints.expr.json; parts: 3; events: 1412]
Wrote saints.synth.wav  [--expression ignored (piano voice only)]
```

`parts` counts the score parts that received a curve. `events` counts the
MIDI events added, once per channel. The synth voice ignores the sidecar,
and its line says so.

**Determinism.** Without `--expression`, a render is bit-identical to
0.4.0's. A sidecar whose `curves` are all empty adds no events and gives
the same bytes as no sidecar. The same score and sidecar give the same
WAV every time. The suite checks all three through the real
`sfizz_render`. It also renders a held note under a CC1 ramp through an
`xfin_locc1` crossfade (VPO's mod-wheel mechanism) and checks that the
level rises inside the note.

### Keyswitches (`keyswitch` curves)

A keyswitch patch holds several recordings of each note (sustained, short,
accented) and picks one with a note-on on a key below the playing range.
VPO 3.3's `trumpet-SEC-KS-C2.sfz` has `sw_lokey=c2 sw_hikey=e2`, with
`sw_last=c2` Sustain, `d2` Normal, `d#2` Accent and `e2` Staccato, and
`sw_default=c2`. A `keyswitch` curve (v0.6) picks the recording note by
note:

```json
{"parts": {"Trumpet": {"curves": [
    {"keyswitch": true, "interp": "hold", "points": [[0, 36], [16, 39], [20.5, 40]]},
    {"cc": 11, "interp": "linear", "points": [[0, 90], [16, 127]]}]}}}
```

- Each value is a MIDI key, 0–127 (an integer). At each point chantey sends
  that key's note-on, with velocity 100 or the curve's `"velocity"` (1–127),
  and its note-off one tick later. sfizz's `sw_last` follows the most recent
  switch note-on, so the switch does not need to be held.
- The switch comes **before** any note-on of the part at its tick, so the
  note there plays the new recording. Like a controller, it comes after the
  note-offs at that tick.
- A keyswitch is sent **at its points only**. Unlike a controller, it is not
  sent at 0 before the first point: before the first switch, the SFZ's
  `sw_default` plays. To start somewhere else, put a point at 0.
- A point whose key repeats the previous point's is dropped, so it does not
  re-trigger the switch. At one tick the last point wins.
- `interp` must be `hold`; `linear` is an error. One keyswitch curve per
  part.
- **A switch key inside the part's played range** (from its lowest note to
  its highest) would sound as a pitch. That is an error, naming the key and
  the nearest played note:
  `part 'Trumpet' keyswitch: key 61 (c#4) is inside the part's played range, 55 (g3) to 82 (a#5), so it would sound as a pitch; the nearest played note is 60 (c4), first at offset 12`.
- **Under FluidSynth** a keyswitch curve is an error, because a GM SoundFont
  would play the key as a pitch. Use `--engine sfizz`. The synth voice
  ignores it, as it ignores the whole sidecar.
- On the output line, `events` counts each switch twice, for its note-on
  and its note-off.
- A sidecar with no `keyswitch` curve gives the same MIDI bytes as under
  0.5.0.

**Which key is which.** `chantey info` prints each mapped part's keyswitch
map, so you do not have to read the SFZ by hand. It takes the part's
`CHANTEY_SFZ_MAP` entry, or else `--sfz`:

```
$ chantey info saints.musicxml --sfz /lib/vpo/trumpet-SEC-KS-C2.sfz
part 'Trumpet': 32 measures; 160 quarters
  sfz: /lib/vpo/trumpet-SEC-KS-C2.sfz
  keyswitches: sw_lokey 36 (c2) to sw_hikey 40 (e2); sw_default 36 (c2)
    sw_last 36 (c2): Sustain
    sw_last 38 (d2): Normal
    sw_last 39 (d#2): Accent
    sw_last 40 (e2): Staccato
```

Note names are SFZ spelling, with middle C `c4` = 60. The reader follows
`<global>`, `<master>`, `<group>` and `<region>` inheritance, `#define` and
`#include`. Each `sw_last` (or `sw_lolast`–`sw_hilast` range) is listed once
with the first `sw_label` given for it. If the file sets `note_offset` or
`octave_offset`, a note says the keys are shown as written. An SFZ with no
keyswitch opcodes prints `keyswitches: none`. An SFZ that cannot be read is
a warning, and `info` still prints the score's summary.

The suite renders four notes through the real `sfizz_render` with a
three-articulation SFZ: Sustain, Staccato, a quieter Normal (−20 dB), then
Sustain. It checks each note's level early and late in the note. The
staccato starts at full level and is silent by mid-note, the Normal note is
a tenth of the Sustain level, and without the sidecar all four notes
sustain. Two renders are byte-identical.

### Timing (`time` curves)

Every note chantey renders starts exactly on its written tick. Real players
do not: a section leans behind the beat on a dirge, or pushes ahead. A
`time` curve (v0.7) moves each of a part's notes by a few milliseconds:

```json
{"parts": {"Trumpet": {"time_max": 60, "curves": [
    {"time": true, "interp": "linear", "points": [[0, 12], [64, 12], [68, -5]]}]}}}
```

- Values are milliseconds; positive is later. Each must be within
  ±`time_max`, which defaults to 60 and is set per part, beside
  `bend_range` (greater than 0, at most 1000). One time curve per part.
- The curve is read at **each note event's own written offset**: `hold`
  takes the last point at or before it, and `linear` the exact line between
  the points around it (no steps). Before the first point the first value
  holds; after the last, the last.
- Every note-on and note-off of the part moves by
  `round(ms × ticks-per-quarter × bpm / 60000)` ticks, at the tempo at its
  written offset. A note's on and off are read separately, so a note whose
  start lags and whose end does not comes out shorter. That is the point.
- **Nothing else moves.** Controllers, keyswitches, program changes and
  tempi keep their ticks. A controller value set at a note's written tick
  still reaches a note that lags; a note that leads its tick sounds before
  the value.
- **Caveat with keyswitches.** A note written at a keyswitch's offset and
  moved *earlier* sounds before the switch, in the previous articulation.
  Put the switch point earlier than the note by more than its lead, or keep
  that note's shift at or above 0.
- **Clamps.** Per channel and key, in written order: a note moved before
  the start is clamped to tick 0; a note-off moved past the next note-on of
  its key is pulled back to that note-on, so the next note is never cut
  off; an event moved before the previous event of its key is raised to it.
  An onset is never moved by a clamp unless two onsets of one key would
  swap.
- **Under FluidSynth** a part with a time curve that shares a MIDI channel
  with another part is an error, as for controllers: a moved note-off could
  end the other part's note of that key.
- **Output line.** `events` is unchanged, since nothing is added. When any
  part has a time curve the bracket ends `; shifted: <note events moved off
  their written tick>; clamped: <note events a clamp moved>`:

  ```
  [expression: saints.expr.json; parts: 3; events: 1412; shifted: 2210; clamped: 2]
  ```

- No randomness: the sidecar is explicit, so renders stay deterministic. A
  sidecar with no `time` curve gives the same MIDI bytes as under 0.6.0.

The suite renders four spaced notes through the real `sfizz_render` and
finds each onset in the audio. A `hold` curve at +40 ms moves all four by
40 ms, a `linear` curve from 0 to +40 ms across them gives 0, 13.4, 26.7
and 40 ms, and two renders are byte-identical.

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
  control ignores (a gitignored `scratch/`, say), so licensed material never
  enters git history.
- chantey never publishes, uploads, or hosts anything; the compare page is a
  local file. Sharing decisions are yours alone.

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

No `INFOPATH` needed on a current system: like man-db, modern texinfo derives its
search path from `PATH` (`~/.local/bin` → `~/.local/share/info`; verified
with `INFOPATH` unset). On an older system where `info` comes up empty,
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
uv run pytest          # externals mocked; music21/verovio real on tiny fixtures;
                       # the sfizz tests render through the real sfizz_render
                       # (synthesized SFZ, no library) and skip if it is absent
uv run ruff check . && uv run ruff format --check .
```
