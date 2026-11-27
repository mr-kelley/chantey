"""The `time` curve kind (v0.7): per-part timing offsets in milliseconds."""

import json
import math
import shutil
import wave
from array import array
from pathlib import Path

import pytest
from music21 import instrument, meter, midi, note, stream, tempo
from music21 import metadata as m21metadata

from chantey import expression
from chantey.cli import main
from chantey.convert import expand_repeats
from chantey.deps import Deps
from chantey.errors import ChanteyError

from .conftest import build_score
from .test_expression import (
    SFIZZ,
    _read_midi,
    _timed,
    curve,
    fake_engines,
    plan_for,
    write_sidecar,
)

CC = midi.ChannelVoiceMessages.CONTROLLER_CHANGE
NOTE_ON = midi.ChannelVoiceMessages.NOTE_ON
NOTE_OFF = midi.ChannelVoiceMessages.NOTE_OFF


@pytest.fixture
def sfz(tmp_path):
    path = tmp_path / "a.sfz"
    path.write_text("<region> sample=x.wav\n")
    return str(path)


def tm(points, interp="hold"):
    return {"time": True, "interp": interp, "points": points}


def _notes(track):
    """(tick, "on"|"off", key) for every note event, in track order."""
    return [
        (t, "on" if e.isNoteOn() else "off", e.pitch)
        for t, e in _timed(track)
        if e.isNoteOn() or e.isNoteOff()
    ]


def _merge_parts(tmp_path, score, parts, name="work"):
    """write_part_midis with the merge, as the sfizz path runs it; returns
    (the part MIDIs, the plain part MIDIs, Merged)."""
    from chantey import sfzengine

    expanded, _ = expand_repeats(score)
    plain_dir, work = tmp_path / f"{name}-plain", tmp_path / name
    plain_dir.mkdir()
    work.mkdir()
    plain = sfzengine.write_part_midis(expanded, plain_dir)
    plan = expression.resolve(expression.load(write_sidecar(tmp_path, parts)), expanded)
    done = []
    merged = sfzengine.write_part_midis(
        expanded,
        work,
        edit=lambda tracks, tpq: done.append(expression.merge(plan, tracks, tpq, own_files=True)),
    )
    (result,) = done
    return merged, plain, result


def _ms_ticks(ms, tpq, bpm):
    return round(ms * tpq * bpm / 60000)


# --- load --------------------------------------------------------------------


def test_load_reads_the_tickets_example(tmp_path):
    path = write_sidecar(tmp_path, {"T": {"curves": [tm([[0, 12], [64, 12], [68, -5]], "linear")]}})
    (c,) = expression.load(path).parts["T"].curves
    assert (c.kind, c.label, c.interp) == ("time", "time", "linear")
    assert c.points == ((0.0, 12.0), (64.0, 12.0), (68.0, -5.0))


def test_load_time_max_widens_the_range_per_part(tmp_path):
    path = write_sidecar(
        tmp_path,
        {"A": {"time_max": 120, "curves": [tm([[0, -120]])]}, "B": {"curves": [tm([[0, 60]])]}},
    )
    assert set(expression.load(path).parts) == {"A", "B"}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({**tm([[0, 1]]), "time": 1}, ".time: expected true, got 1"),
        (tm([[0, 61]]), "61 is outside -60 to 60 ms (time_max) for time"),
        (tm([[0, -60.5]]), "-60.5 is outside -60 to 60 ms (time_max) for time"),
        ({**tm([[0, 1]]), "cc": 1}, "name exactly one curve kind (cc, bend, keyswitch, time)"),
        ({**tm([[0, 1]]), "velocity": 90}, "'velocity' belongs to a keyswitch curve"),
    ],
)
def test_load_refuses_a_bad_time_curve_naming_where(tmp_path, bad, message):
    path = write_sidecar(tmp_path, {"Trumpet": {"curves": [curve([[0, 1]], cc=7), bad]}})
    with pytest.raises(ChanteyError) as exc:
        expression.load(path)
    assert "parts['Trumpet'].curves[1]" in str(exc.value)
    assert message in str(exc.value)


