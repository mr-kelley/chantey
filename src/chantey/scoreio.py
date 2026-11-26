"""Score loading, structural summary (`info`), and canonical-form writing.

The canonical form is uncompressed `.musicxml` (spec: the PDF is a
rendering, not the source; one MusicXML file per piece is the source of
truth). Metadata is preserved — and backfilled from the filename when a
score arrives with none, so titles survive into every render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from music21 import bar, converter, key, meter, spanner, stream, tempo

from .convert import expand_repeats
from .errors import ScoreLoadError
from .expression import tail_quarters
from .sfzengine import EOT_TAIL_S


def load_score(path: Path | str) -> stream.Score:
    path = Path(path)
    if not path.exists():
        raise ScoreLoadError(f"no such file: {path}")
    try:
        parsed = converter.parse(str(path))
    except Exception as e:  # music21 raises a zoo of exception types
        raise ScoreLoadError(f"cannot parse {path.name}: {e}") from e
    if not isinstance(parsed, stream.Score):
        raise ScoreLoadError(f"{path.name} did not parse as a score")
    return parsed


@dataclass
class Info:
    title: str | None
    composer: str | None
    # (name, measure count, quarters as rendered, quarters as written); the
    # rendered timeline (repeats expanded) is the expression sidecar's
    parts: list[tuple[str, int, float, float]]
    tail_quarters: float  # the render tail, in quarters at the final tempo
    keys: list[tuple[int, int]]  # (measure, sharps)
    times: list[tuple[int, str]]  # (measure, ratio)
    tempos: list[tuple[int, str]]  # (measure, description)
    note_count: int
    repeat_barlines: int
    voltas: int
    warnings: list[str] = field(default_factory=list)
    # per part, lines printed indented under it (v0.6: its SFZ's keyswitches)
    part_notes: list[list[str]] = field(default_factory=list)


def score_info(score: stream.Score) -> Info:
    md = score.metadata
    expanded, repeat_warning = expand_repeats(score)
    parts = [
        (
            p.partName or f"part{i + 1}",
            len(p.getElementsByClass(stream.Measure)),
            float(rendered.highestTime),
            float(p.highestTime),
        )
        for i, (p, rendered) in enumerate(zip(score.parts, expanded.parts, strict=True))
    ]
    keys = []
    for el in score.recurse().getElementsByClass(key.KeySignature):
        entry = (el.measureNumber or 0, el.sharps)
        if entry not in keys:
            keys.append(entry)
    times = []
    for el in score.recurse().getElementsByClass(meter.TimeSignature):
        entry = (el.measureNumber or 0, el.ratioString)
        if entry not in times:
            times.append(entry)
    tempos = []
    for el in score.recurse().getElementsByClass(tempo.MetronomeMark):
        # fullName keeps dots ("dotted quarter"); .type alone drops them
        referent = el.referent.fullName.lower() if el.referent is not None else "quarter"
        entry = (el.measureNumber or 0, f"{referent}={el.number}")
        if entry not in tempos:
            tempos.append(entry)
    info = Info(
        title=md.title if md else None,
        composer=(md.composer or None) if md else None,
        parts=parts,
        tail_quarters=tail_quarters(expanded),
        keys=sorted(keys),
        times=sorted(times),
        tempos=sorted(tempos),
        note_count=len(list(score.recurse().notes)),
        repeat_barlines=len(list(score.recurse().getElementsByClass(bar.Repeat))),
        voltas=len(list(score.recurse().getElementsByClass(spanner.RepeatBracket))),
    )
    if repeat_warning:
        info.warnings.append(repeat_warning)
    if not info.parts:
        info.warnings.append("score has no parts")
    if info.note_count == 0:
        info.warnings.append("score has no notes")
    return info


def format_info(info: Info) -> str:
    lines = []
    if info.title:
        lines.append(f"title: {info.title}" + (f" — {info.composer}" if info.composer else ""))
    for i, (name, n, rendered, written) in enumerate(info.parts):
        line = f"part {name!r}: {n} measures; {rendered:g} quarters"
        if rendered != written:
            line += f" as rendered (repeats expanded; {written:g} as written)"
        lines.append(line)
        if i < len(info.part_notes):
            lines.extend(f"  {note}" for note in info.part_notes[i])
    if info.parts:
        lines.append(
            f"render tail: {EOT_TAIL_S:g} s = {info.tail_quarters:g} quarters at the final "
            "tempo (expression offsets may run to a part's quarters plus this)"
        )
    if info.keys:
        lines.append("keys: " + ", ".join(f"{s:+d} sharps @ m{m}" for m, s in info.keys))
    if info.times:
        lines.append("times: " + ", ".join(f"{r} @ m{m}" for m, r in info.times))
    lines.append(
        "tempos: " + (", ".join(f"{t} @ m{m}" for m, t in info.tempos) or "none (default 120)")
    )
    lines.append(
        f"notes/chords: {info.note_count}; repeat barlines: {info.repeat_barlines}; "
        f"voltas: {info.voltas}"
    )
    for w in info.warnings:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)


def ensure_title(score: stream.Score, fallback: str) -> None:
    from music21 import metadata as m21metadata

    if score.metadata is None:
        score.insert(0, m21metadata.Metadata())
    if not score.metadata.title:
        score.metadata.title = fallback


def write_canonical(score: stream.Score, out: Path) -> None:
    # music21's exporter injects composer "Music21" when composer is None;
    # an empty string suppresses that, keeping the canonical file's
    # metadata honest (verified against music21 10.5.0).
    if score.metadata is not None and score.metadata.composer is None:
        score.metadata.composer = ""
    score.write("musicxml", fp=str(out))
