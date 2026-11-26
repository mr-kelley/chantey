import copy
import shutil
from pathlib import Path

import pytest
from music21 import pitch

from chantey.cli import main
from chantey.deps import Deps

from .conftest import build_score, make_mxl, write_musicxml


class FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def fake_deps(tmp_path, mxl_source: Path | None = None):
    """All binaries 'installed'; run() side-effects create the output file
    each tool would have produced (vaultctl FakeGocryptfs pattern)."""
    sf = tmp_path / "gm.sf2"
    sf.write_bytes(b"")
    calls: list[list[str]] = []

    def run(argv, env=None):
        calls.append(argv)
        prog = Path(argv[0]).name
        if prog == "fluidsynth":
            Path(argv[argv.index("-F") + 1]).write_bytes(b"RIFFwav")
        elif prog in ("mscore3", "hornpipe"):
            Path(argv[argv.index("-o") + 1]).write_bytes(b"out")
        elif prog == "flatpak" and mxl_source is not None:
            outdir = Path(argv[argv.index("-output") + 1])
            shutil.copy(mxl_source, outdir / "score.mxl")
        return FakeProc()

    deps = Deps(
        which=lambda name: f"/fake/bin/{name}",
        run=run,
        environ={"CHANTEY_SOUNDFONT": str(sf)},
    )
    return deps, calls


def test_info_prints_summary(score_file, capsys):
    assert main(["info", str(score_file())]) == 0
    out = capsys.readouterr().out
    assert "part 'P1': 4 measures" in out
    assert "quarter=100" in out


def test_ingest_normalizes_mxl(tmp_path, capsys):
    mxl = tmp_path / "tune.mxl"
    make_mxl(build_score(title="Tune"), mxl)
    assert main(["ingest", str(mxl)], deps=Deps()) == 0
    out = tmp_path / "tune.musicxml"
    assert out.exists()
    assert "Canonical score written" in capsys.readouterr().out
    # non-clobbering on second run
    assert main(["ingest", str(mxl)], deps=Deps()) == 1


def test_ingest_pdf_runs_audiveris(tmp_path, capsys):
    mxl = tmp_path / "seed.mxl"
    make_mxl(build_score(title="Scanned"), mxl)
    deps, calls = fake_deps(tmp_path, mxl_source=mxl)
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF")
    assert main(["ingest", str(pdf)], deps=deps) == 0
    flatpak_call = calls[0]
    assert f"--filesystem={tmp_path}" in flatpak_call
    assert (tmp_path / "scan.musicxml").exists()
    err = capsys.readouterr().err
    assert "proofing pass" in err  # OMR reminder


def test_render_writes_all_outputs(score_file, tmp_path, capsys):
    score = score_file()
    deps, calls = fake_deps(tmp_path)
    assert main(["render", str(score)], deps=deps) == 0
    stem = score.with_suffix("")
    assert Path(f"{stem}.piano.wav").exists()
    assert Path(f"{stem}.synth.toml").exists()
    assert Path(f"{stem}.synth.wav").exists()
    assert Path(f"{stem}.pdf").exists()
    # the synth toml is a real hornpipe score normalized to seconds
    assert "tempo = 60" in Path(f"{stem}.synth.toml").read_text()
    # non-clobber second run
    assert main(["render", str(score)], deps=deps) == 1
    assert main(["render", str(score), "--force"], deps=deps) == 0


def test_render_voice_selection(score_file, tmp_path):
    score = score_file()
    deps, calls = fake_deps(tmp_path)
    assert main(["render", str(score), "--voice", "piano", "--no-pdf"], deps=deps) == 0
    stem = score.with_suffix("")
    assert Path(f"{stem}.piano.wav").exists()
    assert not Path(f"{stem}.synth.wav").exists()
    assert not Path(f"{stem}.pdf").exists()


def _fluidsynth_call(calls):
    return next(c for c in calls if Path(c[0]).name == "fluidsynth")


def test_render_piano_is_dry_by_default(score_file, tmp_path, capsys):
    score = score_file()
    deps, calls = fake_deps(tmp_path)
    assert main(["render", str(score), "--voice", "piano", "--no-pdf"], deps=deps) == 0
    assert _fluidsynth_call(calls)[-4:] == ["-C", "0", "-R", "0"]
    piano = f"{score.with_suffix('')}.piano.wav"
    assert f"Wrote {piano}  [fx: dry]" in capsys.readouterr().out.splitlines()


def test_render_fx_default_passes_no_effects_flags(score_file, tmp_path, capsys):
    score = score_file()
    deps, calls = fake_deps(tmp_path)
    assert main(["render", str(score), "--fx", "default"], deps=deps) == 0
    fs = _fluidsynth_call(calls)
    assert "-C" not in fs and "-R" not in fs and "-o" not in fs
    lines = capsys.readouterr().out.splitlines()
    # only the piano voice carries the fx note
    assert [ln for ln in lines if "[fx:" in ln] == [
        f"Wrote {score.with_suffix('')}.piano.wav  [fx: default]"
    ]
    assert f"Wrote {score.with_suffix('')}.synth.wav" in lines


