"""The `keyswitch` curve kind (v0.6) and `info`'s SFZ keyswitch map."""

import json
import math
import shutil
import wave
from array import array
from pathlib import Path

import pytest
from music21 import metadata as m21metadata
from music21 import meter, midi, note, stream, tempo

from chantey import expression, sfzinfo
from chantey.cli import main
from chantey.convert import expand_repeats
from chantey.deps import Deps
from chantey.errors import ChanteyError

from .conftest import build_score
from .test_expression import (
    SFIZZ,
    _part_midis,
    _read_midi,
    _timed,
    curve,
    fake_engines,
    plan_for,
    write_sidecar,
)

NOTE_ON = midi.ChannelVoiceMessages.NOTE_ON
NOTE_OFF = midi.ChannelVoiceMessages.NOTE_OFF


@pytest.fixture
def sfz(tmp_path):
    path = tmp_path / "a.sfz"
    path.write_text("<region> sample=x.wav\n")
    return str(path)


def ks(points, **extra):
    return {"keyswitch": True, "interp": "hold", "points": points, **extra}


def _switches(track):
    """(tick, kind, key, velocity) for the switch notes (keys below 48)."""
    out = []
    for t, e in _timed(track):
        if e.type in (NOTE_ON, NOTE_OFF) and e.pitch < 48:
            out.append((t, "on" if e.isNoteOn() else "off", e.pitch, e.velocity))
    return out


# --- load --------------------------------------------------------------------


def test_load_reads_the_tickets_example(tmp_path):
    path = write_sidecar(tmp_path, {"Trumpet": {"curves": [ks([[0, 36], [16, 39], [20.5, 40]])]}})
    (c,) = expression.load(path).parts["Trumpet"].curves
    assert (c.kind, c.label, c.velocity) == ("keyswitch", "keyswitch", 100)
    assert c.points == ((0.0, 36.0), (16.0, 39.0), (20.5, 40.0))


def test_load_velocity_is_settable_per_curve(tmp_path):
    path = write_sidecar(tmp_path, {"T": {"curves": [ks([[0, 36]], velocity=1)]}})
    assert expression.load(path).parts["T"].curves[0].velocity == 1


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({**ks([[0, 36]]), "interp": "linear"}, "a keyswitch curve is 'hold' only"),
        ({**ks([[0, 36]]), "keyswitch": 36}, ".keyswitch: expected true, got 36"),
        (ks([[0, 128]]), "128 is outside 0 to 127 for keyswitch"),
        (ks([[0, -1]]), "-1 is outside 0 to 127 for keyswitch"),
        (ks([[0, 36.5]]), "36.5 is not a MIDI key"),
        (ks([[0, 36]], velocity=0), ".velocity: expected an integer 1-127, got 0"),
        (ks([[0, 36]], velocity=128), "expected an integer 1-127, got 128"),
        (ks([[0, 36]], velocity=True), "expected an integer 1-127, got True"),
        (ks([[0, 36]], velocity=99.5), "expected an integer 1-127, got 99.5"),
        ({**curve([[0, 1]], cc=1), "velocity": 90}, "'velocity' belongs to a keyswitch curve"),
        ({**ks([[0, 36]]), "cc": 1}, "name exactly one curve kind (cc, bend, keyswitch, time)"),
        ({**ks([[0, 36]]), "note": 36}, "unknown key 'note'"),
    ],
)
def test_load_refuses_a_bad_keyswitch_naming_where(tmp_path, bad, message):
    path = write_sidecar(tmp_path, {"Trumpet": {"curves": [curve([[0, 1]], cc=7), bad]}})
    with pytest.raises(ChanteyError) as exc:
        expression.load(path)
    assert "parts['Trumpet'].curves[1]" in str(exc.value)
    assert message in str(exc.value)


def test_load_refuses_two_keyswitch_curves_for_one_part(tmp_path):
    path = write_sidecar(tmp_path, {"T": {"curves": [ks([[0, 36]]), ks([[0, 38]])]}})
    with pytest.raises(ChanteyError, match=r"a second keyswitch curve \(the first is curves\[0\]"):
        expression.load(path)


