"""Ingest: produce the canonical .musicxml from a PDF (via Audiveris OMR)
or from born-digital MusicXML (.mxl/.musicxml — always preferred: lossless,
no proofing pass)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from music21 import stream

from . import deps as depsmod
from .deps import Deps
from .errors import IngestError, ScoreLoadError
from .scoreio import ensure_title, load_score, write_canonical

# Documented OMR loss classes from the walking skeleton (2026-09-15): pinned
# here so the reminder the operator sees stays honest and specific.
OMR_LOSS_NOTE = (
    "OMR output needs a proofing pass (render it back and compare by eye and "
    "ear). Known loss classes: tempo/expression text, fingerings (sometimes "
    "misread as dynamics), articulation glyphs."
)


@dataclass
class IngestResult:
    score: stream.Score
    out: Path
    used_omr: bool
    warnings: list[str] = field(default_factory=list)


def ingest(source: Path, out: Path, deps: Deps) -> IngestResult:
    source = Path(source)
    used_omr = False
    warnings: list[str] = []
    if source.suffix.lower() == ".pdf":
        used_omr = True
        score = _omr(source, deps)
        warnings.append(OMR_LOSS_NOTE)
    elif source.suffix.lower() in (".mxl", ".musicxml", ".xml"):
        score = load_score(source)
    else:
        raise IngestError(f"unsupported input type {source.suffix!r} (want .pdf/.mxl/.musicxml)")
    ensure_title(score, source.stem)
    write_canonical(score, out)
    return IngestResult(score=score, out=out, used_omr=used_omr, warnings=warnings)


def _omr(pdf: Path, deps: Deps) -> stream.Score:
    flatpak = depsmod.require_flatpak(deps)
    workdir = pdf.resolve().parent
    with tempfile.TemporaryDirectory(prefix="chantey-omr-") as tmp:
        outdir = Path(tmp)
        argv = depsmod.audiveris_argv(flatpak, workdir, pdf.resolve(), outdir)
        depsmod.run_checked(deps, argv, what="Audiveris OMR")
        exports = sorted(outdir.rglob("*.mxl"))
        if not exports:
            raise IngestError(
                "Audiveris produced no MusicXML export — the PDF may not contain "
                "recognizable engraved music (photos and handwriting transcribe poorly)"
            )
        try:
            return load_score(exports[0])
        except ScoreLoadError as e:
            raise IngestError(f"OMR export unparseable: {e}") from e
