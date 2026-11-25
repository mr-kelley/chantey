import math
import os
import shutil
import signal
import struct
import wave
from array import array
from pathlib import Path

import pytest
from music21 import midi, note

from chantey import deps as depsmod
from chantey import sfzengine
from chantey.cli import main
from chantey.deps import Deps
from chantey.errors import ChanteyError, OutputCapError

from .conftest import build_score

# --- fixtures: synthesized audio, no library ---------------------------------


def write_wav(path: Path, samples, *, channels: int = 1, rate: int = 44100) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array("h", samples).tobytes())
    return path


def read_wav(path: Path) -> tuple[int, int, array]:
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getframerate(), array("h", w.readframes(w.getnframes()))


TONE_HZ = 220.0  # A3, MIDI 57
C3, C4 = 130.813, 261.626  # the fixture's two parts: P2 plays C3/D3, P1 plays C4/D4


def write_tone(path: Path, seconds: float = 1.0, rate: int = 44100) -> Path:
    n = int(seconds * rate)
    fade = int(0.005 * rate)
    samples = []
    for i in range(n):
        env = min(1.0, i / fade, (n - 1 - i) / fade)
        samples.append(round(12000 * env * math.sin(2 * math.pi * TONE_HZ * i / rate)))
    return write_wav(path, samples, rate=rate)


def write_sfz_kit(tmp_path: Path) -> tuple[Path, Path]:
    """`two.sfz`: one sampled tone, a low region (keys 0-59) and a high region
    (60-127), each repitched from A3. `silent.sfz`: a region on keys 0-10
    only, so the fixture's notes find nothing to play."""
    write_tone(tmp_path / "tone.wav")
    region = "<region> sample=tone.wav pitch_keycenter=57 ampeg_release=0.05"
    two = tmp_path / "two.sfz"
    two.write_text(f"{region} lokey=0 hikey=59\n{region} lokey=60 hikey=127\n")
    silent = tmp_path / "silent.sfz"
    silent.write_text("<region> sample=tone.wav pitch_keycenter=57 lokey=0 hikey=10\n")
    return two, silent


# The runaway-voice reproducer, rebuilt in code: a 1 s
# 440 Hz sine with an `smpl` loop over all of it, and a 70-note legato line
# (ticks at 480/quarter, 176 bpm). Under sfizz 1.2.3, `delay_random` makes
# one note-off go missing and that voice loops forever.
RUNAWAY_SFZ = (
    "<region> sample=loop.wav pitch_keycenter=69 lokey=0 hikey=127 "
    "loop_mode=loop_continuous ampeg_release=0.3 delay_random=0.012 "
    "pitch_random=12 amp_random=1.5\n"
)
RUNAWAY_NOTES = (  # (duration in ticks, key, velocity), back to back
    (480, 76, 45), (160, 77, 100), (480, 77, 126), (320, 77, 126), (320, 69, 100),
    (320, 76, 100), (160, 72, 126), (160, 72, 126), (480, 84, 126), (160, 77, 45),
    (320, 69, 45), (960, 77, 45), (960, 84, 45), (480, 81, 126), (320, 81, 126),
    (320, 69, 126), (960, 69, 126), (960, 77, 100), (960, 81, 126), (160, 69, 45),
    (480, 84, 100), (960, 76, 126), (160, 76, 100), (480, 72, 100), (320, 76, 45),
    (960, 77, 45), (160, 81, 45), (960, 84, 45), (320, 77, 126), (320, 69, 45),
    (480, 69, 45), (480, 77, 100), (160, 76, 126), (480, 77, 45), (480, 76, 100),
    (960, 77, 45), (960, 72, 126), (320, 76, 45), (320, 84, 126), (480, 69, 100),
    (320, 72, 45), (160, 81, 45), (160, 81, 100), (320, 81, 45), (480, 76, 126),
    (320, 69, 126), (960, 81, 100), (480, 84, 100), (960, 84, 126), (960, 72, 100),
    (160, 81, 100), (960, 76, 126), (480, 72, 126), (160, 76, 126), (160, 69, 45),
    (480, 81, 100), (320, 72, 45), (960, 77, 45), (480, 81, 45), (960, 77, 126),
    (960, 69, 45), (480, 81, 100), (960, 84, 126), (160, 72, 126), (320, 84, 45),
    (160, 69, 100), (320, 81, 100), (480, 69, 45), (480, 76, 126), (960, 84, 126),
)  # fmt: skip