# --- resolve -----------------------------------------------------------------


def test_a_keyswitch_is_sent_at_its_points_only(tmp_path):
    """No value from 0, as a controller gets: before the first switch the
    SFZ's sw_default plays."""
    plan = plan_for(tmp_path, build_score(), {"P1": {"curves": [ks([[2, 36], [4, 38]])]}})
    ((c, samples),) = plan.parts[0].samples
    assert samples == ((2.0, 36), (4.0, 38))
    assert plan.keyswitch_parts() == ["P1"]


def test_a_keyswitch_offset_past_the_tail_is_an_error(tmp_path):
    with pytest.raises(ChanteyError, match="part 'P1' keyswitch: offset 11.4 is past the part's"):
        plan_for(tmp_path, build_score(), {"P1": {"curves": [ks([[11.4, 36]])]}})


def test_a_plan_without_keyswitches_names_no_parts(tmp_path):
    plan = plan_for(tmp_path, build_score(), {"P1": {"curves": [curve([[0, 1]], cc=1)]}})
    assert plan.keyswitch_parts() == []


# --- merge -------------------------------------------------------------------


def test_merge_sends_a_one_tick_switch_note_at_each_point(tmp_path):
    parts = {"P1": {"curves": [ks([[0, 36], [2, 40], [5, 39]], velocity=90)]}}
    merged, count = _part_midis(tmp_path, build_score(), parts)
    assert count == 6  # a note-on and a note-off per switch
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote
    assert _switches(_read_midi(merged.paths[0]).tracks[1]) == [
        (0, "on", 36, 90),
        (1, "off", 36, 0),
        (2 * tpq, "on", 40, 90),
        (2 * tpq + 1, "off", 40, 0),
        (5 * tpq, "on", 39, 90),
        (5 * tpq + 1, "off", 39, 0),
    ]


def test_merge_puts_the_switch_before_the_notes_at_its_tick(tmp_path):
    parts = {"P1": {"curves": [ks([[0, 36], [2, 40]])]}}
    merged, _ = _part_midis(tmp_path, build_score(), parts)
    timed = _timed(_read_midi(merged.paths[0]).tracks[1])
    tpq = _read_midi(merged.paths[0]).ticksPerQuarterNote

    def kind(e):
        if e.type in (NOTE_ON, NOTE_OFF) and e.pitch < 48:
            return "switch-on" if e.isNoteOn() else "switch-off"
        return "on" if e.isNoteOn() else "off" if e.isNoteOff() else "x"

    at_zero = [kind(e) for t, e in timed if t == 0 and kind(e) != "x"]
    assert at_zero == ["switch-on", "on"]
    at_two = [kind(e) for t, e in timed if t == 2 * tpq]
    assert at_two == ["off", "switch-on", "on"]  # after the note-off, before the note-on


def test_merge_leaves_every_other_event_unchanged(tmp_path):
    plain, _ = _part_midis(tmp_path, build_score(), name="plain")
    parts = {"P1": {"curves": [ks([[0, 36], [3, 40]]), curve([[0, 90], [4, 60]], cc=11)]}}
    merged, count = _part_midis(tmp_path, build_score(), parts)
    assert count == 4 + 2
    track = _read_midi(merged.paths[0]).tracks[1]
    before = [(t, e.getBytes()) for t, e in _timed(_read_midi(plain.paths[0]).tracks[1])]
    after = [
        (t, e.getBytes())
        for t, e in _timed(track)
        if not (e.type == midi.ChannelVoiceMessages.CONTROLLER_CHANGE)
        and not (e.type in (NOTE_ON, NOTE_OFF) and e.pitch < 48)
    ]
    assert after == before
    assert merged.paths[1].read_bytes() == plain.paths[1].read_bytes()


