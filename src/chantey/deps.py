"""External-binary layer: locate the required programs, build their argv,
run them. All lookups and process execution are injectable (vaultctl's Deps
pattern) so the test suite asserts argv/env without touching real binaries.

Externals (spec: External prerequisites): Audiveris via flatpak (OMR),
mscore3 (PDF), fluidsynth + a GM SoundFont (piano voice), sfizz_render
(piano voice under --engine sfizz), hornpipe (synth voice). Each check
fails with an actionable install hint, never silently.
"""

from __future__ import annotations

import os
import resource
import shlex
import shutil
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ChanteyError, MissingBinaryError, OutputCapError, ToolRunError

AUDIVERIS_APP = "org.audiveris.audiveris"
DEFAULT_SOUNDFONT = "/usr/share/sounds/sf2/FluidR3_GM.sf2"


def _fsize_limiter(limit: int) -> Callable[[], None]:
    """preexec_fn: cap every file the child writes at `limit` bytes (never
    above a limit chantey itself already runs under). Past it the kernel
    sends SIGXFSZ, which subprocess restores to its default: terminate."""

    def apply() -> None:
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        cap = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))

    return apply


def _default_run(
    argv: list[str], *, env: dict | None = None, fsize_limit: int | None = None
) -> subprocess.CompletedProcess:
    preexec = _fsize_limiter(fsize_limit) if fsize_limit is not None else None
    return subprocess.run(argv, env=env, capture_output=True, text=True, preexec_fn=preexec)


@dataclass
class Deps:
    which: Callable[[str], str | None] = shutil.which
    run: Callable[..., subprocess.CompletedProcess] = _default_run
    environ: dict = field(default_factory=lambda: dict(os.environ))


def _require(deps: Deps, names: list[str], hint: str) -> str:
    for name in names:
        path = deps.which(name)
        if path:
            return path
    raise MissingBinaryError(f"'{names[0]}' not found — install it with: {hint}")


def require_flatpak(deps: Deps) -> str:
    return _require(deps, ["flatpak"], "sudo apt install flatpak")


def require_mscore(deps: Deps) -> str:
    return _require(deps, ["mscore3", "musescore3"], "sudo apt install musescore3")


def require_fluidsynth(deps: Deps) -> str:
    return _require(deps, ["fluidsynth"], "sudo apt install fluidsynth fluid-soundfont-gm")


def require_sfizz(deps: Deps) -> str:
    return _require(deps, ["sfizz_render"], "the sfztools 'sfizz' package (provides sfizz_render)")


def require_hornpipe(deps: Deps) -> str:
    return _require(
        deps,
        ["hornpipe"],
        "uv tool install git+https://github.com/mr-kelley/hornpipe  (companion tool)",
    )


def soundfont_path(deps: Deps) -> str:
    path = deps.environ.get("CHANTEY_SOUNDFONT", DEFAULT_SOUNDFONT)
    if not Path(path).exists():
        raise MissingBinaryError(
            f"SoundFont not found at {path} — install it with: sudo apt install "
            "fluid-soundfont-gm  (or set CHANTEY_SOUNDFONT)"
        )
    return path


def audiveris_argv(flatpak: str, workdir: Path, pdf: Path, outdir: Path) -> list[str]:
    """Headless OMR. The flatpak sandbox only sees xdg-documents/pictures by
    default, so the working directory must be granted explicitly."""
    return [
        flatpak,
        "run",
        f"--filesystem={workdir}",
        AUDIVERIS_APP,
        "-batch",
        "-export",
        "-output",
        str(outdir),
        str(pdf),
    ]


def mscore_argv(mscore: str, source: Path, out: Path) -> list[str]:
    return [mscore, "-o", str(out), str(source)]


