"""Map understanding for agents: occupancy grid, raycasts, path planning, exploration.

The grid is built from ViZDoom's live sector geometry (walls + floor/ceiling
heights), optionally enriched with WAD line specials (doors, keys, switches,
exits). Heights are refreshed every time we plan, so doors that open, lifts that
lower and platforms that rise are all taken into account.

Coordinates: world units (x east, y north). Grid row 0 is the *southern* edge.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from . import knowledge as K
from .wad import MapInfo

log = logging.getLogger(__name__)

# 8-connected moves: (d_row, d_col, cost multiplier)
_MOVES = [(0, 1, 1.0), (1, 0, 1.0), (0, -1, 1.0), (-1, 0, 1.0),
          (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2))]

CELL_FREE = 0
CELL_WALL = 1
CELL_DOOR = 2  # closed door we believe we can open
CELL_VOID = 3


@dataclass
class Feature:
    """Something interesting on the map that is attached to a line (exit, switch, door)."""

    kind: str  # exit | switch | door | teleport
    x: float  # line midpoint
    y: float
    x1: float
    y1: float
    x2: float
    y2: float
    approach: tuple[float, float]  # where to stand to use it
    key: str | None = None
    secret: bool = False
    exit_type: str | None = None
    sector: int | None = None  # door sector (doors) / front sector (others)
    seen: bool = False
    used: bool = False
    index: int = 0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "x": round(self.x), "y": round(self.y), "key": self.key,
                "secret": self.secret, "exit_type": self.exit_type, "used": self.used}


@dataclass
class Path:
    points: list[tuple[float, float]]  # dense polyline of cell centres, start -> goal
    cost: float
    door_sectors: list[int] = field(default_factory=list)  # closed doors along the way

    @property
    def length(self) -> float:
        return float(sum(math.dist(a, b) for a, b in zip(self.points, self.points[1:])))


class NavMap:
    """Occupancy-grid navigation built from ViZDoom sector info."""

    def __init__(self, sectors, map_info: MapInfo | None = None, cell: float | None = None):
        self._collect_lines(sectors)
        span_x = float(self.lx.max() - self.lx.min()) if len(self.lx) else 64.0
        span_y = float(self.ly.max() - self.ly.min()) if len(self.ly) else 64.0
        if cell is None:
            # 16 units per cell unless the map is huge (keeps graphs < ~200k nodes)
            cell = max(16.0, math.ceil(math.sqrt(span_x * span_y / 200_000) / 8) * 8)
        self.cell = float(cell)
        self.num_sectors = max(s.id for s in sectors) + 1 if sectors else 0
        self.floor_h = np.zeros(self.num_sectors, dtype=np.float64)
        self.ceil_h = np.zeros(self.num_sectors, dtype=np.float64)
        self.sector_special = np.zeros(self.num_sectors, dtype=np.int32)
        self.door_sector = np.zeros(self.num_sectors, dtype=bool)
        self.door_key: dict[int, str | None] = {}
        self.features: list[Feature] = []
        self.keys: set[str] = set()  # keys the player holds (affects locked doors)
        self.failed_doors: set[int] = set()  # doors we tried and could not open
        self.blocked_cells: set[tuple[int, int]] = set()  # places we could not reach
        self._build_grid(sectors)
        self._attach_map_info(map_info)
        self.seen = np.zeros(self.shape, dtype=bool)
        self._graph = None
        self._graph_key = None
        self.update_heights(sectors)

    # ------------------------------------------------------------------ setup
    def _collect_lines(self, sectors) -> None:
        uniq: dict[tuple, list] = {}
        for sec in sectors:
            for ln in sec.lines:
                a, b = (ln.x1, ln.y1), (ln.x2, ln.y2)
                key = (a, b) if a <= b else (b, a)
                entry = uniq.get(key)
                if entry is None:
                    uniq[key] = [ln.x1, ln.y1, ln.x2, ln.y2, sec.id, -1, bool(ln.is_blocking)]
                elif entry[4] != sec.id and entry[5] < 0:
                    entry[5] = sec.id
        arr = np.array(list(uniq.values()), dtype=np.float64).reshape(-1, 7)
        self.x1, self.y1, self.x2, self.y2 = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
        self.sec_a = arr[:, 4].astype(np.int32)
        self.sec_b = arr[:, 5].astype(np.int32)
        # One-sided lines and lines flagged impassable are walls no matter what.
        self.static_block = (arr[:, 6] > 0) | (self.sec_b < 0)
        self.lx = np.concatenate([self.x1, self.x2])
        self.ly = np.concatenate([self.y1, self.y2])
        self._sector_lines = {sec.id: np.array([(l.x1, l.y1, l.x2, l.y2) for l in sec.lines],
                                               dtype=np.float64).reshape(-1, 4) for sec in sectors}

    def _build_grid(self, sectors) -> None:
        c = self.cell
        self.x0 = float(self.lx.min()) - 2 * c
        self.y0 = float(self.ly.min()) - 2 * c
        w = int(math.ceil((float(self.lx.max()) - self.x0) / c)) + 3
        h = int(math.ceil((float(self.ly.max()) - self.y0) / c)) + 3
        self.shape = (h, w)
        self.cell_sector = np.full(self.shape, -1, dtype=np.int32)
        centers_x = self.x0 + (np.arange(w) + 0.5) * c
        centers_y = self.y0 + (np.arange(h) + 0.5) * c

        # Point-in-polygon (crossing number) per sector; big sectors first so
        # nested sectors overwrite their parents in ambiguous spots.
        order = sorted(self._sector_lines.items(), key=lambda kv: -_bbox_area(kv[1]))
        for sid, segs in order:
            if len(segs) < 3:
                continue
            xs, ys = np.concatenate([segs[:, 0], segs[:, 2]]), np.concatenate([segs[:, 1], segs[:, 3]])
            c0, c1 = self._col(xs.min()), self._col(xs.max())
            r0, r1 = self._row(ys.min()), self._row(ys.max())
            if c1 < c0 or r1 < r0:
                continue
            px, py = np.meshgrid(centers_x[c0:c1 + 1], centers_y[r0:r1 + 1])
            inside = _points_in_polygon(px.ravel(), py.ravel(), segs).reshape(px.shape)
            block = self.cell_sector[r0:r1 + 1, c0:c1 + 1]
            block[inside] = sid

        self.static_wall = np.zeros(self.shape, dtype=bool)
        idx = np.nonzero(self.static_block)[0]
        rr, cc = self._raster_lines(idx)
        self.static_wall[rr, cc] = True
        self.inside = self.cell_sector >= 0

    def _attach_map_info(self, info: MapInfo | None) -> None:
        self.map_info = info
        if info is None:
            return
        wad_sectors_ok = True
        max_sector = max([s for ln in info.lines for s in (ln.front_sector, ln.back_sector) if s is not None],
                         default=-1)
        if max_sector >= self.num_sectors:
            log.warning("WAD sector indices do not match the engine; ignoring door sectors")
            wad_sectors_ok = False
        for sid, special in info.sector_specials.items():
            if sid < self.num_sectors:
                self.sector_special[sid] = special
        for i, ln in enumerate(info.lines):
            if ln.kind == "none":
                continue
            if ln.kind == "door" and wad_sectors_ok and ln.back_sector is not None:
                self.door_sector[ln.back_sector] = True
                if ln.key or ln.back_sector not in self.door_key:
                    self.door_key[ln.back_sector] = ln.key
            mx, my = (ln.x1 + ln.x2) / 2, (ln.y1 + ln.y2) / 2
            dx, dy = ln.x2 - ln.x1, ln.y2 - ln.y1
            norm = math.hypot(dx, dy) or 1.0
            nx, ny = dy / norm, -dx / norm  # the front side is to the right of v1->v2
            if ln.kind == "exit" and ln.exit_type == "walk":
                approach = (mx, my)
            else:
                approach = (mx + nx * 40.0, my + ny * 40.0)
                behind = (mx - nx * 40.0, my - ny * 40.0)
                # Trust the front side unless it is outside the map (odd line orientation).
                if self.sector_at(*approach) < 0 <= self.sector_at(*behind):
                    approach = behind
            self.features.append(Feature(
                kind=ln.kind, x=mx, y=my, x1=ln.x1, y1=ln.y1, x2=ln.x2, y2=ln.y2,
                approach=approach, key=ln.key, secret=ln.secret_exit, exit_type=ln.exit_type,
                sector=ln.back_sector if ln.kind == "door" else ln.front_sector, index=i))

    # ------------------------------------------------------------ coordinates
    def _col(self, x: float) -> int:
        return int(math.floor((x - self.x0) / self.cell))

    def _row(self, y: float) -> int:
        return int(math.floor((y - self.y0) / self.cell))

    def to_cell(self, x: float, y: float) -> tuple[int, int]:
        r = min(max(self._row(y), 0), self.shape[0] - 1)
        c = min(max(self._col(x), 0), self.shape[1] - 1)
        return r, c

    def to_world(self, r: int, c: int) -> tuple[float, float]:
        return self.x0 + (c + 0.5) * self.cell, self.y0 + (r + 0.5) * self.cell

    def _raster_lines(self, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if len(idx) == 0:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
        x1, y1, x2, y2 = self.x1[idx], self.y1[idx], self.x2[idx], self.y2[idx]
        n = np.maximum(2, np.ceil(np.hypot(x2 - x1, y2 - y1) / (self.cell * 0.25)).astype(np.int64) + 1)
        start = np.repeat(np.cumsum(n) - n, n)
        k = np.arange(n.sum()) - start
        t = k / np.repeat(n - 1, n)
        xs = np.repeat(x1, n) + t * np.repeat(x2 - x1, n)
        ys = np.repeat(y1, n) + t * np.repeat(y2 - y1, n)
        rr = np.clip(np.floor((ys - self.y0) / self.cell).astype(np.int64), 0, self.shape[0] - 1)
        cc = np.clip(np.floor((xs - self.x0) / self.cell).astype(np.int64), 0, self.shape[1] - 1)
        return rr, cc

    # --------------------------------------------------------------- dynamics
    def update_heights(self, sectors) -> None:
        for s in sectors:
            self.floor_h[s.id] = s.floor_height
            self.ceil_h[s.id] = s.ceiling_height

    def sector_at(self, x: float, y: float) -> int:
        r, c = self.to_cell(x, y)
        return int(self.cell_sector[r, c])

    def _state_key(self):
        return (self.floor_h.tobytes(), self.ceil_h.tobytes(), frozenset(self.keys),
                frozenset(self.failed_doors), frozenset(self.blocked_cells))

    def cell_types(self) -> np.ndarray:
        """Per-cell classification (CELL_FREE / WALL / DOOR / VOID) for current heights."""
        key = self._state_key()
        if getattr(self, "_types_key", None) == key:
            return self._types
        sec = np.where(self.inside, self.cell_sector, 0)
        open_h = (self.ceil_h - self.floor_h)[sec]
        fits = open_h >= K.PLAYER_HEIGHT - 0.5
        openable = np.array([self._door_openable(s) for s in range(self.num_sectors)], dtype=bool)
        types = np.full(self.shape, CELL_VOID, dtype=np.int8)
        types[self.inside] = CELL_FREE
        types[self.inside & ~fits & openable[sec]] = CELL_DOOR
        types[self.inside & ~fits & ~openable[sec]] = CELL_WALL
        # Faces of closed sectors: thin doors may not contain a single cell centre.
        closed = (self.ceil_h - self.floor_h) < K.PLAYER_HEIGHT - 0.5
        two_sided = ~self.static_block
        a_closed = np.zeros(len(self.sec_a), dtype=bool)
        b_closed = np.zeros(len(self.sec_a), dtype=bool)
        a_closed[two_sided] = closed[self.sec_a[two_sided]]
        b_closed[two_sided] = closed[self.sec_b[two_sided]]
        face = two_sided & (a_closed ^ b_closed)
        if face.any():
            closed_side = np.where(a_closed, self.sec_a, self.sec_b)
            door_face = face & openable[closed_side]
            rr, cc = self._raster_lines(np.nonzero(door_face)[0])
            types[rr, cc] = np.where(types[rr, cc] == CELL_FREE, CELL_DOOR, types[rr, cc])
            rr, cc = self._raster_lines(np.nonzero(face & ~door_face)[0])
            types[rr, cc] = CELL_WALL
        types[self.static_wall] = CELL_WALL
        for (r, c) in self.blocked_cells:
            types[r, c] = CELL_WALL
        self._types, self._types_key = types, key
        return types

    def _door_openable(self, sector: int) -> bool:
        if sector in self.failed_doors:
            return False
        if self.map_info is not None and self.door_sector.any():
            if not self.door_sector[sector]:
                return False
            key = self.door_key.get(sector)
            return key is None or key in self.keys
        # No WAD info: any closed sector bordering walkable space may be a door.
        return True

    def is_closed_door(self, sector: int) -> bool:
        return 0 <= sector < self.num_sectors and \
            (self.ceil_h[sector] - self.floor_h[sector]) < K.PLAYER_HEIGHT - 0.5

    # ---------------------------------------------------------------- graph
    def _build_graph(self):
        key = self._state_key()
        if self._graph is not None and self._graph_key == key:
            return self._graph
        types = self.cell_types()
        h, w = self.shape
        walk = (types == CELL_FREE) | (types == CELL_DOOR)
        floor = np.where(self.inside, self.floor_h[np.where(self.inside, self.cell_sector, 0)], -1e9)
        # Extra cost: hugging walls, crossing doors, standing in damaging sectors.
        near_wall = _dilate(types == CELL_WALL) | _dilate(types == CELL_VOID)
        penalty = 1.0 + near_wall * 1.5 + (types == CELL_DOOR) * 6.0
        if self.sector_special.any():
            dmg = np.isin(self.sector_special, list(K.DAMAGING_SECTOR_SPECIALS)) | \
                ((self.sector_special & 0x60) > 0)
            penalty = penalty + dmg[np.where(self.inside, self.cell_sector, 0)] * self.inside * 12.0
        index = np.arange(h * w).reshape(h, w)
        rows, cols, data = [], [], []
        for dr, dc, mult in _MOVES:
            # Source cells whose neighbour (r + dr, c + dc) is still on the grid.
            rs, cs = slice(max(0, -dr), h - max(0, dr)), slice(max(0, -dc), w - max(0, dc))
            rd, cd = slice(rs.start + dr, rs.stop + dr), slice(cs.start + dc, cs.stop + dc)
            src, dst = (rs, cs), (rd, cd)
            ok = walk[src] & walk[dst] & ((floor[dst] - floor[src]) <= K.MAX_STEP_HEIGHT)
            if dr and dc:  # no corner cutting on diagonals
                ok &= walk[rd, cs] & walk[rs, cd]
            rows.append(index[src][ok])
            cols.append(index[dst][ok])
            data.append((mult * self.cell * penalty[dst])[ok])
        graph = csr_matrix((np.concatenate(data), (np.concatenate(rows), np.concatenate(cols))),
                           shape=(h * w, h * w))
        self._graph, self._graph_key = graph, key
        return graph

    def _nearest_walkable(self, x: float, y: float, radius_cells: int = 3) -> tuple[int, int] | None:
        types = self.cell_types()
        r0, c0 = self.to_cell(x, y)
        best, best_d = None, 1e18
        for dr in range(-radius_cells, radius_cells + 1):
            for dc in range(-radius_cells, radius_cells + 1):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < self.shape[0] and 0 <= c < self.shape[1] and types[r, c] in (CELL_FREE, CELL_DOOR):
                    wx, wy = self.to_world(r, c)
                    d = (wx - x) ** 2 + (wy - y) ** 2 + (5.0 if types[r, c] == CELL_DOOR else 0.0)
                    if d < best_d:
                        best, best_d = (r, c), d
        return best

    def _search(self, x: float, y: float, limit: float | None = None):
        start = self._nearest_walkable(x, y)
        if start is None:
            return None, None, None
        graph = self._build_graph()
        s = start[0] * self.shape[1] + start[1]
        dist, pred = dijkstra(graph, directed=True, indices=s, return_predecessors=True,
                              limit=np.inf if limit is None else limit)
        return start, dist, pred

    def _reconstruct(self, pred: np.ndarray, start_idx: int, goal_idx: int, cost: float,
                     origin: tuple[float, float]) -> Path:
        w = self.shape[1]
        cells = []
        cur = goal_idx
        while cur != start_idx and cur >= 0:
            cells.append(cur)
            cur = pred[cur]
        cells.append(start_idx)
        cells.reverse()
        types = self.cell_types()
        pts = [origin]
        doors: list[int] = []
        for idx in cells:
            r, c = divmod(int(idx), w)
            pts.append(self.to_world(r, c))
            if types[r, c] == CELL_DOOR:
                sid = int(self.cell_sector[r, c])
                if not self.is_closed_door(sid):  # a door face cell: find the closed neighbour
                    sid = self._closed_sector_near(r, c)
                if sid >= 0 and sid not in doors:
                    doors.append(sid)
        return Path(points=pts, cost=float(cost), door_sectors=doors)

    def _closed_sector_near(self, r: int, c: int) -> int:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rr, cc = r + dr, c + dc
                if 0 <= rr < self.shape[0] and 0 <= cc < self.shape[1]:
                    sid = int(self.cell_sector[rr, cc])
                    if sid >= 0 and self.is_closed_door(sid):
                        return sid
        # Face cells can sit just outside a very thin door sector: search the lines.
        wx, wy = self.to_world(r, c)
        best, best_d = -1, 1e18
        for i in np.nonzero(~self.static_block)[0]:
            for sid in (int(self.sec_a[i]), int(self.sec_b[i])):
                if self.is_closed_door(sid):
                    d = _point_segment_dist(wx, wy, self.x1[i], self.y1[i], self.x2[i], self.y2[i])
                    if d < best_d:
                        best, best_d = sid, d
        return best if best_d <= 2 * self.cell else -1

    def find_path(self, start: tuple[float, float], goal: tuple[float, float],
                  goal_radius_cells: int = 3) -> Path | None:
        s_cell, dist, pred = self._search(*start)
        if s_cell is None:
            return None
        gi = self._reachable_goal(dist, goal, goal_radius_cells)
        if gi is None:
            return None
        w = self.shape[1]
        path = self._reconstruct(pred, s_cell[0] * w + s_cell[1], gi, dist[gi], start)
        path.points.append(goal)
        return path

    def _reachable_goal(self, dist: np.ndarray, goal: tuple[float, float], radius_cells: int) -> int | None:
        """Closest *reachable* cell to ``goal`` (the nearest walkable one may be behind a wall)."""
        h, w = self.shape
        r0, c0 = self.to_cell(*goal)
        best, best_d = None, math.inf
        for r in range(max(r0 - radius_cells, 0), min(r0 + radius_cells + 1, h)):
            for c in range(max(c0 - radius_cells, 0), min(c0 + radius_cells + 1, w)):
                i = r * w + c
                if np.isfinite(dist[i]):
                    wx, wy = self.to_world(r, c)
                    d = math.hypot(wx - goal[0], wy - goal[1])
                    if d < best_d:
                        best, best_d = i, d
        return best

    def reachable(self, start: tuple[float, float], goal: tuple[float, float]) -> bool:
        return self.find_path(start, goal) is not None

    def distance_field(self, x: float, y: float) -> "DistanceField | None":
        """Walking distance from (x, y) to everywhere, computed once."""
        start, dist, _ = self._search(x, y)
        return None if start is None else DistanceField(self, dist)

    # ------------------------------------------------------------- exploration
    def mark_seen(self, x: float, y: float, radius: float = 512.0, n_rays: int = 180) -> None:
        types = self.cell_types()
        opaque = (types == CELL_WALL) | (types == CELL_VOID) | (types == CELL_DOOR)
        ang = np.linspace(0.0, 2 * math.pi, n_rays, endpoint=False)
        steps = np.arange(0.0, radius, self.cell * 0.5)
        px = x + np.cos(ang)[:, None] * steps[None, :]
        py = y + np.sin(ang)[:, None] * steps[None, :]
        rr = np.clip(np.floor((py - self.y0) / self.cell).astype(np.int64), 0, self.shape[0] - 1)
        cc = np.clip(np.floor((px - self.x0) / self.cell).astype(np.int64), 0, self.shape[1] - 1)
        blocked = opaque[rr, cc]
        blocked[:, 0] = False  # the player's own cell never blocks
        first = np.where(blocked.any(axis=1), blocked.argmax(axis=1), steps.size - 1)
        mask = np.arange(steps.size)[None, :] <= first[:, None]
        self.seen[rr[mask], cc[mask]] = True
        for f in self.features:
            if not f.seen:
                r, c = self.to_cell(f.x, f.y)
                if self.seen[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2].any():
                    f.seen = True

    def explored_fraction(self) -> float:
        types = self.cell_types()
        interior = (types == CELL_FREE)
        total = int(interior.sum())
        return float((self.seen & interior).sum()) / total if total else 1.0

    def frontier_mask(self) -> np.ndarray:
        types = self.cell_types()
        walk = (types == CELL_FREE) | (types == CELL_DOOR)
        known = walk & self.seen
        unknown = walk & ~self.seen
        return unknown & _dilate4(known)

    def nearest_frontier(self, x: float, y: float, angle_deg: float | None = None,
                         min_dist: float = 48.0) -> Path | None:
        """Shortest path to the closest unexplored-but-reachable spot."""
        s_cell, dist, pred = self._search(x, y)
        if s_cell is None:
            return None
        frontier = self.frontier_mask().ravel()
        for (r, c) in self.blocked_cells:
            frontier[r * self.shape[1] + c] = False
        cand = np.nonzero(frontier & np.isfinite(dist) & (dist >= min_dist))[0]
        if cand.size == 0:
            return None
        score = dist[cand].copy()
        if angle_deg is not None:  # mild preference for what is in front of us
            rr, cc = np.divmod(cand, self.shape[1])
            wx = self.x0 + (cc + 0.5) * self.cell
            wy = self.y0 + (rr + 0.5) * self.cell
            bearing = np.degrees(np.arctan2(wy - y, wx - x)) - angle_deg
            bearing = (bearing + 180.0) % 360.0 - 180.0
            score += np.abs(bearing) / 180.0 * 96.0
        best = int(cand[np.argmin(score)])
        w = self.shape[1]
        return self._reconstruct(pred, s_cell[0] * w + s_cell[1], best, dist[best], (x, y))

    def mark_unreachable(self, x: float, y: float, radius_cells: int = 1) -> None:
        r0, c0 = self.to_cell(x, y)
        for dr in range(-radius_cells, radius_cells + 1):
            for dc in range(-radius_cells, radius_cells + 1):
                self.blocked_cells.add((r0 + dr, c0 + dc))

    # ---------------------------------------------------------------- sensing
    def raycast(self, x: float, y: float, angle_deg: float, max_dist: float = 1024.0) -> tuple[float, str]:
        """Walk along a ray until something blocks a walking player.

        Returns (distance, what) where what is one of: open, wall, door, step.
        """
        types = self.cell_types()
        a = math.radians(angle_deg)
        dx, dy = math.cos(a), math.sin(a)
        step = self.cell * 0.25
        prev_floor = None
        d = 0.0
        while d < max_dist:
            px, py = x + dx * d, y + dy * d
            r, c = self._row(py), self._col(px)
            if not (0 <= r < self.shape[0] and 0 <= c < self.shape[1]):
                return d, "wall"
            t = types[r, c]
            if d > K.PLAYER_RADIUS * 0.5:
                if t in (CELL_WALL, CELL_VOID):
                    return d, "wall"
                if t == CELL_DOOR:
                    return d, "door"
            sid = self.cell_sector[r, c]
            if sid >= 0:
                fl = self.floor_h[sid]
                if prev_floor is not None and fl - prev_floor > K.MAX_STEP_HEIGHT:
                    return d, "step"
                prev_floor = fl
            d += step
        return max_dist, "open"

    def features_of(self, kind: str) -> list[Feature]:
        return [f for f in self.features if f.kind == kind]


class DistanceField:
    def __init__(self, nav: NavMap, dist: np.ndarray):
        self.nav = nav
        self.dist = dist.reshape(nav.shape)

    def at(self, x: float, y: float, radius_cells: int = 2) -> float | None:
        """Walking distance to (x, y); None if it cannot be reached."""
        r0, c0 = self.nav.to_cell(x, y)
        h, w = self.nav.shape
        block = self.dist[max(r0 - radius_cells, 0):min(r0 + radius_cells + 1, h),
                          max(c0 - radius_cells, 0):min(c0 + radius_cells + 1, w)]
        best = float(block.min()) if block.size else math.inf
        return best if math.isfinite(best) else None


# ---------------------------------------------------------------------- utils
def _bbox_area(segs: np.ndarray) -> float:
    if len(segs) == 0:
        return 0.0
    xs = np.concatenate([segs[:, 0], segs[:, 2]])
    ys = np.concatenate([segs[:, 1], segs[:, 3]])
    return float((xs.max() - xs.min()) * (ys.max() - ys.min()))


def _points_in_polygon(px: np.ndarray, py: np.ndarray, segs: np.ndarray, chunk: int = 4096) -> np.ndarray:
    """Even-odd rule against an unordered set of edges (works with holes)."""
    x1, y1, x2, y2 = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    out = np.zeros(px.size, dtype=bool)
    dy = np.where(y2 == y1, 1e-12, y2 - y1)
    for s in range(0, px.size, chunk):
        qx, qy = px[s:s + chunk, None], py[s:s + chunk, None]
        straddle = (y1[None, :] > qy) != (y2[None, :] > qy)
        xint = x1[None, :] + (qy - y1[None, :]) * (x2 - x1)[None, :] / dy[None, :]
        out[s:s + chunk] = ((straddle & (qx < xint)).sum(axis=1) % 2) == 1
    return out


def _dilate(mask: np.ndarray) -> np.ndarray:
    """8-neighbourhood binary dilation."""
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    out[1:, 1:] |= mask[:-1, :-1]
    out[1:, :-1] |= mask[:-1, 1:]
    out[:-1, 1:] |= mask[1:, :-1]
    out[:-1, :-1] |= mask[1:, 1:]
    return out


def _dilate4(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def _point_segment_dist(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
