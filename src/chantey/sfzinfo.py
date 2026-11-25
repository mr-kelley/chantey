"""An SFZ instrument's keyswitch map, for `chantey info` (v0.6).

A keyswitch patch (VPO's `*-KS-*`) holds several articulations of each
note and picks one by a note-on below the playing range: `sw_lokey` to
`sw_hikey` is the switch range, each region's `sw_last` is the key that
selects it, `sw_label` names it, and `sw_default` plays before any switch.
`read_keyswitches()` collects those opcodes so a caller can map
articulation to key (an expression sidecar's `keyswitch` curve) without
reading the SFZ by hand.

This is a reader for those few opcodes, not an SFZ parser: it follows the
header inheritance (`<global>` → `<master>` → `<group>` → `<region>`),
`#define` and `#include`, and ignores everything else. Keys may be numbers
or note names, with middle C `c4` = 60 (the SFZ default).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ChanteyError
from .sfzengine import _map_key, parse_sfz_map

NOTE_NAMES = ("c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b")
_NOTE_STEPS = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_NOTE = re.compile(r"([a-g])([#b]?)(-?\d+)", re.IGNORECASE)
_TOKEN = re.compile(r"<(\w+)>|(\w+)=")
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_DIRECTIVE = re.compile(r"^\s*#(define|include)\s+(.*?)\s*$", re.MULTILINE)
MAX_INCLUDE_DEPTH = 16
# <control> opcodes that move keys away from the values written
_KEY_SHIFTS = ("note_offset", "octave_offset", "sw_note_offset", "sw_octave_offset")
_SCOPES = ("global", "master", "group")


def key_name(key: int) -> str:
    """36 → `c2`, 39 → `d#2` (SFZ spelling, middle C = c4 = 60)."""
    return f"{NOTE_NAMES[key % 12]}{key // 12 - 1}"


def key_label(key: int) -> str:
    """36 → `36 (c2)`."""
    return f"{key} ({key_name(key)})"


def parse_key(value: str) -> int | None:
    """An SFZ key value, a number or a note name, as a MIDI key; None when
    it is neither or lies outside 0-127."""
    value = value.strip()
    if re.fullmatch(r"-?\d+", value):
        key = int(value)
    else:
        m = _NOTE.fullmatch(value)
        if m is None:
            return None
        letter, accidental, octave = m.groups()
        key = (
            _NOTE_STEPS[letter.lower()]
            + {"#": 1, "b": -1, "": 0}[accidental]
            + 12 * (int(octave) + 1)
        )
    return key if 0 <= key <= 127 else None


@dataclass(frozen=True)
class KeyswitchMap:
    lokey: tuple[int, ...]  # each distinct sw_lokey over the regions, sorted
    hikey: tuple[int, ...]
    default: tuple[int, ...]
    # (lowest, highest, sw_label or None): sw_last, or sw_lolast-sw_hilast
    switches: tuple[tuple[int, int, str | None], ...]
    shifted: tuple[str, ...]  # key-shifting <control> opcodes the file sets
    bad: tuple[str, ...]  # `opcode=value` pairs whose value is not a key

    @property
    def empty(self) -> bool:
        return not (self.lokey or self.hikey or self.default or self.switches)


def _source(path: Path, root: Path, depth: int, defines: dict[str, str]) -> str:
    """The file's text with comments removed, `#include`s inlined (paths
    relative to the top file's directory, as sfizz resolves them) and
    `#define`d names substituted."""
    if depth > MAX_INCLUDE_DEPTH:
        raise ChanteyError(f"{path}: #include nested deeper than {MAX_INCLUDE_DEPTH}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ChanteyError(f"cannot read SFZ {path}: {exc.strerror or exc}") from None
    text = _COMMENT.sub(" ", text)
    out, at = [], 0
    for m in _DIRECTIVE.finditer(text):
        out.append(_substitute(text[at : m.start()], defines))
        at = m.end()
        verb, rest = m.groups()
        if verb == "define":
            name, _, value = rest.partition(" ")
            if name.startswith("$"):
                defines[name] = _substitute(value.strip(), defines)
        else:
            target = rest.strip().strip('"')
            out.append(_source(root / _substitute(target, defines), root, depth + 1, defines))
    out.append(_substitute(text[at:], defines))
    return "\n".join(out)


def _substitute(text: str, defines: dict[str, str]) -> str:
    for name in sorted(defines, key=len, reverse=True):
        text = text.replace(name, defines[name])
    return text


def _opcodes(text: str):
    """(header or None, opcode, value) in file order; a header yields
    (header, None, None)."""
    tokens = list(_TOKEN.finditer(text))
    for i, m in enumerate(tokens):
        if m.group(1) is not None:
            yield m.group(1).lower(), None, None
            continue
        end = tokens[i + 1].start() if i + 1 < len(tokens) else len(text)
        yield None, m.group(2), text[m.end() : end].strip()


def read_keyswitches(path: Path | str) -> KeyswitchMap:
    """The keyswitch opcodes of every region in the SFZ at `path`, each
    region seeing its headers' values as sfizz does."""
    path = Path(path)
    if not path.is_file():
        raise ChanteyError(f"SFZ file not found: {path}")
    text = _source(path, path.parent, 0, {})
    control: dict[str, str] = {}
    scopes: dict[str, dict[str, str]] = {s: {} for s in _SCOPES}
    current: dict[str, str] | None = None  # where opcodes go; None = ignored
    regions: list[dict[str, str]] = []
    for header, opcode, value in _opcodes(text):
        if header is not None:
            if header == "control":
                current = control
            elif header in _SCOPES:
                # a header resets itself and every level below it
                for s in _SCOPES[_SCOPES.index(header) :]:
                    scopes[s] = {}
                current = scopes[header]
            elif header == "region":
                current = {k: v for s in _SCOPES for k, v in scopes[s].items()}
                regions.append(current)
            else:  # <curve>, <effect>, <midi>, <sample>: not region opcodes
                current = None
            continue
        if current is not None:
            current[opcode] = value

    lokey, hikey, default, bad = set(), set(), set(), []
    switches: dict[tuple[int, int], str | None] = {}

    def key(region, opcode):
        if opcode not in region:
            return None
        k = parse_key(region[opcode])
        if k is None:
            bad.append(f"{opcode}={region[opcode]}")
        return k

    for region in regions:
        for opcode, into in (("sw_lokey", lokey), ("sw_hikey", hikey), ("sw_default", default)):
            k = key(region, opcode)
            if k is not None:
                into.add(k)
        label = region.get("sw_label") or None
        last = key(region, "sw_last")
        low, high = key(region, "sw_lolast"), key(region, "sw_hilast")
        span = (last, last) if last is not None else None
        if span is None and (low is not None or high is not None):
            span = (low if low is not None else high, high if high is not None else low)
        if span is not None and (span not in switches or switches[span] is None):
            switches[span] = label
    shifted = tuple(o for o in _KEY_SHIFTS if control.get(o, "0").strip() not in ("0", ""))
    return KeyswitchMap(
        tuple(sorted(lokey)),
        tuple(sorted(hikey)),
        tuple(sorted(default)),
        tuple((lo, hi, label) for (lo, hi), label in sorted(switches.items())),
        shifted,
        tuple(dict.fromkeys(bad)),
    )


