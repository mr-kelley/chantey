import json
import math
import shutil
import wave
from array import array
from pathlib import Path

import pytest
from music21 import instrument, meter, midi, note, stream, tempo
from music21 import metadata as m21metadata

from chantey import expression, sfzengine
from chantey.cli import main
from chantey.convert import expand_repeats
from chantey.deps import Deps
from chantey.errors import ChanteyError

from .conftest import build_score

CC = midi.ChannelVoiceMessages.CONTROLLER_CHANGE
BEND = midi.ChannelVoiceMessages.PITCH_BEND


def write_sidecar(tmp_path: Path, parts, name: str = "expr.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"parts": parts}))
    return path


def curve(points, *, cc=None, interp="hold", bend=None):
    c = {"interp": interp, "points": points}
    if cc is not None:
        c["cc"] = cc
    if bend is not None:
        c["bend"] = bend
    return c


def held_note_score(quarters: int = 8, bpm: float = 120.0, name: str = "Trumpet"):
    """One part, one held C4 over `quarters` quarters of 4/4."""
    score = stream.Score()
    md = m21metadata.Metadata()
    md.title, md.composer = "Held", ""
    score.insert(0, md)
    part = stream.Part()
    part.partName = name
    measure = stream.Measure(number=1)
    measure.insert(0, meter.TimeSignature(f"{quarters}/4"))
    measure.insert(0, tempo.MetronomeMark(number=bpm))
    measure.append(note.Note("C4", quarterLength=float(quarters)))
    part.append(measure)
    score.insert(0, part)
    return score


def plan_for(tmp_path, score, parts):
    expanded, _ = expand_repeats(score)
    return expression.resolve(expression.load(write_sidecar(tmp_path, parts)), expanded)


# --- load: the format on its own ---------------------------------------------


def test_load_reads_the_tickets_example(tmp_path):
    path = write_sidecar(
        tmp_path,
        {
            "Trumpet": {
                "bend_range": 1200,
                "curves": [
                    curve([[0, 90], [16, 90], [20, 127], [24, 80]], cc=11, interp="linear"),
                    curve([[0, 40], [64, 40], [66, 110]], cc=1, interp="linear"),
                    curve([[0, 0], [4, 127], [7.5, 0]], cc=64),
                    curve([[32, 0], [32.5, -1200]], bend=True, interp="linear"),
                ],
            }
        },
    )
    sidecar = expression.load(path)
    trumpet = sidecar.parts["Trumpet"]
    assert trumpet.bend_range == 1200
    assert [c.label for c in trumpet.curves] == ["CC11", "CC1", "CC64", "pitch bend"]
    assert trumpet.curves[2].points == ((0.0, 0.0), (4.0, 127.0), (7.5, 0.0))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("{", "not valid JSON"),
        ('{"parts": {"T": {"curves": [{"cc": 1, "interp": "hold", "points": [[0, NaN]]}]}}}',
         "NaN is not a number JSON allows"),
        ('{"parts": {}, "version": 1}', "top level: unknown key 'version'"),
        ("{}", "missing 'parts'"),
        ('{"parts": []}', "parts: expected part-names"),
        ('{"parts": {"T": {}}}', "missing 'curves'"),
        ('{"parts": {"T": {"curves": [], "gain": 1}}}', "unknown key 'gain'"),
        ('{"parts": {"T": {"curves": {}}}}', "expected a list of curves"),
        ('{"parts": {"T": {"curves": [], "bend_range": 0}}}', "expected cents > 0"),
    ],
)  # fmt: skip
def test_load_refuses_a_malformed_file(tmp_path, document, message):
    path = tmp_path / "bad.json"
    path.write_text(document)
    with pytest.raises(ChanteyError, match="expression sidecar") as exc:
        expression.load(path)
    assert message in str(exc.value)


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"interp": "hold", "points": [[0, 1]]}, "name exactly one curve kind"),
        ({"cc": 1, "bend": True, "interp": "hold", "points": [[0, 1]]}, "exactly one curve kind"),
        ({"note": 36, "interp": "hold", "points": [[0, 1]]}, "unknown key 'note'"),
        (curve([[0, 1]], cc=120), "controller 120 is outside 0-119"),
        (curve([[0, 1]], cc=True), "expected a controller number"),
        (curve([[0, 1]], bend=False), "expected true"),
        (curve([[0, 1]], cc=1, interp="cubic"), "expected one of linear, hold"),
        (curve([], cc=1), "non-empty list"),
        (curve([[0]], cc=1), "expected [offset, value]"),
        (curve([[0, "90"]], cc=1), "expected [offset, value]"),
        (curve([[0, True]], cc=1), "expected [offset, value]"),
        (curve([[0, 128]], cc=11), "128 is outside 0 to 127 for cc"),
        (curve([[0, -1]], cc=11), "-1 is outside 0 to 127 for cc"),
        (curve([[0, -201]], bend=True), "-201 is outside -200 to 200 cents for bend"),
        (curve([[-1, 0]], cc=11), "offset -1 is before the score's start"),
        (curve([[4, 0], [2, 1]], cc=11), "points[1]: offset 2 comes before the previous"),
    ],
)
def test_load_refuses_a_bad_curve_naming_where(tmp_path, bad, message):
    path = write_sidecar(tmp_path, {"Trumpet": {"curves": [curve([[0, 1]], cc=7), bad]}})
    with pytest.raises(ChanteyError) as exc:
        expression.load(path)
    assert "parts['Trumpet'].curves[1]" in str(exc.value)
    assert message in str(exc.value)