def test_render_env_opts_appended_and_recorded(score_file, tmp_path, capsys):
    score = score_file()
    deps, calls = fake_deps(tmp_path)
    deps.environ["CHANTEY_FLUIDSYNTH_OPTS"] = "-o synth.polyphony=128"
    argv = ["render", str(score), "--voice", "piano", "--no-pdf", "--fx", "light"]
    assert main(argv, deps=deps) == 0
    fs = _fluidsynth_call(calls)
    assert fs[-3:] == ["synth.reverb.room-size=0.3", "-o", "synth.polyphony=128"]
    out = capsys.readouterr().out
    assert "[fx: light; CHANTEY_FLUIDSYNTH_OPTS: -o synth.polyphony=128]" in out


def test_render_rejects_unknown_fx(score_file, tmp_path):
    deps, _ = fake_deps(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["render", str(score_file()), "--fx", "wet"], deps=deps)
    assert exc.value.code == 2


def test_diff_reports_changed_measures(tmp_path, capsys):
    a = build_score()
    b = copy.deepcopy(a)
    b.parts[0].measure(2).notes[0].pitch = pitch.Pitch("G4")
    pa, pb = tmp_path / "a.musicxml", tmp_path / "b.musicxml"
    write_musicxml(a, pa)
    write_musicxml(b, pb)
    assert main(["diff", str(pa), str(pb), "-v"]) == 0
    out = capsys.readouterr().out
    assert "changed measures: 2" in out
    assert "P1 (1): 2" in out


def test_diff_identical(tmp_path, capsys):
    a = build_score()
    pa, pb = tmp_path / "a.musicxml", tmp_path / "b.musicxml"
    write_musicxml(a, pa)
    write_musicxml(a, pb)
    assert main(["diff", str(pa), str(pb)]) == 0
    assert "identical scores" in capsys.readouterr().out


def test_compare_identical_writes_nothing(tmp_path, capsys):
    a = build_score()
    pa, pb = tmp_path / "a.musicxml", tmp_path / "b.musicxml"
    write_musicxml(a, pa)
    write_musicxml(a, pb)
    assert main(["compare", str(pa), str(pb)]) == 0
    assert "no comparison page" in capsys.readouterr().out
    assert not (tmp_path / "compare.html").exists()


def test_compare_writes_page_and_warns_on_missing_audio(tmp_path, capsys):
    a = build_score(title="Tune")
    b = copy.deepcopy(a)
    b.parts[0].measure(3).notes[0].pitch = pitch.Pitch("A4")
    pa, pb = tmp_path / "a.musicxml", tmp_path / "b.musicxml"
    write_musicxml(a, pa)
    write_musicxml(b, pb)
    assert main(["compare", str(pa), str(pb)]) == 0
    captured = capsys.readouterr()
    assert "changed measures: 3" in captured.out
    assert "run 'chantey render' first" in captured.err
    html = (tmp_path / "compare.html").read_text()
    assert 'data-n="3"' in html
    # non-clobber
    assert main(["compare", str(pa), str(pb)]) == 1


def test_missing_binary_is_actionable(score_file, capsys):
    deps = Deps(which=lambda name: None, run=None, environ={})
    assert main(["render", str(score_file())], deps=deps) == 1
    assert "install" in capsys.readouterr().err


def test_ingest_does_not_inject_default_composer(tmp_path, capsys):
    mxl = tmp_path / "anon.mxl"
    make_mxl(build_score(title="Anon"), mxl)
    assert main(["ingest", str(mxl)], deps=Deps()) == 0
    capsys.readouterr()
    assert main(["info", str(tmp_path / "anon.musicxml")]) == 0
    out = capsys.readouterr().out
    assert "Music21" not in out


def test_render_piano_leaves_chord_symbols_silent(score_file, tmp_path):
    import dataclasses

    from music21 import harmony, midi, stream

    score = build_score()
    score.parts[0].getElementsByClass(stream.Measure).first().insert(0, harmony.ChordSymbol("C"))
    deps, _ = fake_deps(tmp_path)
    note_ons = []

    def run(argv, env=None):
        if Path(argv[0]).name == "fluidsynth":
            mf = midi.MidiFile()
            mf.open(argv[3])
            mf.read()
            mf.close()
            note_ons.append(
                sum(
                    1
                    for e in mf.tracks[1].events
                    if e.type == midi.ChannelVoiceMessages.NOTE_ON and e.velocity
                )
            )
        return fake_run(argv, env=env)

    fake_run, deps = deps.run, dataclasses.replace(deps, run=run)
    assert main(["render", str(score_file(score)), "--voice", "piano", "--no-pdf"], deps=deps) == 0
    assert note_ons == [8]  # part 1's eight notes; the C chord symbol adds none