def _vlq(n: int) -> bytes:
    out = [n & 0x7F]
    while n > 0x7F:
        n >>= 7
        out.append(0x80 | (n & 0x7F))
    return bytes(reversed(out))


def write_runaway_kit(tmp_path: Path) -> tuple[Path, Path]:
    """The reproducer: (`runaway.sfz`, `runaway.mid`), its End-of-Track one
    quarter after the last note-off."""
    rate, n = 44100, 44100
    pcm = array("h", (round(13107 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)))
    smpl = struct.pack("<9I", 0, 0, 22675, 60, 0, 0, 0, 1, 0)
    smpl += struct.pack("<6I", 0, 0, 0, n - 1, 0, 0)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, 2 * rate, 2, 16)
    chunks = b"".join(
        tag + struct.pack("<I", len(body)) + body
        for tag, body in ((b"fmt ", fmt), (b"smpl", smpl), (b"data", pcm.tobytes()))
    )
    (tmp_path / "loop.wav").write_bytes(
        b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks
    )
    sfz = tmp_path / "runaway.sfz"
    sfz.write_text(RUNAWAY_SFZ)

    track = b"\x00\xff\x51\x03" + (340909).to_bytes(3, "big")
    for duration, key, velocity in RUNAWAY_NOTES:
        track += b"\x00\x90" + bytes((key, velocity)) + _vlq(duration) + b"\x80" + bytes((key, 0))
    track += _vlq(480) + b"\xff\x2f\x00"
    mid = tmp_path / "runaway.mid"
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
    mid.write_bytes(header + b"MTrk" + struct.pack(">I", len(track)) + track)
    return sfz, mid


def tone_power(samples, channels: int, rate: int, freq: float) -> float:
    """Goertzel power of the first channel at `freq`, per sample."""
    mono = samples[::channels]
    coeff = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for x in mono:
        s1, s2 = x + coeff * s1 - s2, s1
    return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / max(len(mono), 1)


# --- the map -----------------------------------------------------------------


def test_parse_sfz_map():
    raw = " Trumpet = /lib/Trumpet.sfz ; Sousaphone=/lib/Tuba.sfz;"
    assert sfzengine.parse_sfz_map(raw) == {
        "Trumpet": "/lib/Trumpet.sfz",
        "Sousaphone": "/lib/Tuba.sfz",
    }
    assert sfzengine.parse_sfz_map("") == {}


@pytest.mark.parametrize("raw", ["Trumpet", "=/x.sfz", "Trumpet=", "A=/a.sfz;A=/b.sfz"])
def test_parse_sfz_map_refuses_malformed(raw):
    with pytest.raises(ChanteyError, match="CHANTEY_SFZ_MAP"):
        sfzengine.parse_sfz_map(raw)


@pytest.fixture
def sfz_files(tmp_path):
    def _make(*names):
        paths = []
        for name in names:
            p = tmp_path / name
            p.write_text("<region> sample=x.wav\n")
            paths.append(str(p))
        return paths

    return _make


def test_resolve_exact_then_case_insensitive_then_default(sfz_files):
    exact, folded, fallback = sfz_files("exact.sfz", "folded.sfz", "fallback.sfz")
    mapping = {"Trumpet": exact, "sousaphone": folded}
    parts, warnings = sfzengine.resolve_parts(["Trumpet", "Sousaphone", "Drums"], mapping, fallback)
    assert [(p.label, p.sfz) for p in parts] == [
        ("Trumpet", exact),
        ("Sousaphone", folded),
        ("Drums", fallback),
    ]
    assert warnings == []


def test_resolve_exact_beats_case_insensitive(sfz_files):
    upper, lower = sfz_files("upper.sfz", "lower.sfz")
    parts, _ = sfzengine.resolve_parts(["TRUMPET"], {"trumpet": lower, "TRUMPET": upper}, None)
    assert parts[0].sfz == upper


def test_resolve_ambiguous_case_insensitive_is_an_error(sfz_files):
    a, b = sfz_files("a.sfz", "b.sfz")
    with pytest.raises(ChanteyError, match="case-insensitively"):
        sfzengine.resolve_parts(["Trumpet"], {"trumpet": a, "TRUMPET": b}, None)


