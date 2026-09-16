"""External-binary layer: locate the required programs, build their argv,
run them. All lookups and process execution are injectable (vaultctl's Deps
pattern) so the test suite asserts argv/env without touching real binaries.

Externals (spec: External prerequisites): Audiveris via flatpak (OMR),
mscore3 (PDF), fluidsynth + a GM SoundFont (piano voice), hornpipe (synth
voice). Each check fails with an actionable install hint, never silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import MissingBinaryError, ToolRunError

AUDIVERIS_APP = "org.audiveris.audiveris"
DEFAULT_SOUNDFONT = "/usr/share/sounds/sf2/FluidR3_GM.sf2"


def _default_run(argv: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, capture_output=True, text=True)


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


def fluidsynth_argv(fluidsynth: str, soundfont: str, midi: Path, wav: Path) -> list[str]:
    return [fluidsynth, "-ni", soundfont, str(midi), "-F", str(wav), "-r", "44100", "-g", "0.6"]


def hornpipe_argv(hornpipe: str, toml: Path, wav: Path) -> list[str]:
    # --force is safe here: chantey enforced its own non-clobber check first.
    return [hornpipe, "render", str(toml), "-o", str(wav), "--normalize", "--force"]


def run_checked(deps: Deps, argv: list[str], *, env: dict | None = None, what: str = "") -> None:
    """Run an external program; raise ToolRunError with the output tail on
    failure. Callers verify the expected output file exists afterwards."""
    proc = deps.run(argv, env=env)
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-8:]
        detail = "\n  ".join(tail) if tail else "(no output)"
        raise ToolRunError(f"{what or argv[0]} failed (exit {proc.returncode}):\n  {detail}")