def part_lines(
    names: list[str | None], raw_map: str, default_sfz: str | None
) -> tuple[list[list[str]], list[str]]:
    """Per part (score order), the info lines for its SFZ: from the
    `CHANTEY_SFZ_MAP` value `raw_map` as render maps it, else `default_sfz`
    (`--sfz`); none for a part with neither. A map or SFZ that cannot be read
    is a warning, never an error: `info` still summarizes the score."""
    warnings: list[str] = []
    try:
        mapping = parse_sfz_map(raw_map)
    except ChanteyError as exc:
        mapping = {}
        warnings.append(f"{exc}; map ignored")
    out: list[list[str]] = []
    for name in names:
        try:
            key = _map_key(mapping, name) if name else None
        except ChanteyError as exc:
            warnings.append(str(exc))
            key = None
        chosen = mapping[key] if key is not None else default_sfz
        if chosen is None:
            out.append([])
            continue
        sfz = os.path.abspath(os.path.expanduser(chosen))
        try:
            out.append([f"sfz: {sfz}", *format_keyswitches(read_keyswitches(sfz))])
        except ChanteyError as exc:
            out.append([f"sfz: {sfz}"])
            warnings.append(str(exc))
    return out, warnings


def format_keyswitches(ks: KeyswitchMap) -> list[str]:
    """`chantey info`'s lines for one SFZ (unindented)."""
    if ks.empty:
        return ["keyswitches: none"]

    def keys(values):
        return " / ".join(key_label(k) for k in values) or "unset"

    lines = [
        f"keyswitches: sw_lokey {keys(ks.lokey)} to sw_hikey {keys(ks.hikey)}; "
        f"sw_default {keys(ks.default)}"
    ]
    for low, high, label in ks.switches:
        if low == high:
            at = f"sw_last {key_label(low)}"
        else:
            at = f"sw_lolast {key_label(low)} to sw_hilast {key_label(high)}"
        lines.append(f"  {at}: {label or '(no sw_label)'}")
    if ks.shifted:
        lines.append(
            f"  note: the file sets {', '.join(ks.shifted)}; the keys above are as written"
        )
    if ks.bad:
        lines.append(f"  note: not a key, skipped: {', '.join(ks.bad)}")
    return lines