def test_resolve_unmapped_part_is_an_error_naming_it(sfz_files):
    (a,) = sfz_files("a.sfz")
    with pytest.raises(ChanteyError) as exc:
        sfzengine.resolve_parts(["Trumpet", "Sousaphone", None], {"Trumpet": a}, None)
    msg = str(exc.value)
    assert "'Sousaphone'" in msg and "part 3 (no part-name)" in msg
    assert "'Trumpet'" not in msg
    assert "--sfz" in msg and "CHANTEY_SFZ_MAP" in msg


def test_resolve_missing_sfz_file_is_an_error(tmp_path):
    with pytest.raises(ChanteyError, match="SFZ file not found: Tuba="):
        sfzengine.resolve_parts(["Tuba"], {"Tuba": str(tmp_path / "no.sfz")}, None)


def test_resolve_warns_on_map_entry_matching_no_part(sfz_files):
    a, b = sfz_files("a.sfz", "b.sfz")
    _, warnings = sfzengine.resolve_parts(["Trumpet"], {"trumpet": a, "Oboe": b}, None)
    assert warnings == ["CHANTEY_SFZ_MAP entry 'Oboe' matches no part"]


def test_mapping_label_lists_each_part_once(sfz_files):
    a, b = sfz_files("a.sfz", "b.sfz")
    parts, _ = sfzengine.resolve_parts(["Piano", "Piano", "Bass"], {"Bass": b}, a)
    assert sfzengine.mapping_label(parts) == f"Piano={a}, Bass={b}"


# --- per-part MIDI -----------------------------------------------------------


def _read_midi(path):
    mf = midi.MidiFile()
    mf.open(str(path))
    mf.read()
    mf.close()
    return mf


def _note_ons(track):
    return [e.pitch for e in track.events if e.type == midi.ChannelVoiceMessages.NOTE_ON]


def _timed(track):
    """[(tick, event)] for the non-delta events of a track."""
    tick, out = 0, []
    for e in track.events:
        if isinstance(e, midi.DeltaTime):
            tick += e.time
        else:
            out.append((tick, e))
    return out


def _is_tail(e) -> bool:
    return e.type == midi.MetaEvents.END_OF_TRACK or (
        e.type == midi.MetaEvents.TEXT_EVENT and e.data == sfzengine.TAIL_TEXT
    )


def _without_tail(track):
    return [(t, e.getBytes()) for t, e in _timed(track) if not _is_tail(e)]


def _eot_tick(track):
    tick, last = _timed(track)[-1]
    assert last.type == midi.MetaEvents.END_OF_TRACK
    return tick


def _tail_ticks(track):
    return [t for t, e in _timed(track) if _is_tail(e) and e.type == midi.MetaEvents.TEXT_EVENT]


def tempo_change():
    return build_score(tempo_marks=((0.0, 100.0), (4.0, 140.0)))


# 8 quarters, 4 at 100 bpm and 4 at 140, then 2 s at 140 bpm
TEMPO_CHANGE_SECONDS = 4 * 60 / 100 + 4 * 60 / 140 + 2.0


def test_part_midis_carry_the_tempo_map_and_one_part_each(tmp_path):
    result = sfzengine.write_part_midis(tempo_change(), tmp_path)
    assert [p.name for p in result.paths] == ["part01.mid", "part02.mid"]
    full = _read_midi(tmp_path / "score.mid")
    for i, path in enumerate(result.paths, start=1):
        mf = _read_midi(path)
        assert len(mf.tracks) == 2
        assert mf.ticksPerQuarterNote == full.ticksPerQuarterNote
        # the conductor track is the full render's, tempo changes included
        tempos = [e for e in mf.tracks[0].events if e.type == midi.MetaEvents.SET_TEMPO]
        assert len(tempos) == 2
        assert _without_tail(mf.tracks[0]) == _without_tail(full.tracks[0])
        # and the part's events are the full render's, at the same ticks;
        # only the end moves
        assert _without_tail(mf.tracks[1]) == _without_tail(full.tracks[i])
    assert set(_note_ons(_read_midi(result.paths[0]).tracks[1])) == {60, 62}
    assert set(_note_ons(_read_midi(result.paths[1]).tracks[1])) == {48, 50}