@pytest.mark.parametrize("time_max", [0, -5, 1001, "60", True])
def test_load_refuses_a_bad_time_max(tmp_path, time_max):
    path = write_sidecar(tmp_path, {"T": {"time_max": time_max, "curves": []}})
    with pytest.raises(
        ChanteyError, match=r"parts\['T'\]\.time_max: expected milliseconds > 0 and at most 1000"
    ):
        expression.load(path)


def test_load_refuses_two_time_curves_for_one_part(tmp_path):
    path = write_sidecar(tmp_path, {"T": {"curves": [tm([[0, 1]]), tm([[0, 2]])]}})
    with pytest.raises(ChanteyError, match=r"a second time curve \(the first is curves\[0\]"):
        expression.load(path)


# --- the value at an offset --------------------------------------------------


def test_value_at_hold_and_linear():
    points = ((2.0, 10.0), (4.0, 30.0), (4.0, -20.0), (6.0, 0.0))
    hold = expression.Curve("time", None, "hold", points)
    linear = expression.Curve("time", None, "linear", points)
    at = (0, 2, 3, 3.999, 4, 5, 6, 9)
    assert [expression.value_at(hold, t) for t in at] == [10, 10, 10, 10, -20, -20, 0, 0]
    assert [round(expression.value_at(linear, t), 3) for t in at] == [
        10, 10, 20, 29.99, -20, -10, 0, 0
    ]  # fmt: skip


def test_resolve_samples_nothing_for_a_time_curve(tmp_path):
    plan = plan_for(tmp_path, build_score(), {"P1": {"curves": [tm([[0, 40]])]}})
    ((c, samples),) = plan.parts[0].samples
    assert (c.kind, samples) == ("time", ())
    assert plan.n_parts == 1 and plan.has_time
    assert plan.segments == ((0.0, 100.0),)


# --- merge -------------------------------------------------------------------


def test_merge_hold_moves_every_note_event_and_nothing_else(tmp_path):
    merged, plain, done = _merge_parts(tmp_path, build_score(), {"P1": {"curves": [tm([[0, 40]])]}})
    mine, theirs = _read_midi(merged.paths[0]), _read_midi(plain.paths[0])
    tpq = mine.ticksPerQuarterNote
    shift = _ms_ticks(40, tpq, 100)
    before, after = _notes(theirs.tracks[1]), _notes(mine.tracks[1])
    assert after == [(t + shift, kind, key) for t, kind, key in before]
    others = [
        (t, e.getBytes()) for t, e in _timed(mine.tracks[1]) if e.type not in (NOTE_ON, NOTE_OFF)
    ]
    assert others == [
        (t, e.getBytes()) for t, e in _timed(theirs.tracks[1]) if e.type not in (NOTE_ON, NOTE_OFF)
    ]
    assert done == expression.Merged(events=0, shifted=len(before), clamped=0)
    assert merged.paths[1].read_bytes() == plain.paths[1].read_bytes()
    assert merged.seconds == plain.seconds


def test_merge_converts_ms_at_the_tempo_at_each_written_offset(tmp_path):
    score = build_score(tempo_marks=((0.0, 100.0), (4.0, 50.0)))
    merged, plain, _ = _merge_parts(tmp_path, score, {"P1": {"curves": [tm([[0, 30]])]}})
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    before, after = (
        _notes(_read_midi(plain.paths[0]).tracks[1]),
        _notes(_read_midi(merged.paths[0]).tracks[1]),
    )
    shifts = {(b[0], b[1]): a[0] - b[0] for a, b in zip(after, before, strict=True)}
    assert shifts[(0, "on")] == _ms_ticks(30, tpq, 100)
    assert shifts[(4 * tpq, "on")] == _ms_ticks(30, tpq, 50)
    assert shifts[(4 * tpq, "off")] == _ms_ticks(30, tpq, 50)  # the off is read where it is