def test_load_allows_equal_offsets_for_a_jump(tmp_path):
    path = write_sidecar(tmp_path, {"T": {"curves": [curve([[0, 0], [4, 0], [4, 127]], cc=1)]}})
    assert len(expression.load(path).parts["T"].curves[0].points) == 3


@pytest.mark.parametrize(
    ("curves", "message"),
    [
        ([curve([[0, 1]], cc=11), curve([[0, 2]], cc=11)], "a second CC11 curve (the first is"),
        ([curve([[0, 1]], bend=True), curve([[0, 2]], bend=True)], "a second pitch bend curve"),
    ],
)
def test_load_refuses_two_curves_on_one_controller(tmp_path, curves, message):
    with pytest.raises(ChanteyError, match="one curve per controller per part") as exc:
        expression.load(write_sidecar(tmp_path, {"T": {"curves": curves}}))
    assert message in str(exc.value)


def test_load_bend_range_widens_the_cents_range(tmp_path):
    path = write_sidecar(
        tmp_path, {"T": {"bend_range": 1200, "curves": [curve([[0, -1200]], bend=True)]}}
    )
    assert expression.load(path).parts["T"].bend_range == 1200


def test_load_missing_file(tmp_path):
    with pytest.raises(ChanteyError, match="expression sidecar not found"):
        expression.load(tmp_path / "none.json")


# --- resolve: against the score ----------------------------------------------


def test_resolve_matches_part_names_exact_then_case_insensitive(tmp_path):
    plan = plan_for(tmp_path, build_score(), {"p2": {"curves": [curve([[0, 64]], cc=1)]}})
    assert plan.parts[0] is None
    assert plan.parts[1].label == "P2"
    assert plan.n_parts == 1


def test_resolve_unknown_part_name_is_an_error(tmp_path):
    with pytest.raises(
        ChanteyError, match=r"no part named 'Tuba' \(the score's parts: 'P1', 'P2'\)"
    ):
        plan_for(tmp_path, build_score(), {"Tuba": {"curves": []}})


