import copy

from music21 import pitch

from chantey.diffing import diff_scores

from .conftest import build_score


def _revised(mutate):
    a = build_score()
    b = copy.deepcopy(a)
    mutate(b)
    return a, b


def test_identical_scores():
    a, b = _revised(lambda s: None)
    report = diff_scores(a, b)
    assert report.identical
    assert report.changed == []


def test_pitch_change_flags_measure():
    def mutate(s):
        s.parts[0].measure(3).notes[0].pitch = pitch.Pitch("G4")

    report = diff_scores(*_revised(mutate))
    assert report.changed == [3]
    assert list(report.per_part) == ["P1 (1)"]


def test_duration_change_flags_measure():
    def mutate(s):
        m = s.parts[1].measure(2)
        m.notes[0].duration.quarterLength = 0.5

    report = diff_scores(*_revised(mutate))
    assert report.changed == [2]


def test_added_note_flags_measure():
    def mutate(s):
        from music21 import note

        s.parts[0].measure(4).insert(0.5, note.Note("A4", quarterLength=0.25))

    report = diff_scores(*_revised(mutate))
    assert report.changed == [4]


def test_removed_note_flags_measure():
    def mutate(s):
        m = s.parts[0].measure(1)
        m.remove(m.notes[0])

    report = diff_scores(*_revised(mutate))
    assert report.changed == [1]


def test_changes_in_both_parts_union():
    def mutate(s):
        s.parts[0].measure(2).notes[0].pitch = pitch.Pitch("B4")
        s.parts[1].measure(4).notes[1].pitch = pitch.Pitch("B2")

    report = diff_scores(*_revised(mutate))
    assert report.changed == [2, 4]
    assert len(report.per_part) == 2


def test_part_count_mismatch_flagged():
    a = build_score()
    b = copy.deepcopy(a)
    b.remove(list(b.parts)[1])
    report = diff_scores(a, b)
    assert report.part_mismatch
    assert not report.identical


def _two_voice_score():
    """build_score() with part 1's measure 2 split into two voices."""
    from music21 import note, stream

    score = build_score()
    meas = score.parts[0].measure(2)
    for n in list(meas.notes):
        meas.remove(n)
    upper, lower = stream.Voice(id="1"), stream.Voice(id="2")
    upper.append([note.Note("E5", quarterLength=1.0), note.Note("F5", quarterLength=1.0)])
    lower.append([note.Note("C4", quarterLength=1.0), note.Note("D4", quarterLength=1.0)])
    meas.insert(0, upper)
    meas.insert(0, lower)
    return score


def test_identical_multi_voice_scores():
    a = _two_voice_score()
    report = diff_scores(a, copy.deepcopy(a))
    assert report.identical


def test_pitch_change_inside_voice_flags_measure():
    a = _two_voice_score()
    b = copy.deepcopy(a)
    b.parts[0].measure(2).voices[1].notes[1].pitch = pitch.Pitch("G4")
    report = diff_scores(a, b)
    assert report.changed == [2]
    assert list(report.per_part) == ["P1 (1)"]
