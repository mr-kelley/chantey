"""Render a canonical score to audio (both voices) and PDF.

The repeat-parity guarantee lives here: `expand_repeats` is called exactly
once and the SAME expanded score feeds the MIDI/FluidSynth path and the
hornpipe converter. The PDF renders from the original file, keeping repeat
notation on the page.

The piano voice has two engines: FluidSynth with one SoundFont (the
default), or sfizz with one SFZ per part (`sfzengine`). Both start from the
same MIDI export of the same expanded score, into which an expression
sidecar's curves are merged when one is given (`expression`).
"""

from __future__ import annotations

import signal
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from music21 import midi, stream

from . import deps as depsmod
from . import expression, sfzengine
from .convert import convert, expand_repeats
from .deps import Deps
from .errors import OutputCapError, Stopped, ToolRunError


@dataclass
class RenderResult:
    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # per-output notes for the CLI's "Wrote" line, one bracket each (the
    # piano voice's engine or fx setting; the expression sidecar)
    notes: dict[Path, list[str]] = field(default_factory=dict)


def render(
    score: stream.Score,
    score_path: Path,
    outdir: Path,
    deps: Deps,
    *,
    voice: str = "both",  # piano | synth | both
    pdf: bool = True,
    fx: str | None = None,  # piano voice, fluidsynth only: dry | light | default
    engine: str = "fluidsynth",  # piano voice only: fluidsynth | sfizz
    sfz: str | None = None,  # sfizz: the SFZ for parts CHANTEY_SFZ_MAP leaves unmapped
    sidecar: expression.Sidecar | None = None,  # piano voice only: controller curves
) -> RenderResult:
    result = RenderResult()
    stem = score_path.stem
    title = (score.metadata.bestTitle if score.metadata else None) or stem

    expanded = None
    if voice in ("piano", "synth", "both"):
        expanded, warning = expand_repeats(score)
        if warning:
            result.warnings.append(warning)
    # checked against the score before anything is written, whatever the voice
    plan = expression.resolve(sidecar, expanded) if sidecar is not None else None
    if plan is not None and voice in ("piano", "both") and engine != "sfizz":
        expression.refuse_keyswitches(plan)

    if voice in ("piano", "both"):
        wav = outdir / f"{stem}.piano.wav"
        if engine == "sfizz":
            note, events = _render_piano_sfizz(expanded, wav, deps, sfz, fx, plan, result.warnings)
        else:
            fx = fx or depsmod.DEFAULT_FX
            extra = depsmod.fluidsynth_extra_opts(deps)
            events = _render_piano(expanded, wav, deps, fx, extra, plan)
            note = depsmod.fx_label(fx, extra)
        result.notes[wav] = [note]
        if plan is not None:
            result.notes[wav].append(expression.label(plan, events))
        result.outputs.append(wav)
    if voice in ("synth", "both"):
        outputs = _render_synth(expanded, title, outdir, stem, deps, result.warnings)
        if plan is not None:
            result.notes[outputs[-1]] = ["--expression ignored (piano voice only)"]
        result.outputs.extend(outputs)
    if pdf:
        result.outputs.append(_render_pdf(score_path, outdir / f"{stem}.pdf", deps))
    return result


