"""The sfizz piano-voice engine: one SFZ instrument per part (v0.3).

Three pieces, each testable without sfizz:

1. **Mapping** — every part gets an SFZ from `CHANTEY_SFZ_MAP` (by MusicXML
   part-name: exact match first, then case-insensitive) or else from `--sfz`.
   A part with neither is an error naming it; there is no fallback to
   FluidSynth.
2. **Per-part MIDI** — the score's MIDI is written once, exactly as the
   FluidSynth path writes it (same repeat expansion), then split: each part's
   file is the conductor track (tempo map, meters, keys) plus that part's
   track. A part's events are therefore byte-identical to the full render's,
   except at the end (v0.4): a text event at the score's end plus a tail,
   with every End-of-Track moved there. sfizz_render runs with --use-eot,
   so that tick, not sfizz's idle-voice test, decides where a stem ends.
3. **Mix** — the per-part WAVs are summed at unity gain, padded to the
   longest part; a sum above -0.1 dBFS is scaled down by its peak.
"""

from __future__ import annotations

import math
import operator
import os
import sys
import wave
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from music21 import midi, stream

from .errors import ChanteyError

SFZ_MAP_ENV = "CHANTEY_SFZ_MAP"
CEILING_DBFS = -0.1
FULL_SCALE = 32767
# Seconds of audio after the score's end, so the last note's release is not
# cut by --use-eot. The longest ampeg_release in the libraries it was sized
# against (VPO, Salamander) is 1 s.
EOT_TAIL_S = 2.0
DEFAULT_TEMPO_US = 500_000  # MIDI's default: 120 bpm
TAIL_TEXT = b"chantey: end of score + tail"


@dataclass(frozen=True)
class PartSfz:
    label: str  # the part-name, or "part <n>" when the part has none
    sfz: str  # absolute path


def parse_sfz_map(raw: str) -> dict[str, str]:
    """`"<part-name>=<path>;<part-name>=<path>"` → {name: path}. Whitespace
    around names and paths is stripped; empty segments are skipped."""
    mapping: dict[str, str] = {}
    for segment in raw.split(";"):
        if not segment.strip():
            continue
        name, sep, path = segment.partition("=")
        name, path = name.strip(), path.strip()
        if not sep or not name or not path:
            raise ChanteyError(f"{SFZ_MAP_ENV}: expected '<part-name>=<path>', got {segment!r}")
        if name in mapping:
            raise ChanteyError(f"{SFZ_MAP_ENV}: part {name!r} is mapped twice")
        mapping[name] = path
    return mapping


def _map_key(mapping: dict[str, object], name: str, source: str = SFZ_MAP_ENV) -> str | None:
    """The map key a part-name selects: exact match, else the single
    case-insensitive match (two or more is an error). The expression
    sidecar (v0.5) matches part-names the same way."""
    if name in mapping:
        return name
    folded = [key for key in mapping if key.casefold() == name.casefold()]
    if len(folded) > 1:
        keys = ", ".join(repr(k) for k in folded)
        raise ChanteyError(f"{source}: part {name!r} matches {keys} case-insensitively")
    return folded[0] if folded else None


def resolve_parts(
    part_names: list[str | None], mapping: dict[str, str], default_sfz: str | None
) -> tuple[list[PartSfz], list[str]]:
    """One PartSfz per part, in score order, plus warnings for map entries
    that matched no part. The map wins per part; `default_sfz` fills the
    rest; any part left without an SFZ is an error naming every such part."""
    resolved: list[PartSfz] = []
    unmapped: list[str] = []
    used: set[str] = set()
    for i, name in enumerate(part_names):
        label = name or f"part {i + 1}"
        key = _map_key(mapping, name) if name else None
        if key is not None:
            used.add(key)
        chosen = mapping[key] if key is not None else default_sfz
        if chosen is None:
            unmapped.append(repr(label) if name else f"{label} (no part-name)")
            continue
        resolved.append(PartSfz(label, os.path.abspath(os.path.expanduser(chosen))))
    if unmapped:
        raise ChanteyError(
            f"no SFZ for {', '.join(unmapped)} — map it in {SFZ_MAP_ENV} or give --sfz"
        )
    missing = [f"{p.label}={p.sfz}" for p in resolved if not Path(p.sfz).is_file()]
    if missing:
        raise ChanteyError("SFZ file not found: " + ", ".join(dict.fromkeys(missing)))
    warnings = [
        f"{SFZ_MAP_ENV} entry {key!r} matches no part" for key in mapping if key not in used
    ]
    return resolved, warnings


def mapping_label(parts: list[PartSfz]) -> str:
    """`Trumpet=/lib/Trumpet.sfz, Tuba=/lib/Tuba.sfz` — each label once."""
    return ", ".join(dict.fromkeys(f"{p.label}={p.sfz}" for p in parts))


@dataclass(frozen=True)
class PartMidis:
    paths: list[Path]  # one per part, in score order
    seconds: float  # where every file's End-of-Track sits: score end + tail


def _tempo_us(event: midi.MidiEvent) -> int:
    return int.from_bytes(bytes(event.data[:3]), "big")


def _absolute(track: midi.MidiTrack) -> list[tuple[int, midi.MidiEvent]]:
    """(tick, event) for every non-delta event in the track."""
    tick, out = 0, []
    for event in track.events:
        if isinstance(event, midi.DeltaTime):
            tick += event.time
        else:
            out.append((tick, event))
    return out