def test_merge_linear_onsets_across_a_bar(tmp_path):
    """Acceptance check (b): 0 -> +40 ms across four quarters' onsets."""
    merged, plain, done = _merge_parts(
        tmp_path, build_score(), {"P1": {"curves": [tm([[0, 0], [3, 40]], "linear")]}}
    )
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    ons = [t for t, kind, _ in _notes(_read_midi(merged.paths[0]).tracks[1]) if kind == "on"]
    expected_ms = [0, 40 / 3, 80 / 3, 40]
    assert ons[:4] == [q * tpq + _ms_ticks(ms, tpq, 100) for q, ms in enumerate(expected_ms)]
    assert [round((ons[q] - q * tpq) * 60000 / tpq / 100) for q in range(4)] == [0, 13, 27, 40]
    # a note's on and off are read separately: the first note lengthens by
    # the curve's rise over it
    first_off = next(
        t for t, kind, _ in _notes(_read_midi(merged.paths[0]).tracks[1]) if kind == "off"
    )
    assert first_off - ons[0] == tpq + _ms_ticks(40 / 3, tpq, 100)
    assert done.shifted == 2 * 8 - 1  # every note event but the on at 0


def test_merge_clamps_at_tick_0_and_counts_it(tmp_path):
    merged, plain, done = _merge_parts(
        tmp_path, build_score(), {"P1": {"curves": [tm([[0, -40]])]}}
    )
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    shift = _ms_ticks(-40, tpq, 100)
    after = _notes(_read_midi(merged.paths[0]).tracks[1])
    assert after[0] == (0, "on", 60)
    assert after[1] == (tpq + shift, "off", 60)
    assert done == expression.Merged(events=0, shifted=15, clamped=1)


def repeated_note_score():
    """C4 for 7/8 of a quarter, a 32nd rest, C4 again: at 100 bpm the gap
    is 75 ms, less than a 60 ms lag plus a 60 ms lead."""
    score = stream.Score()
    md = m21metadata.Metadata()
    md.title, md.composer = "Repeat", ""
    score.insert(0, md)
    part = stream.Part()
    part.partName = "P1"
    m = stream.Measure(number=1)
    m.insert(0, meter.TimeSignature("2/4"))
    m.insert(0, tempo.MetronomeMark(number=100))
    m.append(note.Note("C4", quarterLength=0.875))
    m.append(note.Rest(quarterLength=0.125))
    m.append(note.Note("C4", quarterLength=1.0))
    part.append(m)
    score.insert(0, part)
    return score


def test_merge_pulls_an_off_back_to_the_next_on_of_its_key(tmp_path):
    parts = {"P1": {"curves": [tm([[0, 60], [1, -60]])]}}
    merged, plain, done = _merge_parts(tmp_path, repeated_note_score(), parts)
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    late, early = _ms_ticks(60, tpq, 100), _ms_ticks(-60, tpq, 100)
    after = _notes(_read_midi(merged.paths[0]).tracks[1])
    assert after == [
        (late, "on", 60),
        (tpq + early, "off", 60),  # would be 7/8 q + 60 ms, past the next on
        (tpq + early, "on", 60),  # the onset itself is not moved by the clamp
        (2 * tpq + early, "off", 60),
    ]
    assert done == expression.Merged(events=0, shifted=4, clamped=1)


def test_controllers_and_keyswitches_keep_their_ticks_while_notes_move(tmp_path):
    parts = {
        "P1": {
            "curves": [
                curve([[0, 90], [2, 100]], cc=11),
                {"keyswitch": True, "interp": "hold", "points": [[2, 36]]},
                tm([[0, 40]]),
            ]
        }
    }
    merged, _, done = _merge_parts(tmp_path, build_score(), parts)
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    shift = _ms_ticks(40, tpq, 100)
    timed = _timed(_read_midi(merged.paths[0]).tracks[1])
    assert [t for t, e in timed if e.type == CC] == [0, 2 * tpq]
    switch = [
        (t, e.isNoteOn()) for t, e in timed if e.type in (NOTE_ON, NOTE_OFF) and e.pitch == 36
    ]
    assert switch == [(2 * tpq, True), (2 * tpq + 1, False)]
    played = [(t, k) for t, k, key in _notes(_read_midi(merged.paths[0]).tracks[1]) if key != 36]
    assert (2 * tpq + shift, "on") in played
    assert done.events == 2 + 2 and done.shifted == 16


def test_merge_shared_channel_refuses_a_time_curve_in_the_single_file(tmp_path):
    plan = plan_for(tmp_path, build_score(), {"P1": {"curves": [tm([[0, 10]])]}})
    path = tmp_path / "score.mid"
    expand_repeats(build_score())[0].write("midi", fp=str(path))
    mf = _read_midi(path)
    with pytest.raises(ChanteyError) as exc:
        expression.merge(plan, mf.tracks[1:], mf.ticksPerQuarterNote, own_files=False)
    assert (
        "part 'P1' shares MIDI channel 1 with 'P2' in the export, so its time curve's moved "
        "note-offs could end that part's notes under FluidSynth" in str(exc.value)
    )


