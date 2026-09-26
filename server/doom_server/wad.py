"""Minimal WAD reader used to enrich ViZDoom's geometry with *line specials*.

ViZDoom exposes walls and sector heights but not what a line *does*. Reading the
map lumps straight from the WAD tells us which lines are doors (and which key they
need), switches, teleporters and - most importantly - the level exit.

Supported map formats: vanilla Doom (binary), Hexen (binary) and UDMF (text).
Anything we cannot parse simply yields ``None`` and the server falls back to
geometry-only behaviour.
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import knowledge as K

log = logging.getLogger(__name__)

MAP_LUMPS = {
    "THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SEGS", "SSECTORS", "NODES",
    "SECTORS", "REJECT", "BLOCKMAP", "BEHAVIOR", "SCRIPTS", "TEXTMAP", "ZNODES",
    "DIALOGUE", "ENDMAP",
}


@dataclass
class MapLine:
    x1: float
    y1: float
    x2: float
    y2: float
    special: int
    tag: int
    front_sector: int | None
    back_sector: int | None
    # Derived meaning -------------------------------------------------------
    kind: str = "none"  # none | door | switch | exit | teleport
    key: str | None = None  # key colour required, if any
    exit_type: str | None = None  # switch | walk | shoot (exits only)
    secret_exit: bool = False


@dataclass
class MapInfo:
    name: str
    format: str
    lines: list[MapLine] = field(default_factory=list)
    sector_specials: dict[int, int] = field(default_factory=dict)

    @property
    def exits(self) -> list[MapLine]:
        return [ln for ln in self.lines if ln.kind == "exit"]


class WadFile:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        ident, count, offset = struct.unpack_from("<4sii", self.data, 0)
        if ident not in (b"IWAD", b"PWAD"):
            raise ValueError(f"{path} is not a WAD file")
        self.lumps: list[tuple[str, int, int]] = []
        for i in range(count):
            pos, size, raw = struct.unpack_from("<ii8s", self.data, offset + 16 * i)
            name = raw.split(b"\0", 1)[0].decode("ascii", "replace").upper()
            self.lumps.append((name, pos, size))

    def map_lumps(self, map_name: str) -> dict[str, bytes] | None:
        map_name = map_name.upper()
        for i, (name, _, _) in enumerate(self.lumps):
            if name != map_name:
                continue
            out: dict[str, bytes] = {}
            for lname, pos, size in self.lumps[i + 1:]:
                if lname not in MAP_LUMPS or lname in out:
                    break
                out[lname] = self.data[pos:pos + size]
                if lname == "ENDMAP":
                    break
            return out
        return None


def load_map_info(wad_paths: list[str | Path], map_name: str) -> MapInfo | None:
    """Find ``map_name`` in the given WADs (later files override earlier ones)."""
    for path in reversed([p for p in wad_paths if p]):
        try:
            lumps = WadFile(path).map_lumps(map_name)
        except (OSError, ValueError, struct.error) as exc:
            log.debug("cannot read %s: %s", path, exc)
            continue
        if not lumps:
            continue
        try:
            if "TEXTMAP" in lumps:
                info = _parse_udmf(map_name, lumps["TEXTMAP"])
            elif "BEHAVIOR" in lumps:
                info = _parse_binary(map_name, lumps, hexen=True)
            else:
                info = _parse_binary(map_name, lumps, hexen=False)
        except (KeyError, struct.error, ValueError) as exc:
            log.warning("failed to parse %s in %s: %s", map_name, path, exc)
            return None
        log.info("loaded %s (%s format) from %s: %d lines, %d exits", map_name,
                 info.format, Path(path).name, len(info.lines), len(info.exits))
        return info
    return None


# ---------------------------------------------------------------------------
# Binary formats
# ---------------------------------------------------------------------------
def _parse_binary(map_name: str, lumps: dict[str, bytes], hexen: bool) -> MapInfo:
    vraw, lraw, sraw = lumps["VERTEXES"], lumps["LINEDEFS"], lumps["SIDEDEFS"]
    verts = [struct.unpack_from("<hh", vraw, i * 4) for i in range(len(vraw) // 4)]
    side_sector = [struct.unpack_from("<H", sraw, i * 30 + 28)[0] for i in range(len(sraw) // 30)]
    info = MapInfo(map_name, "hexen" if hexen else "doom")

    def sector_of(side: int) -> int | None:
        return side_sector[side] if side != 0xFFFF and side < len(side_sector) else None

    if hexen:
        for i in range(len(lraw) // 16):
            v1, v2, flags, special, a0, a1, a2, a3, a4, s1, s2 = struct.unpack_from("<HHHBBBBBBHH", lraw, i * 16)
            line = _make_line(verts, v1, v2, special, a0, sector_of(s1), sector_of(s2))
            # Activation type lives in flag bits 10-12; 1 == SPAC_Use.
            _classify_hexen(line, special, (a0, a1, a2, a3, a4), use=((flags >> 10) & 7) == 1)
            info.lines.append(line)
    else:
        for i in range(len(lraw) // 14):
            v1, v2, _flags, special, tag, s1, s2 = struct.unpack_from("<HHhhhHH", lraw, i * 14)
            line = _make_line(verts, v1, v2, special, tag, sector_of(s1), sector_of(s2))
            _classify_doom(line)
            info.lines.append(line)

    secraw = lumps.get("SECTORS", b"")
    for i in range(len(secraw) // 26):
        special = struct.unpack_from("<h", secraw, i * 26 + 22)[0]
        if special:
            info.sector_specials[i] = special
    return info


def _make_line(verts, v1, v2, special, tag, front, back) -> MapLine:
    x1, y1 = verts[v1]
    x2, y2 = verts[v2]
    return MapLine(float(x1), float(y1), float(x2), float(y2), int(special), int(tag), front, back)


def _classify_doom(line: MapLine) -> None:
    sp = line.special
    if sp in K.DOOM_EXITS:
        line.kind, line.exit_type = "exit", K.DOOM_EXITS[sp]
        line.secret_exit = sp in K.DOOM_SECRET_EXITS
    elif sp in K.DOOM_MANUAL_DOORS:
        line.kind, line.key = "door", K.DOOM_MANUAL_DOORS[sp]
    elif sp in K.DOOM_SWITCHES:
        line.kind, line.key = "switch", K.DOOM_LOCKED_SWITCHES.get(sp)
    elif sp in K.DOOM_TELEPORTS:
        line.kind = "teleport"


def _classify_hexen(line: MapLine, special: int, args: tuple, use: bool) -> None:
    if special in K.HEXEN_EXITS:
        line.kind, line.exit_type = "exit", "switch" if use else "walk"
        line.secret_exit = special == 244
    elif special in K.HEXEN_DOORS and use:
        # Tag 0 means "the door is the sector behind this line"; otherwise the
        # line remotely operates tagged sectors, i.e. it behaves like a switch.
        line.kind = "door" if args[0] == 0 else "switch"
        lock = args[3] if special == 13 else (args[4] if special == 202 else 0)
        line.key = K.HEXEN_LOCK_COLORS.get(lock)
    elif special in K.HEXEN_TELEPORTS:
        line.kind = "teleport"
    elif special and use:
        line.kind = "switch"


# ---------------------------------------------------------------------------
# UDMF (text) format
# ---------------------------------------------------------------------------
_BLOCK_RE = re.compile(r"(\w+)\s*\{([^}]*)\}", re.S)
_KV_RE = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|[^;]+);")


def _parse_udmf(map_name: str, text: bytes) -> MapInfo:
    src = re.sub(r"//[^\n]*|/\*.*?\*/", "", text.decode("latin-1"), flags=re.S)
    verts: list[tuple[float, float]] = []
    sides: list[int] = []
    raw_lines: list[dict[str, str]] = []
    info = MapInfo(map_name, "udmf")
    sector_index = 0
    for kind, body in _BLOCK_RE.findall(src):
        kv = {k.lower(): v.strip().strip('"') for k, v in _KV_RE.findall(body)}
        kind = kind.lower()
        if kind == "vertex":
            verts.append((float(kv.get("x", 0)), float(kv.get("y", 0))))
        elif kind == "sidedef":
            sides.append(int(kv.get("sector", 0)))
        elif kind == "linedef":
            raw_lines.append(kv)
        elif kind == "sector":
            special = int(kv.get("special", 0))
            if special:
                info.sector_specials[sector_index] = special
            sector_index += 1
    for kv in raw_lines:
        v1, v2 = int(kv["v1"]), int(kv["v2"])
        s1, s2 = int(kv.get("sidefront", -1)), int(kv.get("sideback", -1))
        special = int(kv.get("special", 0))
        args = tuple(int(kv.get(f"arg{i}", 0)) for i in range(5))
        (x1, y1), (x2, y2) = verts[v1], verts[v2]
        line = MapLine(x1, y1, x2, y2, special, args[0],
                       sides[s1] if 0 <= s1 < len(sides) else None,
                       sides[s2] if 0 <= s2 < len(sides) else None)
        _classify_hexen(line, special, args, use=kv.get("playeruse", "false").lower() == "true")
        info.lines.append(line)
    return info
