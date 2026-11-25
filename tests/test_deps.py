import resource
import sys
from pathlib import Path

import pytest

from chantey import deps as depsmod
from chantey.deps import Deps
from chantey.errors import ChanteyError, MissingBinaryError, OutputCapError, ToolRunError

FSIZE_AT_IMPORT = resource.getrlimit(resource.RLIMIT_FSIZE)


def _deps(which_map=None, environ=None, run=None):
    return Deps(
        which=lambda name: (which_map or {}).get(name),
        run=run or (lambda argv, env=None: None),
        environ=environ or {},
    )


def test_missing_binary_names_install():
    with pytest.raises(MissingBinaryError, match="apt install musescore3"):
        depsmod.require_mscore(_deps())
    with pytest.raises(MissingBinaryError, match="fluidsynth"):
        depsmod.require_fluidsynth(_deps())
    with pytest.raises(MissingBinaryError, match="uv tool install"):
        depsmod.require_hornpipe(_deps())
    with pytest.raises(MissingBinaryError, match="flatpak"):
        depsmod.require_flatpak(_deps())


def test_mscore_fallback_name():
    deps = _deps({"musescore3": "/usr/bin/musescore3"})
    assert depsmod.require_mscore(deps) == "/usr/bin/musescore3"


def test_audiveris_argv_grants_filesystem():
    argv = depsmod.audiveris_argv(
        "/usr/bin/flatpak", Path("/work"), Path("/work/x.pdf"), Path("/tmp/out")
    )
    assert argv[:3] == ["/usr/bin/flatpak", "run", "--filesystem=/work"]
    assert depsmod.AUDIVERIS_APP in argv
    assert "-batch" in argv and "-export" in argv
    assert argv[-1] == "/work/x.pdf"


def test_mscore_env_is_headless():
    env = depsmod.mscore_env(_deps(environ={"HOME": "/h"}))
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert env["HOME"] == "/h"


BASE_FS_ARGV = ["fs", "-ni", "/sf.sf2", "in.mid", "-F", "out.wav", "-r", "44100", "-g", "0.6"]


def test_fluidsynth_argv_is_dry_by_default():
    argv = depsmod.fluidsynth_argv("fs", "/sf.sf2", Path("in.mid"), Path("out.wav"))
    assert argv == BASE_FS_ARGV + ["-C", "0", "-R", "0"]


@pytest.mark.parametrize(
    ("fx", "tail"),
    [
        ("dry", ["-C", "0", "-R", "0"]),
        (
            "light",
            [
                "-C",
                "0",
                "-o",
                "synth.reverb.active=1",
                "-o",
                "synth.reverb.level=0.3",
                "-o",
                "synth.reverb.room-size=0.3",
            ],
        ),
        ("default", []),
    ],
)
def test_fluidsynth_argv_fx_presets(fx, tail):
    argv = depsmod.fluidsynth_argv("fs", "/sf.sf2", Path("in.mid"), Path("out.wav"), fx=fx)
    assert argv == BASE_FS_ARGV + tail


def test_fluidsynth_extra_opts_follow_the_preset():
    argv = depsmod.fluidsynth_argv(
        "fs", "/sf.sf2", Path("in.mid"), Path("out.wav"), fx="light", extra=["-o", "a=1"]
    )
    assert argv[-2:] == ["-o", "a=1"]
    assert argv[:-2] == depsmod.fluidsynth_argv(
        "fs", "/sf.sf2", Path("in.mid"), Path("out.wav"), fx="light"
    )


def test_fluidsynth_extra_opts_from_env():
    assert depsmod.fluidsynth_extra_opts(_deps()) == []
    env = {"CHANTEY_FLUIDSYNTH_OPTS": "-o 'synth.reverb.level=0.1' -g 0.5"}
    assert depsmod.fluidsynth_extra_opts(_deps(environ=env)) == [
        "-o",
        "synth.reverb.level=0.1",
        "-g",
        "0.5",
    ]


def test_fluidsynth_extra_opts_unsplittable_is_an_error():
    env = {"CHANTEY_FLUIDSYNTH_OPTS": "-o 'unterminated"}
    with pytest.raises(ChanteyError, match="CHANTEY_FLUIDSYNTH_OPTS"):
        depsmod.fluidsynth_extra_opts(_deps(environ=env))