def test_part_midis_end_every_track_at_the_score_end_plus_the_tail(tmp_path):
    result = sfzengine.write_part_midis(tempo_change(), tmp_path)
    tpq = _read_midi(result.paths[0]).ticksPerQuarterNote
    # music21 alone ends the part one quarter after its last note-off, and
    # the conductor one quarter after its last tempo change
    full = _read_midi(tmp_path / "score.mid")
    assert [_eot_tick(t) for t in full.tracks] == [5 * tpq, 9 * tpq, 9 * tpq]
    # 2 s at 140 bpm is 4 2/3 quarters
    eot = 8 * tpq + round(2.0 * 140 / 60 * tpq)
    for path in result.paths:
        conductor, part = _read_midi(path).tracks
        assert [_eot_tick(conductor), _eot_tick(part)] == [eot, eot]
        # sfizz_render ignores an EOT's own delta: the text event sets the end
        assert _tail_ticks(conductor) == [eot]
        assert _tail_ticks(part) == []
    assert result.seconds == pytest.approx(TEMPO_CHANGE_SECONDS, abs=1e-4)


def test_part_midis_tail_counts_from_the_notated_end(tmp_path):
    """Trailing rests are part of the score: the tail starts after them."""
    score = build_score(tempo_marks=((0.0, 120.0),))
    for part in score.parts:
        last = list(part.getElementsByClass("Measure"))[-1]
        for n in list(last.notes):
            last.replace(n, note.Rest(quarterLength=1.0))
    result = sfzengine.write_part_midis(score, tmp_path, tail=0.5)
    tpq = _read_midi(result.paths[0]).ticksPerQuarterNote
    assert _eot_tick(_read_midi(result.paths[0]).tracks[1]) == 8 * tpq + tpq
    assert result.seconds == pytest.approx(8 * 0.5 + 0.5, abs=1e-4)


def test_part_midis_keep_a_silent_part(tmp_path):
    score = build_score()
    for n in list(score.parts[1].recurse().notes):
        n.activeSite.replace(n, note.Rest(quarterLength=1.0))
    paths = sfzengine.write_part_midis(score, tmp_path).paths
    assert len(paths) == 2
    assert _note_ons(_read_midi(paths[1]).tracks[1]) == []


# --- the mix -----------------------------------------------------------------


def test_mix_sums_at_unity_and_pads_to_the_longest(tmp_path):
    a = write_wav(tmp_path / "a.wav", [100, 200])
    b = write_wav(tmp_path / "b.wav", [1, 2, 3, -4])
    result = sfzengine.mix_wavs([a, b], tmp_path / "out.wav")
    assert result.frames == 4 and result.scale is None
    assert read_wav(tmp_path / "out.wav") == (1, 44100, array("h", [101, 202, 3, -4]))


def test_mix_scales_a_hot_sum_to_the_ceiling(tmp_path):
    a = write_wav(tmp_path / "a.wav", [30000, -10000], channels=2)
    b = write_wav(tmp_path / "b.wav", [30000, 5000], channels=2)
    result = sfzengine.mix_wavs([a, b], tmp_path / "out.wav")
    ceiling = 32767 * 10 ** (-0.1 / 20)
    assert result.scale == pytest.approx(ceiling / 60000)
    assert result.peak_dbfs == pytest.approx(20 * math.log10(60000 / 32767))
    channels, _, out = read_wav(tmp_path / "out.wav")
    assert channels == 2
    assert out[0] == round(ceiling)
    assert out[1] == round(-5000 * ceiling / 60000)


def test_mix_refuses_mismatched_formats(tmp_path):
    a = write_wav(tmp_path / "a.wav", [0, 0], channels=2)
    b = write_wav(tmp_path / "b.wav", [0, 0], channels=1)
    with pytest.raises(ChanteyError, match="b.wav: 1 ch at 44100 Hz"):
        sfzengine.mix_wavs([a, b], tmp_path / "out.wav")


def test_mix_refuses_non_16_bit(tmp_path):
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(44100)
        w.writeframes(b"\0\0\0")
    with pytest.raises(ChanteyError, match="24-bit"):
        sfzengine.mix_wavs([path], tmp_path / "out.wav")


# --- the CLI, sfizz faked ----------------------------------------------------


class FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def fake_sfizz_deps(environ=None, frames_by_sfz=None, on_run=None):
    """sfizz_render 'installed'; each call writes a stereo WAV whose length
    depends on the SFZ, so the mix's padding is observable. `on_run(n, argv)`
    may return a returncode for the n-th call (from 1), after the write.
    `calls` records argv; `calls.limits` the fsize_limit of each call."""
    calls = Calls()

    def run(argv, env=None, fsize_limit=None):
        calls.append(argv)
        calls.limits.append(fsize_limit)
        sfz = argv[argv.index("--sfz") + 1]
        frames = (frames_by_sfz or {}).get(Path(sfz).name, 10)
        write_wav(Path(argv[argv.index("--wav") + 1]), [1000] * (2 * frames), channels=2)
        proc = FakeProc()
        if on_run is not None:
            proc.returncode = on_run(len(calls), argv) or 0
        return proc

    deps = Deps(which=lambda name: f"/fake/bin/{name}", run=run, environ=environ or {})
    return deps, calls


class Calls(list):
    def __init__(self):
        super().__init__()
        self.limits: list[int | None] = []


def _workdir(argv) -> Path:
    return Path(argv[argv.index("--wav") + 1]).parent


PIANO_ONLY = ["--voice", "piano", "--no-pdf", "--engine", "sfizz"]


def test_cli_sfizz_argv_and_output_line(score_file, sfz_files, capsys):
    score = score_file()
    low, high = sfz_files("low.sfz", "high.sfz")
    deps, calls = fake_sfizz_deps(
        environ={"CHANTEY_SFZ_MAP": f"p2={low}"}, frames_by_sfz={"low.sfz": 25}
    )
    assert main(["render", str(score), *PIANO_ONLY, "--sfz", high], deps=deps) == 0
    assert len(calls) == 2
    first = calls[0]
    assert first[:3] == ["/fake/bin/sfizz_render", "--sfz", high]
    assert first[-7:] == ["-s", "44100", "-b", "256", "-q", "10", "--use-eot"]
    assert Path(first[first.index("--midi") + 1]).name == "part01.mid"
    assert calls[1][2] == low
    # 8 quarters at 100 bpm + the 2 s tail; every part gets the same cap
    cap = depsmod.sfizz_output_cap(4.8 + 2.0)
    assert calls.limits == [cap, cap]
    piano = Path(f"{score.with_suffix('')}.piano.wav")
    channels, rate, samples = read_wav(piano)
    assert (channels, rate, len(samples)) == (2, 44100, 50)  # the longest part
    out = capsys.readouterr().out
    line = (
        f"Wrote {piano}  [engine: sfizz; sfz: P1={high}, P2={low}; "
        f"eot: +2.0 s; cap: {cap / 1e6:.1f} MB per part]"
    )
    assert line in out.splitlines()


def test_cli_sfizz_cap_is_an_error_naming_the_engine(score_file, sfz_files, tmp_path, capsys):
    (sfz,) = sfz_files("a.sfz")
    work = tmp_path / "work"
    work.mkdir()
    deps, calls = fake_sfizz_deps(
        environ={"CHANTEY_TMPDIR": str(work)},
        on_run=lambda n, argv: -signal.SIGXFSZ if n == 2 else 0,
    )
    score = score_file()
    assert main(["render", str(score), *PIANO_ONLY, "--sfz", sfz], deps=deps) == 1
    err = capsys.readouterr().err
    assert "chantey: sfizz_render (P2) was stopped at chantey's " in err
    assert "the score plus its 2 s tail is 6.8 s, but the engine kept rendering past it" in err
    assert not Path(f"{score.with_suffix('')}.piano.wav").exists()
    assert _workdir(calls[0]).parent == work
    assert list(work.iterdir()) == []  # the temp dir went with the failure


def test_cli_sfizz_workdir_follows_chantey_tmpdir_and_is_removed(score_file, sfz_files, tmp_path):
    (sfz,) = sfz_files("a.sfz")
    work = tmp_path / "work"
    work.mkdir()
    seen = []
    deps, calls = fake_sfizz_deps(
        environ={"CHANTEY_TMPDIR": str(work), "TMPDIR": str(tmp_path)},
        on_run=lambda n, argv: seen.append(sorted(p.name for p in _workdir(argv).iterdir())),
    )
    assert main(["render", str(score_file()), *PIANO_ONLY, "--sfz", sfz], deps=deps) == 0
    assert _workdir(calls[0]).parent == work
    assert _workdir(calls[0]).name.startswith("chantey-sfizz-")
    assert "part01.mid" in seen[0]
    assert list(work.iterdir()) == []