_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextmanager
def _stop_on_signals():
    """SIGINT/SIGTERM/SIGHUP raise Stopped for the duration, so `with` blocks
    unwind: subprocess.run kills its child and temporary directories are
    removed. Outside the main thread (no handlers allowed) this is a no-op."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, _frame):
        raise Stopped(signum, signal.Signals(signum).name)

    previous = {s: signal.signal(s, handler) for s in _STOP_SIGNALS}
    try:
        yield
    finally:
        for s, old in previous.items():
            signal.signal(s, old)


def _check_produced(path: Path, what: str) -> Path:
    if not path.exists():
        raise ToolRunError(f"{what} reported success but produced no {path.name}")
    return path


def _render_piano(
    expanded: stream.Score,
    wav: Path,
    deps: Deps,
    fx: str,
    extra: list[str],
    plan: expression.Plan | None,
) -> expression.Merged:
    """Render the one score MIDI through FluidSynth; returns what the
    expression merge did to it."""
    fluidsynth = depsmod.require_fluidsynth(deps)
    soundfont = depsmod.soundfont_path(deps)
    events = expression.Merged()
    with tempfile.TemporaryDirectory(prefix="chantey-midi-") as tmp:
        path = Path(tmp) / "score.mid"
        expanded.write("midi", fp=str(path))
        if plan is not None and plan.n_parts:
            events = _merge_into_file(path, plan)
        argv = depsmod.fluidsynth_argv(fluidsynth, soundfont, path, wav, fx=fx, extra=extra)
        depsmod.run_checked(deps, argv, what="fluidsynth")
    _check_produced(wav, "fluidsynth")
    return events


def _merge_into_file(path: Path, plan: expression.Plan) -> expression.Merged:
    """Merge the plan into the single-file export, in place (FluidSynth)."""
    mf = midi.MidiFile()
    mf.open(str(path))
    mf.read()
    mf.close()
    events = expression.merge(plan, mf.tracks[1:], mf.ticksPerQuarterNote, own_files=False)
    mf.open(str(path), "wb")
    mf.write()
    mf.close()
    return events


def _render_piano_sfizz(
    expanded: stream.Score,
    wav: Path,
    deps: Deps,
    sfz: str | None,
    fx: str | None,
    plan: expression.Plan | None,
    warnings: list[str],
) -> tuple[str, expression.Merged]:
    """Render each part through its own SFZ and mix; returns the output-line
    note recording the engine, the mapping and anything ignored or scaled,
    and what the expression merge did to the part files."""
    mapping = sfzengine.parse_sfz_map(deps.environ.get(sfzengine.SFZ_MAP_ENV, ""))
    names = [part.partName for part in expanded.parts]
    parts, map_warnings = sfzengine.resolve_parts(names, mapping, sfz)
    warnings.extend(map_warnings)
    sfizz = depsmod.require_sfizz(deps)
    tmpdir = depsmod.work_tmpdir(deps)
    merged: list[expression.Merged] = []

    def merge(tracks, tpq):
        merged.append(expression.merge(plan, tracks, tpq, own_files=True))

    with (
        _stop_on_signals(),
        tempfile.TemporaryDirectory(prefix="chantey-sfizz-", dir=tmpdir) as tmp,
    ):
        workdir = Path(tmp)
        edit = merge if plan is not None and plan.n_parts else None
        midis = sfzengine.write_part_midis(expanded, workdir, edit=edit)
        cap = depsmod.sfizz_output_cap(midis.seconds)
        stems = []
        for i, (part, midi) in enumerate(zip(parts, midis.paths, strict=True), start=1):
            stem_wav = workdir / f"part{i:02d}.wav"
            argv = depsmod.sfizz_argv(sfizz, part.sfz, midi, stem_wav)
            what = f"sfizz_render ({part.label})"
            try:
                depsmod.run_checked(deps, argv, what=what, fsize_limit=cap, output=stem_wav)
            except OutputCapError as exc:
                raise ToolRunError(
                    f"{exc}: the score plus its {sfzengine.EOT_TAIL_S:g} s tail is "
                    f"{midis.seconds:.1f} s, but the engine kept rendering past it. The "
                    "stem is wrong (most likely an sfizz voice that never released), so "
                    "nothing was written; the cap only protects the disk"
                ) from None
            stems.append(_check_produced(stem_wav, what))
        mixed = sfzengine.mix_wavs(stems, wav)

    note = (
        f"engine: sfizz; sfz: {sfzengine.mapping_label(parts)}; "
        f"eot: +{sfzengine.EOT_TAIL_S:.1f} s; cap: {cap / 1e6:.1f} MB per part"
    )
    if fx is not None:
        note += f"; --fx {fx} ignored (FluidSynth only)"
    if deps.environ.get(depsmod.FLUIDSYNTH_OPTS_ENV):
        note += f"; {depsmod.FLUIDSYNTH_OPTS_ENV} ignored (FluidSynth only)"
    if mixed.scale is not None:
        note += (
            f"; mix peaked at {mixed.peak_dbfs:+.2f} dBFS, scaled by {mixed.scale:.4f} "
            f"to {sfzengine.CEILING_DBFS} dBFS"
        )
    return note, sum(merged, expression.Merged())


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