def test_resolve_offsets_may_run_into_the_tail_but_not_past_it(tmp_path):
    # 8 quarters at 100 bpm; the 2 s tail is 3 1/3 quarters
    ok = {"P1": {"curves": [curve([[0, 0], [11.3, 1]], cc=1)]}}
    assert plan_for(tmp_path, build_score(), ok).n_parts == 1
    late = {"P1": {"curves": [curve([[0, 0], [11.4, 1]], cc=1)]}}
    with pytest.raises(ChanteyError) as exc:
        plan_for(tmp_path, build_score(), late)
    assert (
        "part 'P1' CC1: offset 11.4 is past the part's end (8 quarters) plus the 2 s tail"
        in str(exc.value)
    )


def test_resolve_uses_the_rendered_timeline(tmp_path):
    """Repeats expanded: the fixture's 8 written quarters render as 12."""
    score = build_score(with_repeat=True)
    parts = {"P1": {"curves": [curve([[0, 0], [15, 1]], cc=1)]}}
    assert plan_for(tmp_path, score, parts).n_parts == 1
    with pytest.raises(ChanteyError, match="past the part's end"):
        plan_for(tmp_path, build_score(), parts)


def test_hold_samples_each_point_and_holds_the_first_from_zero(tmp_path):
    plan = plan_for(tmp_path, build_score(), {"P1": {"curves": [curve([[2, 40], [4, 100]], cc=1)]}})
    ((_, samples),) = plan.parts[0].samples
    assert samples == ((0.0, 40), (2.0, 40), (4.0, 100))


def test_linear_steps_by_a_32nd_quarter_or_10_ms_whichever_is_coarser():
    c = expression.Curve("cc", 1, "linear", ((0.0, 0.0), (1.0, 32.0)))
    # at 100 bpm 10 ms is 1/60 quarter, finer than 1/32: steps of 1/32
    slow = expression._sample(c, [(0.0, 100.0)], 200)
    assert [t for t, _ in slow] == [i / 32 for i in range(33)]
    assert [v for _, v in slow] == list(range(33))
    # at 300 bpm 10 ms is 1/20 quarter: steps of 1/20
    fast = expression._sample(c, [(0.0, 300.0)], 200)
    assert [round(t, 9) for t, _ in fast] == [round(i / 20, 9) for i in range(21)]


def test_bend_encodes_cents_against_the_parts_range():
    c = expression.Curve(
        "bend", None, "hold", ((0.0, -200.0), (1.0, 0.0), (2.0, 100.0), (3.0, 200.0))
    )
    assert [v for _, v in expression._sample(c, [(0.0, 120.0)], 200)] == [0, 8192, 12288, 16383]
    assert [v for _, v in expression._sample(c, [(0.0, 120.0)], 400)] == [4096, 8192, 10240, 12288]


# --- merge: into the MIDI ----------------------------------------------------


def _read_midi(path):
    mf = midi.MidiFile()
    mf.open(str(path))
    mf.read()
    mf.close()
    return mf


def _timed(track):
    tick, out = 0, []
    for e in track.events:
        if isinstance(e, midi.DeltaTime):
            tick += e.time
        else:
            out.append((tick, e))
    return out


def _added(e) -> bool:
    return e.type == CC or (e.type == BEND and e.parameter1 + (e.parameter2 << 7) != 8192)


def _part_midis(tmp_path, score, parts=None, name="work"):
    """write_part_midis with (or without) the merge, as the sfizz path runs it."""
    workdir = tmp_path / name
    workdir.mkdir()
    expanded, _ = expand_repeats(score)
    counts = []
    edit = None
    if parts is not None:
        plan = expression.resolve(expression.load(write_sidecar(tmp_path, parts)), expanded)
        edit = lambda tracks, tpq: counts.append(  # noqa: E731
            expression.merge(plan, tracks, tpq, own_files=True)
        )
    result = sfzengine.write_part_midis(expanded, workdir, edit=edit)
    return result, sum(c.events for c in counts)