def mscore_env(deps: Deps) -> dict:
    env = dict(deps.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"  # headless render, no X needed
    return env


# Piano-voice effects presets. FluidSynth's own defaults (three-voice chorus,
# reverb at level 0.9) bake space into every stem, where a mix cannot remove
# it, so `dry` is the default and `default` must be asked for.
FX_PRESETS: dict[str, tuple[str, ...]] = {
    "dry": ("-C", "0", "-R", "0"),
    "light": (
        "-C",
        "0",
        "-o",
        "synth.reverb.active=1",
        "-o",
        "synth.reverb.level=0.3",
        "-o",
        "synth.reverb.room-size=0.3",
    ),
    "default": (),
}
DEFAULT_FX = "dry"
FLUIDSYNTH_OPTS_ENV = "CHANTEY_FLUIDSYNTH_OPTS"


def fluidsynth_extra_opts(deps: Deps) -> list[str]:
    """Extra FluidSynth arguments from CHANTEY_FLUIDSYNTH_OPTS, split
    shell-style and forwarded verbatim; their validity is the caller's."""
    raw = deps.environ.get(FLUIDSYNTH_OPTS_ENV, "")
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise ChanteyError(f"{FLUIDSYNTH_OPTS_ENV} cannot be split: {exc}") from None


def fx_label(fx: str, extra: list[str]) -> str:
    """The effective setting as printed on the render's output line."""
    return f"fx: {fx}" + (f"; {FLUIDSYNTH_OPTS_ENV}: {shlex.join(extra)}" if extra else "")


def fluidsynth_argv(
    fluidsynth: str,
    soundfont: str,
    midi: Path,
    wav: Path,
    *,
    fx: str = DEFAULT_FX,
    extra: list[str] | None = None,
) -> list[str]:
    return [
        fluidsynth,
        "-ni",
        soundfont,
        str(midi),
        "-F",
        str(wav),
        "-r",
        "44100",
        "-g",
        "0.6",
        *FX_PRESETS[fx],
        *(extra or []),
    ]


# sfizz engine settings. The quality is sfizz's top sample (resampling)
# quality — an offline render has no realtime budget to protect. The block
# size is pinned, not left to sfizz_render's default, because envelopes and
# modulation are processed per block: the same render at another block size
# is not the same bytes. --use-eot ends the render at the MIDI's last
# End-of-Track instead of when every voice is silent: sfizz 1.2.3 can lose a
# note-off (patches with delay_random over a looped sample) and then renders
# that voice forever.
SFIZZ_RATE = 44100
SFIZZ_QUALITY = 10
SFIZZ_BLOCKSIZE = 256
# The cap on each stem sfizz_render writes: twice the bytes the score plus
# its tail should take (16-bit stereo), plus a MiB. Reaching it is an error.
SFIZZ_CAP_FACTOR = 2
SFIZZ_CAP_SLACK = 1 << 20
TMPDIR_ENV = "CHANTEY_TMPDIR"


def sfizz_output_cap(seconds: float) -> int:
    return SFIZZ_CAP_FACTOR * 4 * round(seconds * SFIZZ_RATE) + SFIZZ_CAP_SLACK


def work_tmpdir(deps: Deps) -> str | None:
    """Where chantey's large working files go: $CHANTEY_TMPDIR, else $TMPDIR,
    else the system default (None). A named directory must exist."""
    for name in (TMPDIR_ENV, "TMPDIR"):
        path = deps.environ.get(name)
        if path:
            if not Path(path).is_dir():
                raise ChanteyError(f"${name} is not a directory: {path}")
            return path
    return None


def sfizz_argv(sfizz: str, sfz: str, midi: Path, wav: Path) -> list[str]:
    return [
        sfizz,
        "--sfz",
        sfz,
        "--midi",
        str(midi),
        "--wav",
        str(wav),
        "-s",
        str(SFIZZ_RATE),
        "-b",
        str(SFIZZ_BLOCKSIZE),
        "-q",
        str(SFIZZ_QUALITY),
        "--use-eot",
    ]


def hornpipe_argv(hornpipe: str, toml: Path, wav: Path) -> list[str]:
    # --force is safe here: chantey enforced its own non-clobber check first.
    return [hornpipe, "render", str(toml), "-o", str(wav), "--normalize", "--force"]


def run_checked(
    deps: Deps,
    argv: list[str],
    *,
    env: dict | None = None,
    what: str = "",
    fsize_limit: int | None = None,
    output: Path | None = None,
) -> None:
    """Run an external program; raise ToolRunError with the output tail on
    failure. With `fsize_limit`, raise OutputCapError if the child was killed
    by SIGXFSZ or `output` reached the limit (a child that ignores SIGXFSZ
    gets EFBIG instead and may exit either way). Callers verify the expected
    output file exists afterwards."""
    if fsize_limit is None:
        proc = deps.run(argv, env=env)
    else:
        proc = deps.run(argv, env=env, fsize_limit=fsize_limit)
        full = output is not None and output.exists() and output.stat().st_size >= fsize_limit
        if proc.returncode == -signal.SIGXFSZ or full:
            raise OutputCapError(what or argv[0], fsize_limit)
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-8:]
        detail = "\n  ".join(tail) if tail else "(no output)"
        raise ToolRunError(f"{what or argv[0]} failed (exit {proc.returncode}):\n  {detail}")