def test_cli_sfizz_engine_failure_removes_the_workdir(score_file, sfz_files, tmp_path, capsys):
    (sfz,) = sfz_files("a.sfz")
    deps, calls = fake_sfizz_deps(environ={"TMPDIR": str(tmp_path)}, on_run=lambda n, argv: 1)
    assert main(["render", str(score_file()), *PIANO_ONLY, "--sfz", sfz], deps=deps) == 1
    assert "sfizz_render (P1) failed (exit 1)" in capsys.readouterr().err
    assert _workdir(calls[0]).parent == tmp_path
    assert not _workdir(calls[0]).exists()


def test_cli_sfizz_missing_chantey_tmpdir_is_an_error(score_file, sfz_files, tmp_path, capsys):
    (sfz,) = sfz_files("a.sfz")
    deps, calls = fake_sfizz_deps(environ={"CHANTEY_TMPDIR": str(tmp_path / "none")})
    assert main(["render", str(score_file()), *PIANO_ONLY, "--sfz", sfz], deps=deps) == 1
    assert "$CHANTEY_TMPDIR is not a directory" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_cli_sfizz_signal_removes_the_workdir(score_file, sfz_files, tmp_path, capsys, signum):
    (sfz,) = sfz_files("a.sfz")
    before = signal.getsignal(signum)

    def kill(n, argv):
        os.kill(os.getpid(), signum)

    deps, calls = fake_sfizz_deps(environ={"TMPDIR": str(tmp_path)}, on_run=kill)
    score = score_file()
    assert main(["render", str(score), *PIANO_ONLY, "--sfz", sfz], deps=deps) == 128 + signum
    name = signal.Signals(signum).name
    assert f"chantey: stopped by {name}; temporary files removed" in capsys.readouterr().err
    assert len(calls) == 1  # stopped at the first part
    assert not _workdir(calls[0]).exists()
    assert not Path(f"{score.with_suffix('')}.piano.wav").exists()
    assert signal.getsignal(signum) is before


def test_cli_sfizz_says_fx_and_fluidsynth_opts_are_ignored(score_file, sfz_files, capsys):
    (sfz,) = sfz_files("a.sfz")
    deps, _ = fake_sfizz_deps(environ={"CHANTEY_FLUIDSYNTH_OPTS": "-g 0.5"})
    argv = ["render", str(score_file()), *PIANO_ONLY, "--sfz", sfz, "--fx", "light"]
    assert main(argv, deps=deps) == 0
    out = capsys.readouterr().out
    assert "; --fx light ignored (FluidSynth only)" in out
    assert "; CHANTEY_FLUIDSYNTH_OPTS ignored (FluidSynth only)]" in out


def test_cli_sfizz_unmapped_part_fails_naming_it(score_file, sfz_files, capsys):
    (sfz,) = sfz_files("a.sfz")
    deps, calls = fake_sfizz_deps(environ={"CHANTEY_SFZ_MAP": f"P1={sfz}"})
    assert main(["render", str(score_file()), *PIANO_ONLY], deps=deps) == 1
    assert "no SFZ for 'P2'" in capsys.readouterr().err
    assert calls == []  # nothing rendered, no FluidSynth fallback


def test_cli_sfizz_missing_binary_is_actionable(score_file, sfz_files, capsys):
    (sfz,) = sfz_files("a.sfz")
    deps = Deps(which=lambda name: None, run=None, environ={})
    assert main(["render", str(score_file()), *PIANO_ONLY, "--sfz", sfz], deps=deps) == 1
    assert "'sfizz_render' not found" in capsys.readouterr().err


def test_cli_sfz_needs_the_sfizz_engine(score_file, sfz_files, capsys):
    (sfz,) = sfz_files("a.sfz")
    deps, _ = fake_sfizz_deps()
    assert main(["render", str(score_file()), "--sfz", sfz], deps=deps) == 1
    assert "add --engine sfizz" in capsys.readouterr().err


# --- the real sfizz_render ---------------------------------------------------

needs_sfizz = pytest.mark.skipif(
    shutil.which("sfizz_render") is None, reason="sfizz_render not on PATH"
)


def _render_real(score, outdir, environ, *extra):
    return main(
        ["render", str(score), "--voice", "piano", "--no-pdf", "--engine", "sfizz",
         "-o", str(outdir), *extra],
        deps=Deps(environ=environ),
    )  # fmt: skip