def test_merge_places_events_at_their_ticks_around_unchanged_notes(tmp_path):
    parts = {"P1": {"curves": [curve([[0, 90], [2, 100], [4, 60]], cc=11)]}}
    plain, _ = _part_midis(tmp_path, build_score(), name="plain")
    merged, count = _part_midis(tmp_path, build_score(), parts)
    assert count == 3
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    track = _read_midi(merged.paths[0]).tracks[1]
    ccs = [(t, e.channel, e.parameter1, e.parameter2) for t, e in _timed(track) if e.type == CC]
    assert ccs == [(0, 1, 11, 90), (2 * tpq, 1, 11, 100), (4 * tpq, 1, 11, 60)]
    # without the added events, the track is the plain export, event for event
    before = [(t, e.getBytes()) for t, e in _timed(_read_midi(plain.paths[0]).tracks[1])]
    after = [(t, e.getBytes()) for t, e in _timed(track) if not _added(e)]
    assert after == before
    # the conductor and the other part are untouched
    assert merged.paths[1].read_bytes() == plain.paths[1].read_bytes()


def test_merge_puts_a_value_before_the_note_on_at_its_tick(tmp_path):
    parts = {"P1": {"curves": [curve([[0, 90], [2, 100]], cc=11)]}}
    merged, _ = _part_midis(tmp_path, build_score(), parts)
    timed = _timed(_read_midi(merged.paths[0]).tracks[1])

    def kind(e):
        return "cc" if e.type == CC else "on" if e.isNoteOn() else "off" if e.isNoteOff() else "x"

    kinds = [(t, kind(e)) for t, e in timed]
    first_on = kinds.index((0, "on"))
    assert kinds.index((0, "cc")) < first_on
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    at_two = [k for t, k in kinds if t == 2 * tpq]
    assert at_two == ["off", "cc", "on"]  # after the note-off, before the note-on


def test_merge_bend_follows_the_exports_centre_reset(tmp_path):
    parts = {"P1": {"curves": [curve([[0, -100]], bend=True)]}}
    merged, count = _part_midis(tmp_path, build_score(), parts)
    assert count == 1
    timed = _timed(_read_midi(merged.paths[0]).tracks[1])
    bends = [(t, e.parameter1 + (e.parameter2 << 7)) for t, e in timed if e.type == BEND]
    assert bends == [(0, 8192), (0, 4096)]


def test_merge_drops_repeated_values_and_counts_what_it_adds(tmp_path):
    # 0 -> 2 quarters, flat at 64, then a jump: one event for the flat run
    parts = {"P1": {"curves": [curve([[0, 64], [2, 64], [2, 100]], cc=1, interp="linear")]}}
    merged, count = _part_midis(tmp_path, build_score(), parts)
    ccs = [e.parameter2 for _, e in _timed(_read_midi(merged.paths[0]).tracks[1]) if e.type == CC]
    assert ccs == [64, 100]
    assert count == 2


def test_merge_into_the_tail_keeps_the_end_of_track(tmp_path):
    plain, _ = _part_midis(tmp_path, build_score(), name="plain")
    parts = {"P1": {"curves": [curve([[0, 0], [11, 127]], cc=1)]}}
    merged, _ = _part_midis(tmp_path, build_score(), parts)
    end = _timed(_read_midi(plain.paths[0]).tracks[1])[-1][0]
    timed = _timed(_read_midi(merged.paths[0]).tracks[1])
    assert timed[-1][1].type == midi.MetaEvents.END_OF_TRACK
    assert timed[-1][0] == end
    assert merged.seconds == plain.seconds


def test_empty_curves_leave_the_part_files_byte_identical(tmp_path):
    plain, _ = _part_midis(tmp_path, build_score(), name="plain")
    empty, count = _part_midis(tmp_path, build_score(), {"P1": {"curves": []}}, name="empty")
    assert count == 0
    assert [p.read_bytes() for p in empty.paths] == [p.read_bytes() for p in plain.paths]


def test_merge_shared_channel_is_an_error_for_the_single_file(tmp_path):
    """The fixture's parts have no instruments, so music21 puts both on
    channel 1: under FluidSynth a curve for one would shape the other."""
    plan = plan_for(tmp_path, build_score(), {"P1": {"curves": [curve([[0, 1]], cc=1)]}})
    path = tmp_path / "score.mid"
    expand_repeats(build_score())[0].write("midi", fp=str(path))
    mf = _read_midi(path)
    with pytest.raises(ChanteyError, match="'P1' shares MIDI channel 1 with 'P2'"):
        expression.merge(plan, mf.tracks[1:], mf.ticksPerQuarterNote, own_files=False)