def duo_score():
    score = build_score()
    score.parts[0].insert(0, instrument.Trumpet())
    score.parts[1].insert(0, instrument.Tuba())
    return score


def test_merge_single_file_moves_only_its_part(tmp_path):
    plan = plan_for(tmp_path, duo_score(), {"P2": {"curves": [tm([[0, -20]])]}})
    path = tmp_path / "score.mid"
    expand_repeats(duo_score())[0].write("midi", fp=str(path))
    plain = _read_midi(path)
    mf = _read_midi(path)
    done = expression.merge(plan, mf.tracks[1:], mf.ticksPerQuarterNote, own_files=False)
    assert done == expression.Merged(events=0, shifted=15, clamped=1)
    assert _notes(mf.tracks[1]) == _notes(plain.tracks[1])
    assert _notes(mf.tracks[2]) != _notes(plain.tracks[2])


def test_label_adds_the_counts_only_with_a_time_curve(tmp_path):
    with_time = plan_for(tmp_path, build_score(), {"P1": {"curves": [tm([[0, 1]])]}})
    without = plan_for(tmp_path, build_score(), {"P1": {"curves": [curve([[0, 1]], cc=1)]}})
    m = expression.Merged(3, 5, 1)
    assert expression.label(with_time, m).endswith("; parts: 1; events: 3; shifted: 5; clamped: 1")
    assert expression.label(without, m).endswith("; parts: 1; events: 3")


# --- the CLI -----------------------------------------------------------------


def test_cli_sfizz_output_line_counts_shifted_and_clamped(score_file, tmp_path, sfz, capsys):
    deps, _ = fake_engines(tmp_path)
    side = write_sidecar(
        tmp_path,
        {"P1": {"curves": [tm([[0, -40]])]}, "P2": {"curves": [tm([[0, 25]])]}},
    )
    argv = ["render", str(score_file()), *SFIZZ, "--sfz", sfz, "--expression", str(side)]
    assert main(argv, deps=deps) == 0
    (line,) = [x for x in capsys.readouterr().out.splitlines() if x.startswith("Wrote")]
    assert line.endswith(f"]  [expression: {side}; parts: 2; events: 0; shifted: 31; clamped: 1]")


def test_cli_fluidsynth_output_line_counts_shifted(tmp_path, capsys):
    path = tmp_path / "duo.musicxml"
    duo_score().write("musicxml", fp=str(path))
    deps, midis = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, {"P1": {"curves": [tm([[0, 30]])]}})
    argv = ["render", str(path), "--voice", "piano", "--no-pdf", "--expression", str(side)]
    assert main(argv, deps=deps) == 0
    assert "[fx: dry]  [expression: " in (out := capsys.readouterr().out)
    assert "; parts: 1; events: 0; shifted: 16; clamped: 0]" in out
    (data,) = midis
    sent = tmp_path / "sent.mid"
    sent.write_bytes(data)
    tpq = _read_midi(sent).ticksPerQuarterNote
    # the B-flat trumpet's written C4 sounds B-flat 3 (MIDI 58)
    assert _notes(_read_midi(sent).tracks[1])[0] == (_ms_ticks(30, tpq, 100), "on", 58)


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ({"P1": {"curves": [tm([[0, 61]])]}}, "61 is outside -60 to 60 ms (time_max) for time"),
        ({"P1": {"time_max": 2000, "curves": []}}, "at most 1000, got 2000"),
        ({"P1": {"curves": [tm([[0, 1], [99, 1]])]}}, "part 'P1' time: offset 99 is past"),
    ],
)
def test_cli_time_errors_exit_1_and_write_nothing(
    score_file, tmp_path, sfz, capsys, parts, message
):
    deps, midis = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, parts)
    out = tmp_path / "out"
    argv = ["render", str(score_file()), *SFIZZ, "--sfz", sfz, "-o", str(out),
            "--expression", str(side)]  # fmt: skip
    assert main(argv, deps=deps) == 1
    assert message in capsys.readouterr().err
    assert midis == []
    assert not out.exists() or list(out.iterdir()) == []


