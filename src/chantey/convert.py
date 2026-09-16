"""MusicXML → hornpipe score conversion — the pipeline's heart.

Two behaviors this module guarantees (each found the hard way during the
walking-skeleton phase):

1. **Repeats are expanded once, by the caller, for BOTH audio voices.**
   `expand_repeats()` lives here; the renderer calls it a single time and
   feeds the same expanded score to the MIDI path and to `convert()`, so the
   voices can never disagree about form again (the skeleton's ~180s vs
   ~142s defect).

2. **Time is normalized to seconds.** hornpipe has a single tempo; real
   scores change tempo mid-piece. The converter resolves every metronome
   mark itself (piecewise over quarter-note offsets) and emits a hornpipe
   score with `tempo = 60`, where one beat is one second — so `at`/`dur`
   values ARE seconds and hornpipe needs no tempo knowledge at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from music21 import stream, tempo

# (wave, gain) per part, cycled: keep the first parts distinct and audible.
TRACK_VOICES = [("triangle", 0.9), ("sine", 0.6), ("square", 0.5), ("saw", 0.45)]

ENVELOPE = "{ attack = 0.005, decay = 0.25, sustain = 0.25, release = 0.12 }"


@dataclass
class ConvertResult:
    toml_text: str
    n_events: int
    duration_seconds: float
    warnings: list[str] = field(default_factory=list)


def expand_repeats(score: stream.Score) -> tuple[stream.Score, str | None]:
    """Expand repeats/voltas into a linear score. Called ONCE by the
    renderer; both audio voices consume the result. Falls back to the
    unexpanded score (with a warning) on malformed repeat structure."""
    try:
        return score.expandRepeats(), None
    except Exception as e:
        return score, f"could not expand repeats ({e}); rendering without expansion"


def tempo_segments(score: stream.Score) -> tuple[list[tuple[float, float]], str | None]:
    """(offset_in_quarterLengths, quarter_BPM) change points, sorted from
    offset 0. Marks are collected score-wide because engravings usually put
    them in the top part only."""
    marks: dict[float, float] = {}
    flat = score.flatten()
    for mark in flat.getElementsByClass(tempo.MetronomeMark):
        bpm = mark.getQuarterBPM()
        if bpm:
            marks[float(mark.offset)] = float(bpm)
    if not marks:
        return [(0.0, 120.0)], "no tempo mark; defaulting to quarter=120"
    segments = sorted(marks.items())
    if segments[0][0] > 0:
        segments.insert(0, (0.0, segments[0][1]))
    return segments, None


def seconds_at(segments: list[tuple[float, float]], offset: float) -> float:
    """Absolute time in seconds of a quarterLength offset, integrating the
    piecewise-constant tempo map."""
    total = 0.0
    for i, (start, bpm) in enumerate(segments):
        end = segments[i + 1][0] if i + 1 < len(segments) else float("inf")
        if offset <= start:
            break
        span = min(offset, end) - start
        total += span * 60.0 / bpm
    return total


def convert(score: stream.Score, title: str) -> ConvertResult:
    """Convert an (already repeat-expanded) score to hornpipe TOML text."""
    segments, tempo_warning = tempo_segments(score)
    warnings = [tempo_warning] if tempo_warning else []

    out = [
        "[score]",
        f'title = "{_toml_escape(title)}"',
        "tempo = 60  # one beat = one second; all tempo marks resolved by chantey",
        "gain = 0.5",
        "",
    ]
    n_events = 0
    last_end = 0.0
    grace_skipped = 0
    for i, part in enumerate(score.parts):
        wave, gain = TRACK_VOICES[i % len(TRACK_VOICES)]
        events = []
        flat = part.stripTies().flatten()
        for el in flat.notes:
            ql = float(el.duration.quarterLength)
            if ql <= 0:  # grace notes and other zero-length ornaments
                grace_skipped += len(el.pitches)
                continue
            off = float(el.offset)
            at = seconds_at(segments, off)
            dur = seconds_at(segments, off + ql) - at
            if dur <= 0:
                continue
            for p in el.pitches:
                events.append(f"  {{ at = {at:.4f}, dur = {dur:.4f}, pitch = {p.frequency:.3f} }},")
                last_end = max(last_end, at + dur)
        if not events:
            warnings.append(f"part {i + 1} has no notes; skipped")
            continue
        name = part.partName or f"part{i + 1}"
        out += [
            "[[track]]",
            f'name = "{_toml_escape(name)}{i + 1 if _is_dup_name(score, name) else ""}"',
            f'wave = "{wave}"',
            f"gain = {gain}",
            f"envelope = {ENVELOPE}",
            "notes = [",
            *events,
            "]",
            "",
        ]
        n_events += len(events)
    if grace_skipped:
        warnings.append(f"{grace_skipped} grace/ornament notes skipped in the synth voice")
    return ConvertResult(
        toml_text="\n".join(out),
        n_events=n_events,
        duration_seconds=last_end,
        warnings=warnings,
    )


def _is_dup_name(score: stream.Score, name: str) -> bool:
    return sum(1 for p in score.parts if (p.partName or "") == name) > 1


def _toml_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
