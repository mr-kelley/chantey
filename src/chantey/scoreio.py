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

from .errors import ScoreLoadError


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
    parts: list[tuple[str, int]]  # (name, measure count)
    keys: list[tuple[int, int]]  # (measure, sharps)
    times: list[tuple[int, str]]  # (measure, ratio)
    tempos: list[tuple[int, str]]  # (measure, description)
    note_count: int
    repeat_barlines: int
    voltas: int
    warnings: list[str] = field(default_factory=list)


def score_info(score: stream.Score) -> Info:
    md = score.metadata
    parts = [
        (p.partName or f"part{i + 1}", len(p.getElementsByClass(stream.Measure)))
        for i, p in enumerate(score.parts)
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
        referent = el.referent.type if el.referent is not None else "quarter"
        entry = (el.measureNumber or 0, f"{referent}={el.number}")
        if entry not in tempos:
            tempos.append(entry)
    info = Info(
        title=md.title if md else None,
        composer=(md.composer or None) if md else None,
        parts=parts,
        keys=sorted(keys),
        times=sorted(times),
        tempos=sorted(tempos),
        note_count=len(list(score.recurse().notes)),
        repeat_barlines=len(list(score.recurse().getElementsByClass(bar.Repeat))),
        voltas=len(list(score.recurse().getElementsByClass(spanner.RepeatBracket))),
    )
    if not info.parts:
        info.warnings.append("score has no parts")
    if info.note_count == 0:
        info.warnings.append("score has no notes")
    return info


def format_info(info: Info) -> str:
    lines = []
    if info.title:
        lines.append(f"title: {info.title}" + (f" — {info.composer}" if info.composer else ""))
    for name, n in info.parts:
        lines.append(f"part {name!r}: {n} measures")
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