def test_merge_distinct_instruments_get_distinct_channels(tmp_path):
    score = build_score()
    score.parts[0].insert(0, instrument.Trumpet())
    score.parts[1].insert(0, instrument.Tuba())
    plan = plan_for(tmp_path, score, {"P2": {"curves": [curve([[0, 1]], cc=1)]}})
    path = tmp_path / "score.mid"
    expand_repeats(score)[0].write("midi", fp=str(path))
    mf = _read_midi(path)
    assert (
        expression.merge(plan, mf.tracks[1:], mf.ticksPerQuarterNote, own_files=False).events == 1
    )
    (cc,) = [e for _, e in _timed(mf.tracks[2]) if e.type == CC]
    tuba_channels = {e.channel for _, e in _timed(mf.tracks[2]) if e.isNoteOn()}
    assert tuba_channels == {cc.channel}
    assert not [e for _, e in _timed(mf.tracks[1]) if e.type == CC]


def test_merge_refuses_a_controller_the_export_already_drives(tmp_path):
    plan = plan_for(tmp_path, build_score(), {"P1": {"curves": [curve([[0, 1]], cc=7)]}})
    path = tmp_path / "score.mid"
    expand_repeats(build_score())[0].write("midi", fp=str(path))
    mf = _read_midi(path)
    track = mf.tracks[1]
    volume = midi.MidiEvent(track, type=CC, channel=1)
    volume.parameter1, volume.parameter2 = 7, 100
    track.events[0:0] = [midi.DeltaTime(track, time=0), volume]
    with pytest.raises(ChanteyError, match="already has CC7 events"):
        expression.merge(plan, mf.tracks[1:], mf.ticksPerQuarterNote, own_files=True)


# --- the CLI -----------------------------------------------------------------


class FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def fake_engines(tmp_path):
    """fluidsynth, sfizz_render and hornpipe 'installed'; `midis` records
    the bytes of every MIDI file an engine was given."""
    sf = tmp_path / "gm.sf2"
    sf.write_bytes(b"")
    midis: list[bytes] = []

    def run(argv, env=None, fsize_limit=None):
        prog = Path(argv[0]).name
        if prog == "fluidsynth":
            midis.append(Path(argv[argv.index("-ni") + 2]).read_bytes())
            Path(argv[argv.index("-F") + 1]).write_bytes(b"RIFFwav")
        elif prog == "sfizz_render":
            midis.append(Path(argv[argv.index("--midi") + 1]).read_bytes())
            with wave.open(argv[argv.index("--wav") + 1], "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(array("h", [100] * 20).tobytes())
        elif prog == "hornpipe":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"out")
        return FakeProc()

    deps = Deps(
        which=lambda name: f"/fake/bin/{name}", run=run, environ={"CHANTEY_SOUNDFONT": str(sf)}
    )
    return deps, midis


@pytest.fixture
def sfz(tmp_path):
    path = tmp_path / "a.sfz"
    path.write_text("<region> sample=x.wav\n")
    return str(path)


SFIZZ = ["--voice", "piano", "--no-pdf", "--engine", "sfizz"]


def test_cli_output_line_names_the_sidecar_parts_and_events(score_file, tmp_path, sfz, capsys):
    deps, _ = fake_engines(tmp_path)
    side = write_sidecar(
        tmp_path,
        {
            "P1": {"curves": [curve([[0, 90], [4, 60]], cc=11)]},
            "P2": {"curves": [curve([[0, 0], [2, 127]], cc=64), curve([[0, 100]], cc=1)]},
        },
    )
    score = score_file()
    argv = ["render", str(score), *SFIZZ, "--sfz", sfz, "--expression", str(side)]
    assert main(argv, deps=deps) == 0
    (line,) = [x for x in capsys.readouterr().out.splitlines() if x.startswith("Wrote")]
    assert line.startswith(f"Wrote {score.with_suffix('')}.piano.wav  [engine: sfizz; ")
    assert line.endswith(f"]  [expression: {side}; parts: 2; events: 5]")