def test_merge_drops_a_switch_to_the_key_already_selected(tmp_path):
    parts = {"P1": {"curves": [ks([[0, 36], [2, 36], [4, 38], [6, 38], [7, 36]])]}}
    merged, count = _part_midis(tmp_path, build_score(), parts)
    ons = [k for _, kind, k, _ in _switches(_read_midi(merged.paths[0]).tracks[1]) if kind == "on"]
    assert ons == [36, 38, 36]
    assert count == 6


def test_merge_refuses_a_switch_key_inside_the_played_range(tmp_path):
    """P1 plays C4 (60) and D4 (62): key 61 would sound as a pitch."""
    parts = {"P1": {"curves": [ks([[0, 36], [2, 61]])]}}
    with pytest.raises(ChanteyError) as exc:
        _part_midis(tmp_path, build_score(), parts)
    assert str(exc.value) == (
        "part 'P1' keyswitch: key 61 (c#4) is inside the part's played range, 60 (c4) to "
        "62 (d4), so it would sound as a pitch; the nearest played note is 60 (c4), first "
        "at offset 0"
    )


def test_merge_refuses_a_switch_key_on_a_played_note(tmp_path):
    parts = {"P1": {"curves": [ks([[0, 62]])]}}
    with pytest.raises(
        ChanteyError, match=r"the nearest played note is 62 \(d4\), first at offset 1"
    ):
        _part_midis(tmp_path, build_score(), parts)


def test_merge_allows_switch_keys_above_the_range(tmp_path):
    _, count = _part_midis(tmp_path, build_score(), {"P1": {"curves": [ks([[0, 96]])]}})
    assert count == 2


def test_merge_refuses_keyswitches_in_the_single_file(tmp_path):
    plan = plan_for(tmp_path, build_score(), {"P2": {"curves": [ks([[0, 36]])]}})
    path = tmp_path / "score.mid"
    expand_repeats(build_score())[0].write("midi", fp=str(path))
    mf = _read_midi(path)
    with pytest.raises(ChanteyError, match="part 'P2' has a keyswitch curve, which needs"):
        expression.merge(plan, mf.tracks[1:], mf.ticksPerQuarterNote, own_files=False)


# --- the CLI -----------------------------------------------------------------


def test_cli_fluidsynth_keyswitch_is_an_error_naming_sfizz(score_file, tmp_path, capsys):
    deps, midis = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, {"P1": {"curves": [ks([[0, 36]])]}})
    out = tmp_path / "out"
    argv = ["render", str(score_file()), "-o", str(out), "--expression", str(side)]
    assert main(argv, deps=deps) == 1
    err = capsys.readouterr().err
    assert "part 'P1' has a keyswitch curve, which needs --engine sfizz" in err
    assert "a GM SoundFont would play the switch key as a pitch" in err
    assert midis == []
    assert not out.exists() or list(out.iterdir()) == []


def test_cli_synth_voice_alone_ignores_a_keyswitch_curve(score_file, tmp_path, capsys):
    deps, _ = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, {"P1": {"curves": [ks([[0, 36]])]}})
    argv = ["render", str(score_file()), "--voice", "synth", "--no-pdf", "--expression", str(side)]
    assert main(argv, deps=deps) == 0
    assert "[--expression ignored (piano voice only)]" in capsys.readouterr().out


def test_cli_colliding_key_exits_1_and_writes_nothing(score_file, tmp_path, sfz, capsys):
    deps, midis = fake_engines(tmp_path)
    side = write_sidecar(tmp_path, {"P2": {"curves": [ks([[0, 49]])]}})  # P2 plays C3-D3
    out = tmp_path / "out"
    argv = ["render", str(score_file()), *SFIZZ, "--sfz", sfz, "-o", str(out),
            "--expression", str(side)]  # fmt: skip
    assert main(argv, deps=deps) == 1
    assert "key 49 (c#3) is inside the part's played range, 48 (c3) to 50 (d3)" in (
        capsys.readouterr().err
    )
    assert midis == []
    assert not (out / "fixture.piano.wav").exists()