def _end_track_at(track: midi.MidiTrack, tick: int, text: bytes | None = None) -> None:
    """Re-time the track's End-of-Track (its last event) to `tick`, preceded
    at that tick by a text meta event when `text` is given."""
    events = track.events
    if (
        len(events) < 2
        or not isinstance(events[-2], midi.DeltaTime)
        or events[-1].type != midi.MetaEvents.END_OF_TRACK
    ):
        raise ChanteyError("MIDI export: a track does not end with End-of-Track")
    before = sum(e.time for e in events[:-2] if isinstance(e, midi.DeltaTime))
    if tick < before:
        raise ChanteyError("MIDI export: an event lies past the score's end")
    events[-2].time = tick - before
    if text is not None:
        marker = midi.MidiEvent(track, type=midi.MetaEvents.TEXT_EVENT)
        marker.data = text
        events[-1:-1] = [marker, midi.DeltaTime(track, time=0)]


def _seconds_at(tempos: list[tuple[int, int]], tick: int, tpq: int) -> float:
    """Seconds from tick 0 to `tick` under the (tick, µs-per-quarter) map."""
    seconds, at, us = 0.0, 0, DEFAULT_TEMPO_US
    for change, new_us in tempos:
        if change >= tick:
            break
        seconds += (change - at) * us / tpq / 1e6
        at, us = change, new_us
    return seconds + (tick - at) * us / tpq / 1e6


def write_part_midis(
    expanded: stream.Score,
    workdir: Path,
    tail: float = EOT_TAIL_S,
    edit: Callable[[list[midi.MidiTrack], int], None] | None = None,
) -> PartMidis:
    """Write the full-score MIDI, then one file per part holding the
    conductor track plus that part's track, with every End-of-Track at the
    score's end plus `tail` seconds (at the tempo in force there). `edit`,
    when given, gets the part tracks and ticks-per-quarter after the ends
    are placed and before the files are written (the expression merge)."""
    full = workdir / "score.mid"
    expanded.write("midi", fp=str(full))
    source = midi.MidiFile()
    source.open(str(full))
    source.read()
    source.close()
    n_parts = len(expanded.parts)
    if len(source.tracks) != n_parts + 1:
        raise ChanteyError(
            f"MIDI export has {len(source.tracks)} tracks for {n_parts} parts "
            "(expected a conductor track plus one per part)"
        )
    tpq = source.ticksPerQuarterNote
    timed = [_absolute(track) for track in source.tracks]
    # The score's end is its notated end (trailing rests count), never short
    # of the last event the export holds.
    last_event = max(
        (t for events in timed for t, e in events if e.type != midi.MetaEvents.END_OF_TRACK),
        default=0,
    )
    end = max(round(float(expanded.highestTime) * tpq), last_event)
    tempos = sorted((t, _tempo_us(e)) for t, e in timed[0] if e.type == midi.MetaEvents.SET_TEMPO)
    us_at_end = next((us for t, us in reversed(tempos) if t <= end), DEFAULT_TEMPO_US)
    eot = end + round(tail * 1e6 / us_at_end * tpq)
    # sfizz_render 1.2.3 --use-eot ends at the last event *before* the final
    # End-of-Track and ignores the EOT's own delta, so an inert text event
    # carries the tail; every EOT moves to the same tick.
    _end_track_at(source.tracks[0], eot, TAIL_TEXT)
    for track in source.tracks[1:]:
        _end_track_at(track, eot)
    if edit is not None:
        edit(source.tracks[1:], tpq)

    conductor = source.tracks[0]
    paths = []
    for i, track in enumerate(source.tracks[1:], start=1):
        out = midi.MidiFile()
        out.format = source.format
        out.ticksPerQuarterNote = tpq
        out.tracks = [conductor, track]
        path = workdir / f"part{i:02d}.mid"
        out.open(str(path), "wb")
        out.write()
        out.close()
        paths.append(path)
    return PartMidis(paths, _seconds_at(tempos, eot, tpq))


@dataclass
class MixResult:
    frames: int
    scale: float | None  # None when the sum was within the ceiling
    peak_dbfs: float | None  # the unscaled sum's peak, when it was scaled


def _read_pcm16(path: Path) -> tuple[tuple[int, int], array]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ChanteyError(
                f"{path.name}: {8 * w.getsampwidth()}-bit audio; chantey mixes 16-bit PCM only"
            )
        params = (w.getnchannels(), w.getframerate())
        samples = array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    return params, samples


def mix_wavs(inputs: list[Path], out: Path) -> MixResult:
    """Sum 16-bit WAVs at unity gain; the result is as long as the longest.
    A peak above CEILING_DBFS scales the whole mix to land exactly on it."""
    params = None
    total = array("i")
    for path in inputs:
        these, samples = _read_pcm16(path)
        if params is None:
            params = these
        elif these != params:
            raise ChanteyError(
                f"{path.name}: {these[0]} ch at {these[1]} Hz, but the first part "
                f"has {params[0]} ch at {params[1]} Hz"
            )
        if len(samples) > len(total):
            total.extend(array("i", bytes(4 * (len(samples) - len(total)))))
        total[: len(samples)] = array("i", map(operator.add, total[: len(samples)], samples))
    if params is None:
        raise ChanteyError("no part audio to mix")
    channels, rate = params

    ceiling = FULL_SCALE * 10 ** (CEILING_DBFS / 20)
    peak = max(map(abs, total), default=0)
    scale = peak_dbfs = None
    if peak > ceiling:
        scale = ceiling / peak
        peak_dbfs = 20 * math.log10(peak / FULL_SCALE)
        pcm = array("h", (round(s * scale) for s in total))
    else:
        pcm = array("h", total)
    if sys.byteorder == "big":
        pcm.byteswap()
    with wave.open(str(out), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return MixResult(frames=len(total) // channels, scale=scale, peak_dbfs=peak_dbfs)