def test_cli_without_or_with_an_empty_sidecar_gives_identical_midi(score_file, tmp_path, sfz):
    score = score_file()
    empty = write_sidecar(tmp_path, {"P1": {"curves": []}})
    for engine in (["--engine", "sfizz", "--sfz", sfz], ["--engine", "fluidsynth"]):
        deps, midis = fake_engines(tmp_path)
        base = ["render", str(score), "--voice", "piano", "--no-pdf", "--force", *engine]
        assert main(base, deps=deps) == 0
        plain = list(midis)
        midis.clear()
        assert main([*base, "--expression", str(empty)], deps=deps) == 0
        assert midis == plain


def test_cli_fluidsynth_merges_into_the_single_file(tmp_path, capsys):
    score = build_score()
    score.parts[0].insert(0, instrument.Trumpet())
    score.parts[1].insert(0, instrument.Tuba())
    path = tmp_path / "duo.musicxml"
    score.write("musicxml", fp=str(path))
    deps, midis = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, {"P1": {"curves": [curve([[0, 30], [4, 90]], cc=1)]}})
    argv = ["render", str(path), "--voice", "piano", "--no-pdf", "--expression", str(side)]
    assert main(argv, deps=deps) == 0
    assert "[fx: dry]  [expression: " in capsys.readouterr().out
    (data,) = midis
    sent = tmp_path / "sent.mid"
    sent.write_bytes(data)
    tracks = _read_midi(sent).tracks
    assert [e.parameter2 for _, e in _timed(tracks[1]) if e.type == CC] == [30, 90]
    assert not [e for _, e in _timed(tracks[2]) if e.type == CC]


def test_cli_synth_voice_says_it_ignores_the_sidecar(score_file, tmp_path, capsys):
    deps, _ = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, {"P1": {"curves": [curve([[0, 1]], cc=1)]}})
    argv = ["render", str(score_file()), "--voice", "synth", "--no-pdf", "--expression", str(side)]
    assert main(argv, deps=deps) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(x.endswith(".synth.wav  [--expression ignored (piano voice only)]") for x in out)
    assert any(x.endswith(".synth.toml") for x in out)


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ({"Tuba": {"curves": []}}, "no part named 'Tuba'"),
        ({"P1": {"curves": [curve([[0, 200]], cc=1)]}}, "200 is outside 0 to 127"),
        ({"P1": {"curves": [curve([[0, 1], [99, 1]], cc=1)]}}, "offset 99 is past"),
        ({"P1": {"curves": [curve([[2, 1], [1, 1]], cc=1)]}}, "points must be sorted"),
        ({"P1": {"curves": [curve([[0, 1]], cc=1), curve([[0, 2]], cc=1)]}}, "one curve per"),
    ],
)
def test_cli_sidecar_errors_exit_1_and_write_nothing(
    score_file, tmp_path, sfz, capsys, parts, message
):
    deps, midis = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, parts)
    out = tmp_path / "out"
    argv = ["render", str(score_file()), "--engine", "sfizz", "--sfz", sfz, "-o", str(out),
            "--expression", str(side)]  # fmt: skip
    assert main(argv, deps=deps) == 1
    assert message in capsys.readouterr().err
    assert midis == []
    assert not out.exists() or list(out.iterdir()) == []


def test_cli_info_prints_quarters_per_part(score_file, capsys):
    assert main(["info", str(score_file(build_score(with_repeat=True)))]) == 0
    out = capsys.readouterr().out
    assert "part 'P1': 4 measures; 12 quarters as rendered (repeats expanded; 8 as written)" in out
    assert "render tail: 2 s = 3.33333 quarters at the final tempo" in out