@needs_sfizz
def test_sfizz_renders_both_parts_through_the_two_region_sfz(score_file, tmp_path, capsys):
    two, _ = write_sfz_kit(tmp_path)
    score = score_file()
    assert _render_real(score, tmp_path / "out", {}, "--sfz", str(two)) == 0
    assert "[engine: sfizz; sfz: " in capsys.readouterr().out
    channels, rate, samples = read_wav(tmp_path / "out" / "fixture.piano.wav")
    assert rate == 44100
    # 8 quarters at 100 bpm: the last note starts at 4.2 s and ends at 4.8 s
    assert len(samples) // channels >= int(4.8 * rate)
    low, high = (tone_power(samples, channels, rate, f) for f in (C3, C4))
    assert low > 0 and high > 0
    assert 0.1 < low / high < 10


@needs_sfizz
@pytest.mark.parametrize(("muted", "present", "absent"), [("P2", C4, C3), ("P1", C3, C4)])
def test_sfizz_muting_one_part_removes_its_pitch(score_file, tmp_path, muted, present, absent):
    two, silent = write_sfz_kit(tmp_path)
    environ = {"CHANTEY_SFZ_MAP": f"{muted}={silent}"}
    assert _render_real(score_file(), tmp_path / "out", environ, "--sfz", str(two)) == 0
    channels, rate, samples = read_wav(tmp_path / "out" / "fixture.piano.wav")
    on = tone_power(samples, channels, rate, present)
    off = tone_power(samples, channels, rate, absent)
    assert on > 0
    assert off < on * 1e-3


@needs_sfizz
def test_sfizz_render_is_deterministic(score_file, tmp_path):
    two, _ = write_sfz_kit(tmp_path)
    score = score_file(build_score(tempo_marks=((0.0, 100.0), (4.0, 140.0)), with_repeat=True))
    first, second = tmp_path / "first", tmp_path / "second"
    assert _render_real(score, first, {}, "--sfz", str(two)) == 0
    assert _render_real(score, second, {}, "--sfz", str(two)) == 0
    a = (first / "fixture.piano.wav").read_bytes()
    assert len(a) > 44
    assert a == (second / "fixture.piano.wav").read_bytes()


@needs_sfizz
def test_sfizz_unmapped_part_fails_before_rendering(score_file, tmp_path, capsys):
    two, _ = write_sfz_kit(tmp_path)
    environ = {"CHANTEY_SFZ_MAP": f"P1={two}"}
    assert _render_real(score_file(), tmp_path / "out", environ) == 1
    assert "no SFZ for 'P2'" in capsys.readouterr().err
    assert not (tmp_path / "out" / "fixture.piano.wav").exists()


@needs_sfizz
def test_sfizz_stem_ends_at_the_score_end_plus_the_tail(score_file, tmp_path):
    """--use-eot: the length is the score's (4.8 s) plus 2 s, to the block."""
    two, _ = write_sfz_kit(tmp_path)
    assert _render_real(score_file(), tmp_path / "out", {}, "--sfz", str(two)) == 0
    channels, rate, samples = read_wav(tmp_path / "out" / "fixture.piano.wav")
    assert abs(len(samples) // channels - round(6.8 * rate)) <= depsmod.SFIZZ_BLOCKSIZE


@needs_sfizz
def test_sfizz_runaway_reproducer_ends_at_eot_and_is_capped_without_it(tmp_path):
    """The runaway reproducer through the real sfizz_render: with --use-eot it ends
    at the MIDI's last event before End-of-Track (the last note-off — sfizz
    ignores the EOT's own delta, which is why chantey adds a text event);
    without it, it never ends and the cap stops it."""
    sfz, mid = write_runaway_kit(tmp_path)
    seconds = 35680 * 340909 / 480 / 1e6  # End-of-Track at 176 bpm
    cap = depsmod.sfizz_output_cap(seconds)
    wav = tmp_path / "out.wav"
    argv = depsmod.sfizz_argv(shutil.which("sfizz_render"), str(sfz), mid, wav)
    depsmod.run_checked(Deps(), argv, what="sfizz_render", fsize_limit=cap, output=wav)
    _, rate, samples = read_wav(wav)
    last_off = 35200 * 340909 / 480 / 1e6
    assert abs(len(samples) // 2 - round(last_off * rate)) <= depsmod.SFIZZ_BLOCKSIZE
    assert wav.stat().st_size == 4_410_412  # as measured on the original report
    assert argv[-1] == "--use-eot"
    with pytest.raises(OutputCapError):
        depsmod.run_checked(Deps(), argv[:-1], what="sfizz_render", fsize_limit=cap, output=wav)
    assert wav.stat().st_size == cap
