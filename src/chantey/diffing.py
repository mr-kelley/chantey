"""Measure-level score comparison.

The changed-measure list is chantey's lingua franca: the role quotes it back
to the operator after an edit, and `compare` uses it to drive highlighting —
no hand-maintained lists (skeleton lesson).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from music21 import stream


@dataclass
class DiffReport:
    changed: list[int]  # sorted union across parts
    per_part: dict[str, list[int]] = field(default_factory=dict)
    part_mismatch: bool = False  # differing part counts

    @property
    def identical(self) -> bool:
        return not self.changed and not self.part_mismatch


def _measure_signatures(part: stream.Part) -> dict[int, tuple]:
    sigs: dict[int, tuple] = {}
    for meas in part.getElementsByClass(stream.Measure):
        events = []
        for el in meas.notes:
            pitches = tuple(sorted(p.midi for p in el.pitches))
            events.append(
                (round(float(el.offset), 4), round(float(el.duration.quarterLength), 4), pitches)
            )
        sigs[meas.number] = tuple(sorted(events))
    return sigs


def diff_scores(a: stream.Score, b: stream.Score) -> DiffReport:
    parts_a, parts_b = list(a.parts), list(b.parts)
    report = DiffReport(changed=[])
    if len(parts_a) != len(parts_b):
        report.part_mismatch = True
    changed: set[int] = set()
    # unequal part counts already flagged; compare the common prefix
    for i, (pa, pb) in enumerate(zip(parts_a, parts_b, strict=False)):
        label = pa.partName or f"part{i + 1}"
        sa, sb = _measure_signatures(pa), _measure_signatures(pb)
        diffs = sorted(n for n in set(sa) | set(sb) if sa.get(n) != sb.get(n))
        if diffs:
            report.per_part[f"{label} ({i + 1})"] = diffs
            changed.update(diffs)
    report.changed = sorted(changed)
    return report
