"""Render a canonical score to audio (both voices) and PDF.

The repeat-parity guarantee lives here: `expand_repeats` is called exactly
once and the SAME expanded score feeds the MIDI/FluidSynth path and the
hornpipe converter. The PDF renders from the original file, keeping repeat
notation on the page.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from music21 import stream

from . import deps as depsmod
from .convert import convert, expand_repeats
from .deps import Deps
from .errors import ToolRunError


@dataclass
class RenderResult:
    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def render(
    score: stream.Score,
    score_path: Path,
    outdir: Path,
    deps: Deps,
    *,
    voice: str = "both",  # piano | synth | both
    pdf: bool = True,
) -> RenderResult:
    result = RenderResult()
    stem = score_path.stem
    title = (score.metadata.title if score.metadata else None) or stem

    expanded = None
    if voice in ("piano", "synth", "both"):
        expanded, warning = expand_repeats(score)
        if warning:
            result.warnings.append(warning)

    if voice in ("piano", "both"):
        result.outputs.append(_render_piano(expanded, outdir / f"{stem}.piano.wav", deps))
    if voice in ("synth", "both"):
        result.outputs.extend(_render_synth(expanded, title, outdir, stem, deps, result.warnings))
    if pdf:
        result.outputs.append(_render_pdf(score_path, outdir / f"{stem}.pdf", deps))
    return result


def _check_produced(path: Path, what: str) -> Path:
    if not path.exists():
        raise ToolRunError(f"{what} reported success but produced no {path.name}")
    return path


def _render_piano(expanded: stream.Score, wav: Path, deps: Deps) -> Path:
    fluidsynth = depsmod.require_fluidsynth(deps)
    soundfont = depsmod.soundfont_path(deps)
    with tempfile.TemporaryDirectory(prefix="chantey-midi-") as tmp:
        midi = Path(tmp) / "score.mid"
        expanded.write("midi", fp=str(midi))
        argv = depsmod.fluidsynth_argv(fluidsynth, soundfont, midi, wav)
        depsmod.run_checked(deps, argv, what="fluidsynth")
    return _check_produced(wav, "fluidsynth")


def _render_synth(
    expanded: stream.Score, title: str, outdir: Path, stem: str, deps: Deps, warnings: list[str]
) -> list[Path]:
    hornpipe = depsmod.require_hornpipe(deps)
    converted = convert(expanded, title)
    warnings.extend(converted.warnings)
    toml = outdir / f"{stem}.synth.toml"
    toml.write_text(converted.toml_text, encoding="utf-8")
    wav = outdir / f"{stem}.synth.wav"
    depsmod.run_checked(deps, depsmod.hornpipe_argv(hornpipe, toml, wav), what="hornpipe")
    return [toml, _check_produced(wav, "hornpipe")]


def _render_pdf(score_path: Path, pdf: Path, deps: Deps) -> Path:
    mscore = depsmod.require_mscore(deps)
    argv = depsmod.mscore_argv(mscore, score_path, pdf)
    depsmod.run_checked(deps, argv, env=depsmod.mscore_env(deps), what="mscore")
    return _check_produced(pdf, "mscore")