def test_cli_fluidsynth_shared_channel_time_curve_exits_1(score_file, tmp_path, capsys):
    deps, _ = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, {"P1": {"curves": [tm([[0, 10]])]}})
    out = tmp_path / "out"
    argv = ["render", str(score_file()), "--voice", "piano", "--no-pdf", "-o", str(out),
            "--expression", str(side)]  # fmt: skip
    assert main(argv, deps=deps) == 1
    assert "its time curve's moved note-offs could end that part's notes" in (
        capsys.readouterr().err
    )
    assert not (out / "fixture.piano.wav").exists()


# --- the real sfizz_render ---------------------------------------------------

needs_sfizz = pytest.mark.skipif(
    shutil.which("sfizz_render") is None, reason="sfizz_render not on PATH"
)

RATE = 44100


def _write_tone(path: Path, seconds: float = 1.0) -> None:
    n = int(seconds * RATE)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(
            array(
                "h", (round(12000 * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(n))
            ).tobytes()
        )


def spaced_score():
    """Four A3 eighths a quarter apart at 120 bpm: an onset every 0.5 s."""
    score = stream.Score()
    md = m21metadata.Metadata()
    md.title, md.composer = "Spaced", ""
    score.insert(0, md)
    part = stream.Part()
    part.partName = "Trumpet"
    m = stream.Measure(number=1)
    m.insert(0, meter.TimeSignature("4/4"))
    m.insert(0, tempo.MetronomeMark(number=120))
    for _ in range(4):
        m.append(note.Note("A3", quarterLength=0.5))
        m.append(note.Rest(quarterLength=0.5))
    part.append(m)
    score.insert(0, part)
    return score


def _render_spaced(tmp_path, out, curve_=None):
    if not (tmp_path / "tone.wav").exists():
        _write_tone(tmp_path / "tone.wav")
        (tmp_path / "t.sfz").write_text(
            "<region> sample=tone.wav pitch_keycenter=57 amp_veltrack=0 ampeg_release=0.01\n"
        )
        spaced_score().write("musicxml", fp=str(tmp_path / "spaced.musicxml"))
    argv = ["render", str(tmp_path / "spaced.musicxml"), *SFIZZ, "--sfz", str(tmp_path / "t.sfz"),
            "-o", str(out)]  # fmt: skip
    if curve_ is not None:
        path = tmp_path / f"{out.name}.json"
        path.write_text(json.dumps({"parts": {"Trumpet": {"curves": [curve_]}}}))
        argv += ["--expression", str(path)]
    assert main(argv, deps=Deps(environ={})) == 0
    wav = out / "spaced.piano.wav"
    with wave.open(str(wav), "rb") as w:
        channels = w.getnchannels()
        mono = array("h", w.readframes(w.getnframes()))[::channels]
    onsets = []
    for n in range(4):  # the first loud sample in a window around each written onset
        start = int((0.5 * n - 0.1) * RATE) if n else 0
        window = mono[start : int((0.5 * n + 0.2) * RATE)]
        onsets.append((start + next(i for i, x in enumerate(window) if abs(x) > 500)) / RATE)
    return onsets, wav.read_bytes()


@needs_sfizz
def test_sfizz_hold_and_linear_shifts_are_heard_at_the_onsets(tmp_path):
    """Acceptance checks (a) and (b), in miniature: onsets measured in the
    rendered audio against the same score rendered without the sidecar."""
    plain, _ = _render_spaced(tmp_path, tmp_path / "plain")
    held, first = _render_spaced(tmp_path, tmp_path / "held", tm([[0, 40]]))
    assert [round((h - p) * 1000, 1) for h, p in zip(held, plain, strict=True)] == pytest.approx(
        [40] * 4, abs=0.5
    )
    ramp, _ = _render_spaced(tmp_path, tmp_path / "ramp", tm([[0, 0], [3, 40]], "linear"))
    assert [(r - p) * 1000 for r, p in zip(ramp, plain, strict=True)] == pytest.approx(
        [0, 40 / 3, 80 / 3, 40], abs=0.5
    )
    _, again = _render_spaced(tmp_path, tmp_path / "again", tm([[0, 40]]))
    assert again == first
