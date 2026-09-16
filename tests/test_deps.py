from pathlib import Path

import pytest

from chantey import deps as depsmod
from chantey.deps import Deps
from chantey.errors import MissingBinaryError, ToolRunError


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


def test_fluidsynth_argv():
    argv = depsmod.fluidsynth_argv("fs", "/sf.sf2", Path("in.mid"), Path("out.wav"))
    assert argv == ["fs", "-ni", "/sf.sf2", "in.mid", "-F", "out.wav", "-r", "44100", "-g", "0.6"]


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