def test_fx_label():
    assert depsmod.fx_label("dry", []) == "fx: dry"
    label = depsmod.fx_label("light", ["-o", "a b=1"])
    assert label == "fx: light; CHANTEY_FLUIDSYNTH_OPTS: -o 'a b=1'"


def test_hornpipe_argv_normalizes():
    argv = depsmod.hornpipe_argv("hp", Path("s.toml"), Path("s.wav"))
    assert "--normalize" in argv and "render" in argv


def test_soundfont_env_override(tmp_path):
    sf = tmp_path / "x.sf2"
    sf.write_bytes(b"")
    deps = _deps(environ={"CHANTEY_SOUNDFONT": str(sf)})
    assert depsmod.soundfont_path(deps) == str(sf)
    with pytest.raises(MissingBinaryError, match="SoundFont"):
        depsmod.soundfont_path(_deps(environ={"CHANTEY_SOUNDFONT": str(tmp_path / "no.sf2")}))


def test_run_checked_raises_with_tail():
    class Proc:
        returncode = 3
        stdout = ""
        stderr = "line1\nfatal thing"

    deps = _deps(run=lambda argv, env=None: Proc())
    with pytest.raises(ToolRunError, match="fatal thing"):
        depsmod.run_checked(deps, ["prog"], what="prog")


def test_sfizz_argv_ends_at_end_of_track():
    argv = depsmod.sfizz_argv("/bin/sfizz_render", "/lib/a.sfz", Path("p.mid"), Path("p.wav"))
    assert argv[-7:] == ["-s", "44100", "-b", "256", "-q", "10", "--use-eot"]


def test_sfizz_output_cap_is_twice_the_expected_bytes_plus_a_mib():
    assert depsmod.sfizz_output_cap(10.0) == 2 * 4 * 441000 + (1 << 20)


def test_work_tmpdir_prefers_chantey_tmpdir_then_tmpdir(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert depsmod.work_tmpdir(_deps(environ={})) is None
    assert depsmod.work_tmpdir(_deps(environ={"TMPDIR": str(b)})) == str(b)
    both = {"TMPDIR": str(b), "CHANTEY_TMPDIR": str(a)}
    assert depsmod.work_tmpdir(_deps(environ=both)) == str(a)
    with pytest.raises(ChanteyError, match=r"\$CHANTEY_TMPDIR is not a directory"):
        depsmod.work_tmpdir(_deps(environ={"CHANTEY_TMPDIR": str(tmp_path / "none")}))


def test_run_checked_stops_a_child_at_the_file_size_cap(tmp_path):
    """The real runner: RLIMIT_FSIZE in the child; dd takes SIGXFSZ's default
    (killed) → OutputCapError, output stopped at the cap."""
    out = tmp_path / "big"
    argv = ["dd", "if=/dev/zero", f"of={out}", "bs=1M", "count=1"]
    with pytest.raises(OutputCapError, match="prog was stopped at chantey's 65,536-byte"):
        depsmod.run_checked(Deps(), argv, what="prog", fsize_limit=1 << 16)
    assert out.stat().st_size == 1 << 16
    depsmod.run_checked(Deps(), argv, what="prog", fsize_limit=2 << 20, output=out)
    assert out.stat().st_size == 1 << 20
    # the limit was the child's alone
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == FSIZE_AT_IMPORT


WRITE_MIB = "import sys; open(sys.argv[1], 'wb').write(bytes(1 << 20))"


def test_run_checked_cap_also_caught_by_output_size(tmp_path):
    """Python ignores SIGXFSZ, so it sees EFBIG and exits 1: the full output
    file still makes it a cap, not a plain failure."""
    out = tmp_path / "big"
    argv = [sys.executable, "-c", WRITE_MIB, str(out)]
    with pytest.raises(OutputCapError):
        depsmod.run_checked(Deps(), argv, what="prog", fsize_limit=1 << 16, output=out)
    with pytest.raises(ToolRunError, match="File too large") as exc:
        depsmod.run_checked(Deps(), argv, what="prog", fsize_limit=1 << 16)
    assert not isinstance(exc.value, OutputCapError)  # no output named: just a failure


def test_run_checked_without_a_cap_calls_run_as_before():
    """Fakes that predate the cap take (argv, env); no cap, no new keyword."""

    class Proc:
        returncode = 0

    depsmod.run_checked(_deps(run=lambda argv, env=None: Proc()), ["prog"], what="prog")
