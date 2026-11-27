"""Command-line interface: info / ingest / render / diff / compare.

Thin dispatch; all fallible work raises ChanteyError → clean stderr + exit 1
(argparse usage errors exit 2). Non-clobbering everywhere (--force)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, comparepage, diffing, expression, renderer, sfzinfo
from . import ingest as ingestmod
from .deps import (
    FLUIDSYNTH_OPTS_ENV,
    FX_PRESETS,
    SFIZZ_BLOCKSIZE,
    SFIZZ_QUALITY,
    SFIZZ_RATE,
    TMPDIR_ENV,
    Deps,
)
from .errors import ChanteyError, OutputExistsError, Stopped
from .scoreio import format_info, load_score, score_info
from .sfzengine import EOT_TAIL_S, SFZ_MAP_ENV


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chantey",
        description="Sheet-music pipeline: canonical MusicXML → sound, notation, and comparisons.",
    )
    parser.add_argument("--version", action="version", version=f"chantey {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="summarize a score's structure")
    p.add_argument("score", type=Path)
    p.add_argument(
        "--sfz",
        metavar="PATH",
        help=(
            "also print the keyswitch map (sw_lokey-sw_hikey, sw_default, each "
            "sw_last with its sw_label) of each part's SFZ: the part's "
            f"${SFZ_MAP_ENV} entry, else this file. Without --sfz, parts "
            f"${SFZ_MAP_ENV} maps are still shown. An SFZ that cannot be read is a "
            "warning"
        ),
    )

    p = sub.add_parser("ingest", help="produce the canonical .musicxml from PDF or MusicXML")
    p.add_argument("source", type=Path)
    p.add_argument("-o", "--output", type=Path, help="default: <source>.musicxml")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("render", help="render audio (piano + synth voices) and PDF")
    p.add_argument("score", type=Path)
    p.add_argument("--voice", choices=("piano", "synth", "both"), default="both")
    p.add_argument("--no-pdf", action="store_true")
    p.add_argument(
        "--engine",
        choices=("fluidsynth", "sfizz"),
        default="fluidsynth",
        help=(
            "piano-voice engine: fluidsynth = one GM SoundFont (default); sfizz = one "
            "SFZ instrument per part, rendered with sfizz_render "
            f"(-s {SFIZZ_RATE} -b {SFIZZ_BLOCKSIZE} -q {SFIZZ_QUALITY} --use-eot) and "
            "summed at unity gain. Each part ends at the score's end + "
            f"{EOT_TAIL_S:g} s; a render that writes more than twice that is stopped "
            f"and is an error. Working files go under ${TMPDIR_ENV}, else $TMPDIR"
        ),
    )
    p.add_argument(
        "--sfz",
        metavar="PATH",
        help=(
            f"sfizz engine: the SFZ for every part ${SFZ_MAP_ENV} does not map. "
            f'${SFZ_MAP_ENV}="<part-name>=<path>;..." maps parts by MusicXML '
            "part-name (exact, then case-insensitive) and wins per part; a part "
            "with no SFZ is an error"
        ),
    )
    p.add_argument(
        "--fx",
        choices=tuple(FX_PRESETS),
        default=None,
        help=(
            "piano-voice FluidSynth effects: dry = chorus and reverb off (default); "
            "light = chorus off, small short reverb; default = FluidSynth's own "
            f"defaults. Extra FluidSynth arguments from ${FLUIDSYNTH_OPTS_ENV} "
            "(split shell-style) are appended after the preset, unchecked. "
            "Ignored under --engine sfizz (the output line says so)"
        ),
    )
    p.add_argument(
        "--expression",
        metavar="FILE",
        type=Path,
        help=(
            "piano voice, either engine: merge per-part controller curves (CC 0-119, "
            "pitch bend), timing offsets in ms that move each note (time curves) and, "
            "under sfizz only, keyswitch notes from a JSON sidecar "
            "into each part's MIDI before the engine runs. Parts are matched by "
            "part-name as for "
            f"${SFZ_MAP_ENV}; offsets are quarter lengths on the rendered (repeats "
            "expanded) timeline, which 'chantey info' prints per part. Any error in "
            "the sidecar stops the render before anything is written. Ignored by the "
            "synth voice (the output line says so)"
        ),
    )
    p.add_argument("-o", "--outdir", type=Path, help="default: beside the score")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("diff", help="list measures that differ between two versions")
    p.add_argument("original", type=Path)
    p.add_argument("revised", type=Path)
    p.add_argument("-v", "--verbose", action="store_true", help="per-part detail")

    p = sub.add_parser("compare", help="side-by-side comparison page (local HTML)")
    p.add_argument("original", type=Path)
    p.add_argument("revised", type=Path)
    p.add_argument(
        "-o", "--output", type=Path, help="default: compare.html beside the revised score"
    )
    p.add_argument("--force", action="store_true")

    return parser


def _refuse_clobber(paths: list[Path], force: bool) -> None:
    if force:
        return
    existing = [str(p) for p in paths if p.exists()]
    if existing:
        raise OutputExistsError("output exists (use --force to overwrite): " + ", ".join(existing))


def _warn(warnings: list[str]) -> None:
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)


def _cmd_info(args, deps: Deps) -> int:
    score = load_score(args.score)
    info = score_info(score)
    raw_map = deps.environ.get(SFZ_MAP_ENV, "")
    if raw_map or args.sfz is not None:
        names = [part.partName for part in score.parts]
        info.part_notes, warnings = sfzinfo.part_lines(names, raw_map, args.sfz)
        info.warnings.extend(warnings)
    print(format_info(info))
    return 0


def _cmd_ingest(args, deps: Deps) -> int:
    out = args.output or args.source.with_suffix(".musicxml")
    _refuse_clobber([out], args.force)
    result = ingestmod.ingest(args.source, out, deps)
    _warn(result.warnings)
    print(f"Canonical score written: {out}" + ("  (via Audiveris OMR)" if result.used_omr else ""))
    print(format_info(score_info(result.score)))
    return 0


def _cmd_render(args, deps: Deps) -> int:
    if args.sfz is not None and args.engine != "sfizz":
        raise ChanteyError("--sfz is an sfizz setting: add --engine sfizz")
    sidecar = expression.load(args.expression) if args.expression is not None else None
    score = load_score(args.score)
    outdir = args.outdir or args.score.parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = args.score.stem
    targets = []
    if args.voice in ("piano", "both"):
        targets.append(outdir / f"{stem}.piano.wav")
    if args.voice in ("synth", "both"):
        targets += [outdir / f"{stem}.synth.toml", outdir / f"{stem}.synth.wav"]
    if not args.no_pdf:
        targets.append(outdir / f"{stem}.pdf")
    _refuse_clobber(targets, args.force)
    result = renderer.render(
        score,
        args.score,
        outdir,
        deps,
        voice=args.voice,
        pdf=not args.no_pdf,
        fx=args.fx,
        engine=args.engine,
        sfz=args.sfz,
        sidecar=sidecar,
    )
    _warn(result.warnings)
    for path in result.outputs:
        notes = result.notes.get(path, [])
        print(f"Wrote {path}" + "".join(f"  [{note}]" for note in notes if note))
    return 0


def _cmd_diff(args, deps: Deps) -> int:
    report = diffing.diff_scores(load_score(args.original), load_score(args.revised))
    if report.part_mismatch:
        print("part counts differ between the two scores", file=sys.stderr)
    if report.identical:
        print("identical scores — no differing measures")
        return 0
    if not report.changed:  # part counts differ; the shared parts match
        print("no differing measures in the parts both scores share")
        return 0
    print("changed measures: " + ", ".join(str(n) for n in report.changed))
    if args.verbose:
        for label, measures in report.per_part.items():
            print(f"  {label}: " + ", ".join(str(n) for n in measures))
    return 0


def _cmd_compare(args, deps: Deps) -> int:
    original, revised = load_score(args.original), load_score(args.revised)
    report = diffing.diff_scores(original, revised)
    if report.identical:
        print("identical scores — no comparison page written")
        return 0
    out = args.output or args.revised.parent / "compare.html"
    _refuse_clobber([out], args.force)
    title = (revised.metadata.bestTitle if revised.metadata else None) or args.revised.stem
    audio = comparepage.find_audio(args.original, "Original") + comparepage.find_audio(
        args.revised, "Revised"
    )
    if not audio:
        print(
            "WARNING: no rendered audio found beside the scores — run "
            "'chantey render' first for playable comparisons",
            file=sys.stderr,
        )
    html = comparepage.build_page(args.original, args.revised, report.changed, audio, title)
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}  (changed measures: {', '.join(str(n) for n in report.changed)})")
    return 0


_DISPATCH = {
    "info": _cmd_info,
    "ingest": _cmd_ingest,
    "render": _cmd_render,
    "diff": _cmd_diff,
    "compare": _cmd_compare,
}


def main(argv: list[str] | None = None, *, deps: Deps | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _DISPATCH[args.command](args, deps or Deps())
    except Stopped as exc:
        print(f"chantey: {exc}", file=sys.stderr)
        return 128 + exc.signum
    except ChanteyError as exc:
        print(f"chantey: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