def test_cli_output_line_counts_the_switch_notes(score_file, tmp_path, sfz, capsys):
    deps, _ = fake_engines(tmp_path)
    side = write_sidecar(
        tmp_path,
        {"P1": {"curves": [ks([[0, 36], [2, 40], [4, 36]]), curve([[0, 90]], cc=11)]}},
    )
    argv = ["render", str(score_file()), *SFIZZ, "--sfz", sfz, "--expression", str(side)]
    assert main(argv, deps=deps) == 0
    (line,) = [x for x in capsys.readouterr().out.splitlines() if x.startswith("Wrote")]
    assert line.endswith(f"]  [expression: {side}; parts: 1; events: 7]")


# --- the SFZ keyswitch map ---------------------------------------------------


@pytest.mark.parametrize(
    ("value", "key"),
    [("36", 36), ("c2", 36), ("C2", 36), ("d#2", 39), ("eb2", 39), ("c4", 60), ("c-1", 0),
     ("g9", 127), ("a9", None), ("h2", None), ("", None), ("-1", None)],
)  # fmt: skip
def test_parse_key_numbers_and_note_names(value, key):
    assert sfzinfo.parse_key(value) == key


def test_key_names_use_sfz_spelling():
    assert [sfzinfo.key_label(k) for k in (36, 39, 60, 0)] == [
        "36 (c2)", "39 (d#2)", "60 (c4)", "0 (c-1)"
    ]  # fmt: skip


VPO_LIKE = """\
// trumpet section, keyswitched (shaped like VPO 3.3's trumpet-SEC-KS-C2.sfz)
<control> default_path=Samples/
<global> sw_lokey=c2 sw_hikey=e2 sw_default=c2
/* four articulations */
<group> sw_last=c2 sw_label=Sustain
<region> sample=sus C4.wav lokey=c4 hikey=c4
<region> sample=sus D4.wav lokey=d4 hikey=d4
<group> sw_last=d2 sw_label=Normal
<region> sample=nor C4.wav key=60
<group> sw_last=d#2 sw_label=Accent
<region> sample=acc C4.wav key=60
<group> sw_last=e2 sw_label=Staccato
<region> sample=sta C4.wav key=60
"""


def test_read_keyswitches_from_a_vpo_shaped_file(tmp_path):
    path = tmp_path / "trumpet-KS.sfz"
    path.write_text(VPO_LIKE)
    ks_map = sfzinfo.read_keyswitches(path)
    assert (ks_map.lokey, ks_map.hikey, ks_map.default) == ((36,), (40,), (36,))
    assert ks_map.switches == (
        (36, 36, "Sustain"), (38, 38, "Normal"), (39, 39, "Accent"), (40, 40, "Staccato")
    )  # fmt: skip
    assert sfzinfo.format_keyswitches(ks_map) == [
        "keyswitches: sw_lokey 36 (c2) to sw_hikey 40 (e2); sw_default 36 (c2)",
        "  sw_last 36 (c2): Sustain",
        "  sw_last 38 (d2): Normal",
        "  sw_last 39 (d#2): Accent",
        "  sw_last 40 (e2): Staccato",
    ]


def test_read_keyswitches_follows_headers_defines_and_includes(tmp_path):
    (tmp_path / "arts").mkdir()
    (tmp_path / "arts" / "short.sfz").write_text(
        "<group> sw_last=$SHORT sw_label=Short notes\n<region> sample=s.wav\n"
    )
    path = tmp_path / "main.sfz"
    path.write_text(
        "#define $SHORT 25\n"
        "<global> sw_lokey=24 sw_hikey=26\n"
        "<master> sw_default=24\n"
        "<group> sw_last=24 sw_label=Long\n<region> sample=l.wav\n"
        "<region> sample=l2.wav sw_label=Long (alt)\n"  # the first label for a key wins
        '#include "arts/short.sfz"\n'
        "<master>\n<group> sw_lolast=26 sw_hilast=27\n<region> sample=t.wav\n"
        "<curve> sw_last=99\n"  # not a region header: ignored
    )
    ks_map = sfzinfo.read_keyswitches(path)
    assert ks_map.default == (24,)  # <master>'s, until the second <master> resets it
    assert ks_map.switches == ((24, 24, "Long"), (25, 25, "Short notes"), (26, 27, None))
    lines = sfzinfo.format_keyswitches(ks_map)
    assert lines[-1] == "  sw_lolast 26 (d1) to sw_hilast 27 (d#1): (no sw_label)"


