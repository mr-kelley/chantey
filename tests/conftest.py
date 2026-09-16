"""Fixture builders: tiny scores constructed with music21 in code — public
domain by construction, no score files checked in."""

from __future__ import annotations

import zipfile

import pytest
from music21 import bar, meter, note, stream, tempo
from music21 import metadata as m21metadata


def build_score(
    tempo_marks: tuple = ((0.0, 100.0),),
    with_repeat: bool = False,
    title: str = "Fixture",
    n_measures: int = 4,
) -> stream.Score:
    """Two parts, `n_measures` of 2/4, two quarter notes per measure.
    Optional repeat around measures 1-2; tempo marks in the top part."""
    score = stream.Score()
    md = m21metadata.Metadata()
    md.title = title
    md.composer = ""  # suppress music21's default "Music21" composer stamp
    score.insert(0, md)
    for pi in range(2):
        part = stream.Part()
        part.partName = f"P{pi + 1}"
        base = "C4" if pi == 0 else "C3"
        second = "D4" if pi == 0 else "D3"
        for mi in range(n_measures):
            meas = stream.Measure(number=mi + 1)
            if mi == 0:
                meas.insert(0, meter.TimeSignature("2/4"))
            if with_repeat:
                if mi == 0:
                    meas.leftBarline = bar.Repeat(direction="start")
                if mi == 1:
                    meas.rightBarline = bar.Repeat(direction="end")
            meas.append(note.Note(base, quarterLength=1.0))
            meas.append(note.Note(second, quarterLength=1.0))
            part.append(meas)
        score.insert(0, part)
    # Tempo marks go INSIDE measures (part-level marks don't survive
    # MusicXML export/reload). Each measure spans 2 quarterLengths.
    measures = list(score.parts[0].getElementsByClass(stream.Measure))
    for offset, bpm in tempo_marks:
        index = min(int(float(offset) // 2), len(measures) - 1)
        local = float(offset) - 2.0 * index
        measures[index].insert(local, tempo.MetronomeMark(number=float(bpm)))
    return score


def write_musicxml(score: stream.Score, path) -> None:
    score.write("musicxml", fp=str(path))


def make_mxl(score: stream.Score, path) -> None:
    """Write a compressed .mxl (a zip holding the MusicXML + container)."""
    inner = str(path) + ".inner.musicxml"
    score.write("musicxml", fp=inner)
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<container><rootfiles>"
        '<rootfile full-path="score.musicxml"/>'
        "</rootfiles></container>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.write(inner, "score.musicxml")


@pytest.fixture
def score_file(tmp_path):
    def _write(score: stream.Score | None = None, name: str = "fixture.musicxml"):
        p = tmp_path / name
        write_musicxml(score or build_score(), p)
        return p

    return _write