def test_cli_info_without_repeats_prints_one_figure(score_file, capsys):
    assert main(["info", str(score_file())]) == 0
    assert "part 'P2': 4 measures; 8 quarters\n" in capsys.readouterr().out


# --- the real sfizz_render ---------------------------------------------------

needs_sfizz = pytest.mark.skipif(
    shutil.which("sfizz_render") is None, reason="sfizz_render not on PATH"
)


def _write_tone(path: Path, seconds: float = 6.0, rate: int = 44100) -> None:
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            array(
                "h", (round(12000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(n))
            ).tobytes()
        )


def _rms(samples, channels, rate, start_s, end_s) -> float:
    mono = samples[int(start_s * rate) * channels : int(end_s * rate) * channels : channels]
    return math.sqrt(sum(x * x for x in mono) / len(mono))


def _render_held(tmp_path, out, extra=()):
    _write_tone(tmp_path / "tone.wav")
    sfz = tmp_path / "mod.sfz"
    # the level crossfades in on CC1, as VPO's *-mod-wheel patches do
    sfz.write_text(
        "<region> sample=tone.wav pitch_keycenter=57 amp_veltrack=0 "
        "xfin_locc1=0 xfin_hicc1=127 ampeg_release=0.05\n"
    )
    score = tmp_path / "held.musicxml"
    if not score.exists():
        held_note_score().write("musicxml", fp=str(score))
    argv = ["render", str(score), *SFIZZ, "--sfz", str(sfz), "-o", str(out), *extra]
    assert main(argv, deps=Deps(environ={})) == 0
    with wave.open(str(out / "held.piano.wav"), "rb") as w:
        return w.getnchannels(), w.getframerate(), array("h", w.readframes(w.getnframes()))


@needs_sfizz
def test_sfizz_cc1_ramp_raises_the_level_inside_a_held_note(tmp_path):
    """Acceptance check (a), in miniature: one held note (8 quarters at 120 bpm,
    4 s), CC1 ramping 0 -> 127 across it, through an SFZ whose level is CC1."""
    side = write_sidecar(
        tmp_path, {"Trumpet": {"curves": [curve([[0, 0], [8, 127]], cc=1, interp="linear")]}}
    )
    channels, rate, samples = _render_held(tmp_path, tmp_path / "ramp", ["--expression", str(side)])
    levels = [_rms(samples, channels, rate, s, s + 0.5) for s in (0.25, 1.25, 2.25, 3.25)]
    assert levels == sorted(levels)
    assert levels[-1] > 2 * levels[0]  # measured: about 790 -> 2115
    # without the sidecar the same note holds one level
    channels, rate, samples = _render_held(tmp_path, tmp_path / "flat")
    flat = [_rms(samples, channels, rate, s, s + 0.5) for s in (0.25, 3.25)]
    assert flat[1] == pytest.approx(flat[0], rel=0.01)


@needs_sfizz
def test_sfizz_empty_sidecar_and_repeat_renders_are_byte_identical(tmp_path):
    empty = write_sidecar(tmp_path, {"Trumpet": {"curves": []}}, name="empty.json")
    ramp = write_sidecar(
        tmp_path,
        {"Trumpet": {"curves": [curve([[0, 0], [8, 127]], cc=1, interp="linear")]}},
        name="ramp.json",
    )
    _render_held(tmp_path, tmp_path / "none")
    _render_held(tmp_path, tmp_path / "empty", ["--expression", str(empty)])
    _render_held(tmp_path, tmp_path / "r1", ["--expression", str(ramp)])
    _render_held(tmp_path, tmp_path / "r2", ["--expression", str(ramp)])
    wav = "held.piano.wav"
    none = (tmp_path / "none" / wav).read_bytes()
    assert (tmp_path / "empty" / wav).read_bytes() == none
    r1 = (tmp_path / "r1" / wav).read_bytes()
    assert r1 == (tmp_path / "r2" / wav).read_bytes()
    assert r1 != none