def test_read_keyswitches_notes_shifts_and_bad_values(tmp_path):
    path = tmp_path / "x.sfz"
    path.write_text("<control> octave_offset=-1\n<group> sw_last=zz sw_lokey=36\n<region> key=60\n")
    lines = sfzinfo.format_keyswitches(sfzinfo.read_keyswitches(path))
    assert lines[0] == "keyswitches: sw_lokey 36 (c2) to sw_hikey unset; sw_default unset"
    assert "  note: the file sets octave_offset; the keys above are as written" in lines
    assert "  note: not a key, skipped: sw_last=zz" in lines


def test_read_keyswitches_none(tmp_path):
    path = tmp_path / "plain.sfz"
    path.write_text("<region> sample=x.wav\n")
    assert sfzinfo.format_keyswitches(sfzinfo.read_keyswitches(path)) == ["keyswitches: none"]


def test_read_keyswitches_missing_include_is_an_error(tmp_path):
    path = tmp_path / "x.sfz"
    path.write_text('#include "gone.sfz"\n')
    with pytest.raises(ChanteyError, match="cannot read SFZ .*gone.sfz"):
        sfzinfo.read_keyswitches(path)


def test_cli_info_sfz_prints_each_parts_keyswitch_map(score_file, tmp_path, capsys):
    vpo = tmp_path / "trumpet-KS.sfz"
    vpo.write_text(VPO_LIKE)
    plain = tmp_path / "plain.sfz"
    plain.write_text("<region> sample=x.wav\n")
    deps = Deps(environ={"CHANTEY_SFZ_MAP": f"p2={plain}"})
    assert main(["info", str(score_file()), "--sfz", str(vpo)], deps=deps) == 0
    out = capsys.readouterr().out
    assert (
        "part 'P1': 4 measures; 8 quarters\n"
        f"  sfz: {vpo}\n"
        "  keyswitches: sw_lokey 36 (c2) to sw_hikey 40 (e2); sw_default 36 (c2)\n"
        "    sw_last 36 (c2): Sustain\n"
    ) in out
    assert f"part 'P2': 4 measures; 8 quarters\n  sfz: {plain}\n  keyswitches: none\n" in out


def test_cli_info_map_alone_shows_mapped_parts(score_file, tmp_path, capsys):
    vpo = tmp_path / "t.sfz"
    vpo.write_text(VPO_LIKE)
    deps = Deps(environ={"CHANTEY_SFZ_MAP": f"P2={vpo}"})
    assert main(["info", str(score_file())], deps=deps) == 0
    out = capsys.readouterr().out
    assert "part 'P1': 4 measures; 8 quarters\npart 'P2'" in out
    assert f"part 'P2': 4 measures; 8 quarters\n  sfz: {vpo}\n" in out


def test_cli_info_unreadable_sfz_is_a_warning(score_file, tmp_path, capsys):
    gone = tmp_path / "gone.sfz"
    deps = Deps(environ={"CHANTEY_SFZ_MAP": "broken"})
    assert main(["info", str(score_file()), "--sfz", str(gone)], deps=deps) == 0
    out = capsys.readouterr().out
    assert f"  sfz: {gone}\n" in out
    assert f"WARNING: SFZ file not found: {gone}" in out
    assert (
        "WARNING: CHANTEY_SFZ_MAP: expected '<part-name>=<path>', got 'broken'; map ignored" in out
    )


def test_cli_info_without_sfz_is_unchanged(score_file, capsys):
    assert main(["info", str(score_file())], deps=Deps(environ={})) == 0
    assert "sfz:" not in capsys.readouterr().out


# --- the real sfizz_render ---------------------------------------------------

needs_sfizz = pytest.mark.skipif(
    shutil.which("sfizz_render") is None, reason="sfizz_render not on PATH"
)

RATE = 44100


