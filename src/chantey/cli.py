"""Command-line interface: info / ingest / render / diff / compare.

Thin dispatch; all fallible work raises ChanteyError → clean stderr + exit 1
(argparse usage errors exit 2). Non-clobbering everywhere (--force)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, comparepage, diffing, renderer
from . import ingest as ingestmod
from .deps import Deps
from .errors import ChanteyError, OutputExistsError
from .scoreio import format_info, load_score, score_info


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chantey",
        description="Sheet-music pipeline: canonical MusicXML → sound, notation, and comparisons.",
    )
    parser.add_argument("--version", action="version", version=f"chantey {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="summarize a score's structure")
    p.add_argument("score", type=Path)

    p = sub.add_parser("ingest", help="produce the canonical .musicxml from PDF or MusicXML")
    p.add_argument("source", type=Path)
    p.add_argument("-o", "--output", type=Path, help="default: <source>.musicxml")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("render", help="render audio (piano + synth voices) and PDF")
    p.add_argument("score", type=Path)
    p.add_argument("--voice", choices=("piano", "synth", "both"), default="both")
    p.add_argument("--no-pdf", action="store_true")
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
    print(format_info(score_info(load_score(args.score))))
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
    result = renderer.render(score, args.score, outdir, deps, voice=args.voice, pdf=not args.no_pdf)
    _warn(result.warnings)
    for path in result.outputs:
        print(f"Wrote {path}")
    return 0


def _cmd_diff(args, deps: Deps) -> int:
    report = diffing.diff_scores(load_score(args.original), load_score(args.revised))
    if report.part_mismatch:
        print("part counts differ between the two scores", file=sys.stderr)
    if report.identical:
        print("identical scores — no differing measures")
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
    title = (revised.metadata.title if revised.metadata else None) or args.revised.stem
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
    except ChanteyError as exc:
        print(f"chantey: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
