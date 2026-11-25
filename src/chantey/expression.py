"""The expression sidecar (v0.5): per-part controller curves merged into
the piano voice's MIDI at render time.

MusicXML carries no controller data and music21 cannot place controller
events in a Stream, so the curves arrive as a JSON sidecar written by the
caller and enter after the MIDI export. Three steps:

1. **Load** — `load()` parses and checks the file on its own: shape, curve
   kinds, value ranges, sorted offsets, one curve per controller per part.
2. **Resolve** — `resolve()` checks it against the expanded score (every
   part-name matches a part, every offset lies within the part plus the
   render tail) and samples every curve on the quarter-length timeline.
   Both run before anything is written.
3. **Merge** — `merge()` turns the samples into MIDI events in each part's
   track, on the channels that track's notes use, interleaved with the
   notes. Program changes, tempi and every existing event keep their ticks,
   bytes and order, except the note events a time curve moves; an empty
   plan leaves the tracks untouched.

A curve names its kind by exactly one key (`cc`, `bend`, and since v0.6
`keyswitch`, since v0.7 `time`), so a later kind is a new key and older
files keep their meaning. A keyswitch curve is not a controller: each point
is a short note-on/note-off on a key outside the part's playing range, which
an SFZ's `sw_last` regions read as "play this articulation from here", so it
renders under sfizz only. A time curve adds no events either: it moves the
part's own note-ons and note-offs by its milliseconds at each one's written
offset.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from music21 import midi, stream

from .convert import tempo_segments
from .errors import ChanteyError
from .sfzengine import EOT_TAIL_S, _absolute, _map_key
from .sfzinfo import key_label

# linear curves: one sample per step, the coarser of these two
STEP_QL = 1 / 32
STEP_S = 0.010
DEFAULT_BEND_RANGE = 200  # cents either way: sfizz's and FluidSynth's default
BEND_CENTER = 8192
BEND_MAX = 16383
CC_MAX_NUMBER = 119  # 120-127 are channel-mode messages, not controllers
INTERPS = ("linear", "hold")
CURVE_KINDS = ("cc", "bend", "keyswitch", "time")
KEYSWITCH_VELOCITY = 100  # the switch note's default velocity
KEYSWITCH_TICKS = 1  # the switch note's length: sfizz's sw_last reads the note-on
DEFAULT_TIME_MAX = 60  # ms either way a time curve may move a note
TIME_MAX_LIMIT = 1000  # ms: half the render tail, so no shift carries a note past it

_TOP_KEYS = {"parts"}
_PART_KEYS = {"curves", "bend_range", "time_max"}
_CURVE_KEYS = {"interp", "points", "velocity", *CURVE_KINDS}


@dataclass(frozen=True)
class Curve:
    kind: str  # "cc" | "bend" | "keyswitch" | "time"
    number: int | None  # the controller, for "cc"
    interp: str  # "linear" | "hold"
    points: tuple[tuple[float, float], ...]  # (quarter-length offset, value)
    velocity: int | None = None  # the switch note's, for "keyswitch"

    @property
    def label(self) -> str:
        if self.kind == "cc":
            return f"CC{self.number}"
        return {"bend": "pitch bend"}.get(self.kind, self.kind)


@dataclass(frozen=True)
class PartCurves:
    curves: tuple[Curve, ...]
    bend_range: float


@dataclass(frozen=True)
class Sidecar:
    path: Path
    parts: dict[str, PartCurves]  # by part-name, in file order


@dataclass(frozen=True)
class PartPlan:
    label: str
    # per curve, in file order: (curve, [(quarter-length offset, MIDI value)])
    samples: tuple[tuple[Curve, tuple[tuple[float, int], ...]], ...]


@dataclass(frozen=True)
class Plan:
    path: Path
    parts: tuple[PartPlan | None, ...]  # one per score part, in score order
    labels: tuple[str, ...]  # every part's name, or "part <n>", in score order
    # (offset, quarter-BPM) tempo change points: a time curve's ms to ticks
    segments: tuple[tuple[float, float], ...] = ((0.0, 120.0),)

    @property
    def n_parts(self) -> int:
        return sum(1 for p in self.parts if p is not None and p.samples)

    def keyswitch_parts(self) -> list[str]:
        """The labels of the parts with a keyswitch curve, in score order."""
        return [
            p.label
            for p in self.parts
            if p is not None and any(c.kind == "keyswitch" for c, _ in p.samples)
        ]

    @property
    def has_time(self) -> bool:
        return any(c.kind == "time" for p in self.parts if p is not None for c, _ in p.samples)


@dataclass(frozen=True)
class Merged:
    """What `merge` did: MIDI events added, and the note events a time
    curve moved off their written tick or a clamp moved."""

    events: int = 0
    shifted: int = 0
    clamped: int = 0

    def __add__(self, other: Merged) -> Merged:
        return Merged(
            self.events + other.events,
            self.shifted + other.shifted,
            self.clamped + other.clamped,
        )


# --- load --------------------------------------------------------------------


class _Where:
    """Error prefix for one place in the file: `<file>: parts['X'].curves[2]`."""

    def __init__(self, path: Path, at: str = ""):
        self.path, self.at = path, at

    def __truediv__(self, more: str) -> _Where:
        return _Where(self.path, self.at + more)

    def error(self, message: str) -> ChanteyError:
        return ChanteyError(f"expression sidecar {self.path}: {self.at or 'top level'}: {message}")


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _refuse_constant(name: str):
    raise ValueError(f"{name} is not a number JSON allows")


def _keys(where: _Where, obj: object, allowed: set[str], what: str) -> dict:
    if not isinstance(obj, dict):
        raise where.error(f"expected {what} (a JSON object)")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise where.error(
            f"unknown key {', '.join(repr(k) for k in unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    return obj


def load(path: Path) -> Sidecar:
    """Parse and check the sidecar on its own (no score needed)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ChanteyError(f"expression sidecar not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ChanteyError(f"expression sidecar {path}: cannot read: {exc}") from None
    try:
        raw = json.loads(text, parse_constant=_refuse_constant)
    except ValueError as exc:
        raise ChanteyError(f"expression sidecar {path}: not valid JSON: {exc}") from None
    top = _Where(path)
    raw = _keys(top, raw, _TOP_KEYS, "an object with 'parts'")
    if "parts" not in raw:
        raise top.error("missing 'parts'")
    parts_where = top / "parts"
    parts = raw["parts"]
    if not isinstance(parts, dict):
        raise parts_where.error("expected part-names → curves (a JSON object)")
    return Sidecar(
        path, {name: _part(parts_where / f"[{name!r}]", entry) for name, entry in parts.items()}
    )


def _part(where: _Where, entry: object) -> PartCurves:
    entry = _keys(where, entry, _PART_KEYS, "an object with 'curves'")
    if "curves" not in entry:
        raise where.error("missing 'curves'")
    bend_range = entry.get("bend_range", DEFAULT_BEND_RANGE)
    if not _number(bend_range) or bend_range <= 0:
        raise (where / ".bend_range").error(f"expected cents > 0, got {bend_range!r}")
    time_max = entry.get("time_max", DEFAULT_TIME_MAX)
    if not _number(time_max) or not 0 < time_max <= TIME_MAX_LIMIT:
        raise (where / ".time_max").error(
            f"expected milliseconds > 0 and at most {TIME_MAX_LIMIT}, got {time_max!r}"
        )
    if not isinstance(entry["curves"], list):
        raise (where / ".curves").error("expected a list of curves")
    curves = tuple(
        _curve(where / f".curves[{i}]", c, bend_range, time_max)
        for i, c in enumerate(entry["curves"])
    )
    seen: dict[str, int] = {}
    for i, curve in enumerate(curves):
        if curve.label in seen:
            raise (where / f".curves[{i}]").error(
                f"a second {curve.label} curve (the first is curves[{seen[curve.label]}]); "
                "one curve per controller per part"
            )
        seen[curve.label] = i
    return PartCurves(curves, float(bend_range))


def _curve(where: _Where, obj: object, bend_range: float, time_max: float) -> Curve:
    obj = _keys(where, obj, _CURVE_KEYS, "a curve")
    kinds = [k for k in CURVE_KINDS if k in obj]
    if len(kinds) != 1:
        raise where.error(f"name exactly one curve kind ({', '.join(CURVE_KINDS)})")
    (kind,) = kinds
    number = None
    if kind == "cc":
        number = obj["cc"]
        if not isinstance(number, int) or isinstance(number, bool):
            raise (where / ".cc").error(f"expected a controller number, got {number!r}")
        if not 0 <= number <= CC_MAX_NUMBER:
            raise (where / ".cc").error(
                f"controller {number} is outside 0-{CC_MAX_NUMBER} "
                "(120-127 are channel-mode messages)"
            )
        low, high, unit = 0, 127, ""
    else:
        if obj[kind] is not True:
            raise (where / f".{kind}").error(f"expected true, got {obj[kind]!r}")
        if kind == "bend":
            low, high, unit = -bend_range, bend_range, " cents"
        elif kind == "time":
            low, high, unit = -time_max, time_max, " ms (time_max)"
        else:
            low, high, unit = 0, 127, ""
    interp = obj.get("interp")
    if interp not in INTERPS:
        raise (where / ".interp").error(f"expected one of {', '.join(INTERPS)}, got {interp!r}")
    if kind == "keyswitch" and interp != "hold":
        raise (where / ".interp").error(
            "a keyswitch curve is 'hold' only: a key picks a recording and has no values in between"
        )
    velocity = None
    if "velocity" in obj:
        velocity = obj["velocity"]
        if kind != "keyswitch":
            raise (where / ".velocity").error("'velocity' belongs to a keyswitch curve")
        if not isinstance(velocity, int) or isinstance(velocity, bool) or not 1 <= velocity <= 127:
            raise (where / ".velocity").error(f"expected an integer 1-127, got {velocity!r}")
    elif kind == "keyswitch":
        velocity = KEYSWITCH_VELOCITY
    raw_points = obj.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise (where / ".points").error("expected a non-empty list of [offset, value]")
    points = []
    for j, point in enumerate(raw_points):
        at = where / f".points[{j}]"
        if not (isinstance(point, list) and len(point) == 2 and all(map(_number, point))):
            raise at.error(f"expected [offset, value] (two numbers), got {point!r}")
        offset, value = point
        if offset < 0:
            raise at.error(f"offset {offset:g} is before the score's start")
        if points and offset < points[-1][0]:
            raise at.error(
                f"offset {offset:g} comes before the previous point's {points[-1][0]:g}; "
                "points must be sorted by offset"
            )
        if not low <= value <= high:
            raise at.error(f"{value:g} is outside {low:g} to {high:g}{unit} for {kind}")
        if kind == "keyswitch" and value != int(value):
            raise at.error(f"{value:g} is not a MIDI key (an integer 0-127)")
        points.append((float(offset), float(value)))
    return Curve(kind, number, interp, tuple(points), velocity)


# --- resolve -----------------------------------------------------------------


def _bpm_at(segments: list[tuple[float, float]], offset: float) -> float:
    bpm = segments[0][1]
    for start, value in segments:
        if start > offset:
            break
        bpm = value
    return bpm


def tail_quarters(expanded: stream.Score) -> float:
    """The render tail (EOT_TAIL_S) in quarters, at the tempo at the end."""
    segments, _ = tempo_segments(expanded)
    return EOT_TAIL_S * _bpm_at(segments, float(expanded.highestTime)) / 60


def _encode(curve: Curve, value: float, bend_range: float) -> int:
    if curve.kind != "bend":
        return round(value)
    return min(BEND_MAX, max(0, BEND_CENTER + round(value / bend_range * BEND_CENTER)))


def _sample(curve: Curve, segments, bend_range: float) -> tuple[tuple[float, int], ...]:
    """(offset, MIDI value) samples: the first value from 0, then each point
    (`hold`), or a sample per step between points (`linear`). A keyswitch
    is sent at its points only: before the first, the SFZ's `sw_default`
    plays."""
    points = curve.points
    backfill = points[0][0] > 0 and curve.kind != "keyswitch"
    raw = [(0.0, points[0][1])] if backfill else []
    if curve.interp == "hold":
        raw.extend(points)
    else:
        raw.append(points[0])
        for (a, va), (b, vb) in zip(points, points[1:], strict=False):
            t = a
            while True:
                t += max(STEP_QL, STEP_S * _bpm_at(segments, t) / 60)
                if t >= b:
                    break
                raw.append((t, va + (vb - va) * (t - a) / (b - a)))
            raw.append((b, vb))
    return tuple((t, _encode(curve, v, bend_range)) for t, v in raw)


def resolve(sidecar: Sidecar, expanded: stream.Score) -> Plan:
    """Check the sidecar against the score the render will use, and sample
    every curve. A part-name that matches no part, or an offset past the
    part's end plus the render tail, is an error."""
    segments, _ = tempo_segments(expanded)
    tail = tail_quarters(expanded)
    source = f"expression sidecar {sidecar.path}"
    used: set[str] = set()
    plans: list[PartPlan | None] = []
    for part in expanded.parts:
        name = part.partName
        key = _map_key(sidecar.parts, name, source) if name else None
        if key is None:
            plans.append(None)
            continue
        used.add(key)
        entry = sidecar.parts[key]
        length = float(part.highestTime)
        limit = length + tail
        for curve in entry.curves:
            last = curve.points[-1][0]
            if last > limit + 1e-9:
                raise ChanteyError(
                    f"{source}: part {name!r} {curve.label}: offset {last:g} is past the "
                    f"part's end ({length:g} quarters) plus the {EOT_TAIL_S:g} s tail "
                    f"({limit:g} quarters at the final tempo)"
                )
        samples = tuple(
            (c, () if c.kind == "time" else _sample(c, segments, entry.bend_range))
            for c in entry.curves
        )
        plans.append(PartPlan(name, samples))
    unknown = [repr(key) for key in sidecar.parts if key not in used]
    if unknown:
        names = ", ".join(repr(p.partName) for p in expanded.parts if p.partName)
        raise ChanteyError(
            f"{source}: no part named {', '.join(unknown)} (the score's parts: {names or 'none'})"
        )
    labels = tuple(p.partName or f"part {i + 1}" for i, p in enumerate(expanded.parts))
    return Plan(sidecar.path, tuple(plans), labels, tuple(segments))


# --- merge -------------------------------------------------------------------


def _ticks(samples, tpq: int) -> list[tuple[int, int]]:
    """Samples on ticks: the last value at each tick, repeats dropped."""
    out: list[tuple[int, int]] = []
    for offset, value in samples:
        tick = round(offset * tpq)
        if out and out[-1][0] == tick:
            out.pop()
        if not out or out[-1][1] != value:
            out.append((tick, value))
    return out


_VOICE_TYPES = {m.value for m in midi.ChannelVoiceMessages}


def _channels(track: midi.MidiTrack) -> list[int]:
    return sorted({e.channel for _, e in _absolute(track) if e.type in _VOICE_TYPES})


def _existing_conflict(track: midi.MidiTrack, curve: Curve) -> bool:
    """An event the export already has for this controller. music21's centre
    pitch-bend at tick 0 is a reset, not a conflict: the curve follows it.
    A keyswitch's conflict is a played note (`_check_switch_keys`); a time
    curve adds no events."""
    if curve.kind in ("keyswitch", "time"):
        return False
    voice = midi.ChannelVoiceMessages
    for tick, e in _absolute(track):
        if curve.kind == "cc":
            if e.type == voice.CONTROLLER_CHANGE and e.parameter1 == curve.number:
                return True
        elif e.type == voice.PITCH_BEND:
            if tick > 0 or e.parameter1 + (e.parameter2 << 7) != BEND_CENTER:
                return True
    return False


def _check_switch_keys(track: midi.MidiTrack, tpq: int, label: str, curve: Curve) -> None:
    """Every switch key lies outside the part's played range (its lowest to
    its highest note-on), or it would sound as a pitch."""
    first: dict[int, int] = {}  # played key -> its first tick
    for tick, e in _absolute(track):
        if e.isNoteOn():
            first.setdefault(e.pitch, tick)
    if not first:
        return
    low, high = min(first), max(first)
    for key in sorted({int(v) for _, v in curve.points}):
        if low <= key <= high:
            nearest = min(first, key=lambda p: (abs(p - key), p))
            raise ChanteyError(
                f"part {label!r} keyswitch: key {key_label(key)} is inside the part's "
                f"played range, {key_label(low)} to {key_label(high)}, so it would sound "
                f"as a pitch; the nearest played note is {key_label(nearest)}, first at "
                f"offset {first[nearest] / tpq:g}"
            )


def _switch_note(
    track: midi.MidiTrack, channel: int, key: int, velocity: int
) -> tuple[midi.MidiEvent, midi.MidiEvent]:
    voice = midi.ChannelVoiceMessages
    on = midi.MidiEvent(track, type=voice.NOTE_ON, channel=channel)
    on.pitch, on.velocity = key, velocity
    off = midi.MidiEvent(track, type=voice.NOTE_OFF, channel=channel)
    off.pitch, off.velocity = key, 0
    return on, off


def _event(track: midi.MidiTrack, channel: int, curve: Curve, value: int) -> midi.MidiEvent:
    if curve.kind == "cc":
        e = midi.MidiEvent(track, type=midi.ChannelVoiceMessages.CONTROLLER_CHANGE, channel=channel)
        e.parameter1, e.parameter2 = curve.number, value
    else:
        e = midi.MidiEvent(track, type=midi.ChannelVoiceMessages.PITCH_BEND, channel=channel)
        e.parameter1, e.parameter2 = value & 0x7F, value >> 7
    return e


def _insert(track: midi.MidiTrack, inserts: list[tuple[int, midi.MidiEvent]]) -> None:
    """Interleave `inserts` (sorted by tick) with the track's events: at a
    shared tick they follow everything but note-ons, which they precede, so
    a note sounds with the value set at its tick. The End-of-Track stays
    last, moved later only if an insert lies past it."""
    timed = _absolute(track)
    (eot_tick, eot), body = timed[-1], timed[:-1]
    if eot.type != midi.MetaEvents.END_OF_TRACK:
        raise ChanteyError("MIDI export: a track does not end with End-of-Track")
    merged, j = [], 0
    for tick, event in body:
        while j < len(inserts) and (
            inserts[j][0] < tick or (inserts[j][0] == tick and event.isNoteOn())
        ):
            merged.append(inserts[j])
            j += 1
        merged.append((tick, event))
    merged.extend(inserts[j:])
    merged.append((max(eot_tick, merged[-1][0]), eot))
    events, previous = [], 0
    for tick, event in merged:
        events += [midi.DeltaTime(track, time=tick - previous), event]
        previous = tick
    track.events = events


def value_at(curve: Curve, offset: float) -> float:
    """A time curve's value at a quarter-length offset: the last point at or
    before it (`hold`), or the line between the points around it (`linear`).
    The first value holds before the first point, the last after the last."""
    points = curve.points
    i = -1
    for j, (t, _) in enumerate(points):
        if t > offset:
            break
        i = j
    if i < 0:
        return points[0][1]
    if curve.interp == "hold" or i == len(points) - 1:
        return points[i][1]
    (a, va), (b, vb) = points[i], points[i + 1]
    return va + (vb - va) * (offset - a) / (b - a)


def _shift_notes(track: midi.MidiTrack, tpq: int, curve: Curve, segments) -> tuple[int, int]:
    """Move every note-on and note-off in the track by the time curve's ms
    at its own written offset, keeping each key's on/off order by clamping
    (see the spec's *Time curves*). Returns (shifted, clamped): the note
    events left off their written tick, and those a clamp moved."""
    timed = _absolute(track)
    (eot_tick, eot), body = timed[-1], timed[:-1]
    if eot.type != midi.MetaEvents.END_OF_TRACK:
        raise ChanteyError("MIDI export: a track does not end with End-of-Track")
    ticks = [tick for tick, _ in body]
    by_tick: dict[int, int] = {}
    keys: dict[tuple[int, int], list[int]] = {}
    clamped: set[int] = set()
    for i, (tick, event) in enumerate(body):
        if not (event.isNoteOn() or event.isNoteOff()):
            continue
        if tick not in by_tick:
            offset = tick / tpq
            ms = value_at(curve, offset)
            by_tick[tick] = round(ms * tpq * _bpm_at(segments, offset) / 60000)
        ticks[i] = tick + by_tick[tick]
        if ticks[i] < 0:
            ticks[i] = 0
            clamped.add(i)
        keys.setdefault((event.channel, event.pitch), []).append(i)
    for sequence in keys.values():
        for n, i in enumerate(sequence):
            if body[i][1].isNoteOff() and n + 1 < len(sequence):
                following = sequence[n + 1]
                if body[following][1].isNoteOn() and ticks[following] < ticks[i]:
                    ticks[i] = ticks[following]
                    clamped.add(i)
            if n and ticks[i] < ticks[sequence[n - 1]]:
                ticks[i] = ticks[sequence[n - 1]]
                clamped.add(i)
    shifted = sum(1 for i in range(len(body)) if ticks[i] != body[i][0])
    order = sorted(range(len(body)), key=lambda i: (ticks[i], i))
    events, previous = [], 0
    for i in order:
        events += [midi.DeltaTime(track, time=ticks[i] - previous), body[i][1]]
        previous = ticks[i]
    end = max(eot_tick, previous)
    track.events = [*events, midi.DeltaTime(track, time=end - previous), eot]
    return shifted, len(clamped)


def merge(plan: Plan, tracks: list[midi.MidiTrack], tpq: int, *, own_files: bool) -> Merged:
    """Merge the plan into the part tracks (score order); returns what it
    did (`Merged`). `own_files` is true when each part renders from its own
    file (sfizz); otherwise two parts sharing a channel is an error for a
    part with curves, since its controllers would shape the other part too,
    and a keyswitch curve is an error (`refuse_keyswitches`)."""
    if not own_files:
        refuse_keyswitches(plan)
    if len(tracks) != len(plan.parts):
        raise ChanteyError(f"MIDI export has {len(tracks)} part tracks for {len(plan.parts)} parts")
    channels = [_channels(track) for track in tracks]
    done = Merged()
    for i, (part, track) in enumerate(zip(plan.parts, tracks, strict=True)):
        if part is None or not part.samples:
            continue
        if not channels[i]:
            raise ChanteyError(f"part {part.label!r}: the MIDI export gives it no channel")
        if not own_files:
            for k, other in enumerate(channels):
                shared = sorted(set(channels[i]) & set(other))
                if k != i and shared:
                    if all(c.kind == "time" for c, _ in part.samples):
                        why = "its time curve's moved note-offs could end that part's notes"
                    else:
                        why = "its controller curves would shape that part too"
                    raise ChanteyError(
                        f"part {part.label!r} shares MIDI channel {shared[0]} with "
                        f"{plan.labels[k]!r} in the export, so {why} under FluidSynth; "
                        "give the parts distinct instruments, or use --engine sfizz "
                        "(one file per part)"
                    )
        inserts: list[tuple[int, int, midi.MidiEvent]] = []
        timing = None
        for order, (curve, samples) in enumerate(part.samples):
            if _existing_conflict(track, curve):
                raise ChanteyError(
                    f"part {part.label!r}: the MIDI export already has {curve.label} "
                    "events, and the sidecar's curve would fight them"
                )
            if curve.kind == "keyswitch":
                _check_switch_keys(track, tpq, part.label, curve)
                for tick, key in _ticks(samples, tpq):
                    for channel in channels[i]:
                        on, off = _switch_note(track, channel, key, curve.velocity)
                        inserts += [(tick, order, on), (tick + KEYSWITCH_TICKS, order, off)]
                continue
            if curve.kind == "time":
                timing = curve
                continue
            for tick, value in _ticks(samples, tpq):
                for channel in channels[i]:
                    inserts.append((tick, order, _event(track, channel, curve, value)))
        inserts.sort(key=lambda item: (item[0], item[1]))
        if timing is not None:
            # the notes move first, so the inserts are placed against the
            # moved note-ons and keep their own ticks
            shifted, clamped = _shift_notes(track, tpq, timing, plan.segments)
            done += Merged(0, shifted, clamped)
        _insert(track, [(tick, e) for tick, _, e in inserts])
        done += Merged(len(inserts))
    return done


def refuse_keyswitches(plan: Plan) -> None:
    """A keyswitch curve under FluidSynth is an error: a GM SoundFont has no
    keyswitches, so it would play the switch key as a pitch."""
    parts = plan.keyswitch_parts()
    if parts:
        raise ChanteyError(
            f"expression sidecar {plan.path}: part {parts[0]!r} has a keyswitch curve, "
            "which needs --engine sfizz: under FluidSynth a GM SoundFont would play "
            "the switch key as a pitch"
        )


def label(plan: Plan, merged: Merged) -> str:
    """The output-line note: `expression: <file>; parts: N; events: M`, and
    `; shifted: S; clamped: C` when a part has a time curve."""
    note = f"expression: {plan.path}; parts: {plan.n_parts}; events: {merged.events}"
    if plan.has_time:
        note += f"; shifted: {merged.shifted}; clamped: {merged.clamped}"
    return note