def _write_tone(path: Path, seconds: float = 3.0) -> None:
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


# Three articulations of one tone: sustained, a quieter "normal", and a
# staccato that holds 0.15 s and is gone by 0.2 s. Switch keys c2-e2 play
# nothing.
KS_SFZ = """\
<global> sw_lokey=c2 sw_hikey=e2 sw_default=c2 lokey=48 hikey=84 sample=tone.wav
  pitch_keycenter=57 amp_veltrack=0 ampeg_release=0.02
<group> sw_last=c2 sw_label=Sustain
<region>
<group> sw_last=d2 sw_label=Normal
<region> volume=-20
<group> sw_last=e2 sw_label=Staccato
<region> ampeg_hold=0.15 ampeg_decay=0.05 ampeg_sustain=0
"""


def four_note_score():
    """Four A3s, two quarters each at 120 bpm: one note per second."""
    score = stream.Score()
    md = m21metadata.Metadata()
    md.title, md.composer = "Four", ""
    score.insert(0, md)
    part = stream.Part()
    part.partName = "Trumpet"
    m = stream.Measure(number=1)
    m.insert(0, meter.TimeSignature("8/4"))
    m.insert(0, tempo.MetronomeMark(number=120))
    for _ in range(4):
        m.append(note.Note("A3", quarterLength=2.0))
    part.append(m)
    score.insert(0, part)
    return score


def _render_four(tmp_path, out, sidecar=None):
    if not (tmp_path / "tone.wav").exists():
        _write_tone(tmp_path / "tone.wav")
        (tmp_path / "ks.sfz").write_text(KS_SFZ)
        four_note_score().write("musicxml", fp=str(tmp_path / "four.musicxml"))
    argv = ["render", str(tmp_path / "four.musicxml"), *SFIZZ, "--sfz", str(tmp_path / "ks.sfz"),
            "-o", str(out)]  # fmt: skip
    if sidecar is not None:
        path = tmp_path / f"{out.name}.json"
        path.write_text(json.dumps({"parts": {"Trumpet": {"curves": [sidecar]}}}))
        argv += ["--expression", str(path)]
    assert main(argv, deps=Deps(environ={})) == 0
    wav = out / "four.piano.wav"
    with wave.open(str(wav), "rb") as w:
        channels = w.getnchannels()
        samples = array("h", w.readframes(w.getnframes()))
    mono = samples[::channels]

    def rms(start, end):
        cut = mono[int(start * RATE) : int(end * RATE)]
        return math.sqrt(sum(x * x for x in cut) / len(cut))

    # per note (one second each): the level early in it, and late in it
    return [(rms(n + 0.02, n + 0.1), rms(n + 0.5, n + 0.9)) for n in range(4)], wav.read_bytes()


@needs_sfizz
def test_sfizz_keyswitches_pick_each_notes_articulation(tmp_path):
    """Acceptance check (a), in miniature: Sustain -> Staccato -> Normal -> Sustain."""
    levels, _ = _render_four(tmp_path, tmp_path / "ks", ks([[0, 36], [2, 40], [4, 38], [6, 36]]))
    (s1_early, s1), (st_early, st), (n_early, n), (s2_early, s2) = levels
    assert s1 > 1000 and s2 == pytest.approx(s1, rel=0.02)  # sustained, both times
    assert st_early == pytest.approx(s1, rel=0.05) and st < 0.01 * s1  # staccato: then gone
    assert n == pytest.approx(s1 / 10, rel=0.05)  # -20 dB
    # without the sidecar every note is sw_default's Sustain
    flat, _ = _render_four(tmp_path, tmp_path / "plain")
    assert all(late == pytest.approx(s1, rel=0.02) for _, late in flat)


@needs_sfizz
def test_sfizz_keyswitch_renders_are_byte_identical(tmp_path):
    curve_ = ks([[0, 36], [2, 40], [4, 38], [6, 36]])
    _, first = _render_four(tmp_path, tmp_path / "r1", curve_)
    _, second = _render_four(tmp_path, tmp_path / "r2", curve_)
    assert first == second
