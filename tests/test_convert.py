import pytest
from music21 import note as m21note

from chantey.convert import convert, expand_repeats, seconds_at, tempo_segments

from .conftest import build_score


def _events(toml_text):
    return [line for line in toml_text.splitlines() if line.strip().startswith("{ at")]


def test_single_tempo_seconds():
    # quarter=100 -> each quarterLength = 0.6 s
    score = build_score(tempo_marks=((0.0, 100.0),))
    result = convert(score, "t")
    assert "tempo = 60" in result.toml_text
    first = _events(result.toml_text)[0]
    assert "at = 0.0000" in first and "dur = 0.6000" in first
    # 4 measures x 2 notes x 2 parts
    assert result.n_events == 16
    assert result.duration_seconds == pytest.approx(8 * 0.6)


def test_mid_score_tempo_change_is_resolved():
    # m1-2 at quarter=60 (1 s/qL), m3-4 at quarter=120 (0.5 s/qL); change at offset 4
    score = build_score(tempo_marks=((0.0, 60.0), (4.0, 120.0)))
    segments, warning = tempo_segments(score)
    assert warning is None
    assert seconds_at(segments, 4.0) == pytest.approx(4.0)
    assert seconds_at(segments, 5.0) == pytest.approx(4.5)
    result = convert(score, "t")
    assert "at = 4.0000, dur = 0.5000" in result.toml_text
    assert result.duration_seconds == pytest.approx(4.0 + 4 * 0.5)


def test_no_tempo_defaults_120_with_warning():
    score = build_score(tempo_marks=())
    result = convert(score, "t")
    assert any("no tempo mark" in w for w in result.warnings)
    assert "dur = 0.5000" in result.toml_text  # 120 bpm -> 0.5 s per quarter


def test_ties_merge_into_one_event():
    score = build_score()
    part = score.parts[0]
    m1, m2 = part.getElementsByClass("Measure")[0:2]
    for n in list(m1.notes) + list(m2.notes):
        n.tie = None
    m1.notes[1].tie = __import__("music21").tie.Tie("start")
    m2.notes[0].tie = __import__("music21").tie.Tie("stop")
    # make the tied pair the same pitch so the tie is real
    m2.notes[0].pitch = m1.notes[1].pitch
    result = convert(score, "t")
    # part 0: 8 notes minus one merged = 7 events
    assert "dur = 1.2000" in result.toml_text  # two 0.6 s quarters merged


def test_chords_expand_to_simultaneous_events():
    from music21 import chord as m21chord

    score = build_score()
    m1 = score.parts[0].getElementsByClass("Measure")[0]
    m1.replace(m1.notes[0], m21chord.Chord(["C4", "E4", "G4"], quarterLength=1.0))
    result = convert(score, "t")
    assert result.n_events == 16 + 2  # chord adds two extra events


def test_grace_notes_skipped_with_warning():
    score = build_score()
    m1 = score.parts[0].getElementsByClass("Measure")[0]
    grace = m21note.Note("E4").getGrace()
    m1.insert(0.0, grace)
    result = convert(score, "t")
    assert any("grace" in w for w in result.warnings)
    assert result.n_events == 16  # grace contributed nothing


def test_empty_part_skipped_with_warning():
    score = build_score()
    for meas in score.parts[1].getElementsByClass("Measure"):
        for n in list(meas.notes):
            meas.remove(n)
    result = convert(score, "t")
    assert any("no notes" in w for w in result.warnings)
    assert result.n_events == 8


def test_duplicate_part_names_get_suffixes():
    score = build_score()
    for p in score.parts:
        p.partName = "Piano"
    result = convert(score, "t")
    assert 'name = "Piano1"' in result.toml_text
    assert 'name = "Piano2"' in result.toml_text


def test_expand_repeats_doubles_repeated_measures():
    score = build_score(with_repeat=True)
    expanded, warning = expand_repeats(score)
    assert warning is None
    plain = convert(score, "t")
    grown = convert(expanded, "t")
    # measures 1-2 (half the score) play twice: 16 -> 24 events
    assert plain.n_events == 16
    assert grown.n_events == 24


def test_title_escaped():
    result = convert(build_score(), 'say "hi" \\ there')
    assert 'title = "say \\"hi\\" \\\\ there"' in result.toml_text
