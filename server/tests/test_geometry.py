import math
import os
from types import SimpleNamespace as NS

import pytest
import vizdoom as vzd

from doom_server.navigation import CELL_DOOR, CELL_FREE, CELL_WALL, NavMap
from doom_server.perception import aim_tolerance, bearing_to, norm_angle
from doom_server.wad import MapInfo, MapLine, load_map_info

WAD_DIR = os.path.dirname(vzd.__file__)


# -------------------------------------------------------------------- WADs
def test_freedoom2_map01_specials():
    info = load_map_info([os.path.join(WAD_DIR, "freedoom2.wad")], "MAP01")
    assert info is not None and info.format == "doom"
    assert len(info.lines) == 1274
    [exit_line] = info.exits
    assert exit_line.exit_type == "switch"
    assert (exit_line.x1, exit_line.y1, exit_line.x2, exit_line.y2) == (184, -632, 248, -632)
    assert sum(1 for ln in info.lines if ln.kind == "door") == 10


def test_udmf_scenario_parses():
    info = load_map_info([os.path.join(vzd.scenarios_path, "my_way_home.wad")], "MAP01")
    assert info is not None and info.format == "udmf" and len(info.lines) > 50


def test_missing_map_returns_none():
    assert load_map_info([os.path.join(WAD_DIR, "freedoom2.wad")], "MAP99") is None


# -------------------------------------------------------------- navigation
def _sector(sid, lines, floor=0.0, ceil=128.0):
    return NS(id=sid, floor_height=floor, ceiling_height=ceil,
              lines=[NS(x1=a, y1=b, x2=c, y2=d, is_blocking=blk) for a, b, c, d, blk in lines])


def two_rooms(door_ceiling=0.0, locked=None):
    """Room A (x 0..256) | door sector (x 256..272, y 96..160) | room B (x 272..528)."""
    a = _sector(0, [(0, 0, 256, 0, True), (256, 0, 256, 96, True), (256, 96, 256, 160, False),
                    (256, 160, 256, 256, True), (256, 256, 0, 256, True), (0, 256, 0, 0, True)])
    door = _sector(1, [(256, 96, 256, 160, False), (256, 96, 272, 96, True), (272, 96, 272, 160, False),
                       (272, 160, 256, 160, True)], ceil=door_ceiling)
    b = _sector(2, [(272, 0, 528, 0, True), (528, 0, 528, 256, True), (528, 256, 272, 256, True),
                    (272, 256, 272, 160, True), (272, 160, 272, 96, False), (272, 96, 272, 0, True)])
    info = MapInfo("TEST", "doom", lines=[
        MapLine(256, 96, 256, 160, 26 if locked else 1, 0, 0, 1, kind="door", key=locked),
        # Doom convention: the front side is on the right of v1 -> v2 (here: north, into room B).
        MapLine(460, 0, 400, 0, 11, 0, 2, None, kind="exit", exit_type="switch"),
    ])
    return [a, door, b], info


def test_grid_classifies_cells():
    sectors, info = two_rooms()
    nav = NavMap(sectors, info)
    types = nav.cell_types()
    assert types[nav.to_cell(128, 128)] == CELL_FREE
    assert types[nav.to_cell(400, 128)] == CELL_FREE
    assert types[nav.to_cell(264, 128)] == CELL_DOOR  # closed door we may open
    assert types[nav.to_cell(-100, -100)] != CELL_FREE
    assert nav.sector_at(128, 128) == 0 and nav.sector_at(400, 128) == 2


def test_path_through_closed_door():
    sectors, info = two_rooms()
    nav = NavMap(sectors, info)
    path = nav.find_path((64, 128), (460, 128))
    assert path is not None
    assert path.door_sectors == [1]
    assert path.points[0] == (64, 128) and path.points[-1] == (460, 128)
    assert 380 < path.length < 520


def test_open_door_is_free_and_failed_door_blocks():
    sectors, info = two_rooms(door_ceiling=128.0)
    nav = NavMap(sectors, info)
    path = nav.find_path((64, 128), (460, 128))
    assert path is not None and path.door_sectors == []
    sectors, info = two_rooms()
    nav = NavMap(sectors, info)
    nav.failed_doors.add(1)
    assert nav.find_path((64, 128), (460, 128)) is None


def test_locked_door_needs_key():
    sectors, info = two_rooms(locked="blue")
    nav = NavMap(sectors, info)
    assert nav.find_path((64, 128), (460, 128)) is None
    nav.keys = {"blue"}
    assert nav.find_path((64, 128), (460, 128)) is not None


def test_damaging_sectors():
    sectors, info = two_rooms(door_ceiling=128.0)
    info.sector_specials = {2: 5}  # 10% damage floor in room B
    nav = NavMap(sectors, info)
    assert nav.is_damaging(2) and not nav.is_damaging(0) and not nav.is_damaging(99)
    path = nav.find_path((64, 128), (460, 128))
    assert path is not None  # still reachable, just expensive


def test_steps_are_directional():
    # Room B is a 40-unit high platform: you can drop down but not climb up.
    sectors, info = two_rooms(door_ceiling=128.0)
    sectors[1].floor_height = sectors[2].floor_height = 40.0
    sectors[1].ceiling_height = sectors[2].ceiling_height = 168.0
    nav = NavMap(sectors, info)
    assert nav.find_path((64, 128), (460, 128)) is None
    assert nav.find_path((460, 128), (64, 128)) is not None


def test_exploration_and_raycast():
    sectors, info = two_rooms()
    nav = NavMap(sectors, info)
    nav.mark_seen(64, 128)
    assert 0.3 < nav.explored_fraction() < 0.7  # the closed door hides room B
    frontier = nav.nearest_frontier(64, 128, angle_deg=0)
    assert frontier is not None and frontier.points[-1][0] > 250
    dist, what = nav.raycast(64, 128, 0.0)
    assert what == "door" and 180 < dist < 210
    dist, what = nav.raycast(64, 128, 90.0)
    assert what == "wall" and 110 < dist < 140
    [exit_feature] = nav.features_of("exit")
    assert not exit_feature.seen
    assert exit_feature.approach[1] > 0  # approach from the front (inside room B)


def test_switch_approach_falls_back_to_the_walkable_side():
    sectors, info = two_rooms()
    info.lines[1] = MapLine(400, 0, 460, 0, 11, 0, 2, None, kind="exit", exit_type="switch")  # reversed
    [exit_feature] = NavMap(sectors, info).features_of("exit")
    assert exit_feature.approach[1] > 0


def test_real_map_has_route_to_exit():
    g = vzd.DoomGame()
    g.load_config(os.path.join(vzd.scenarios_path, "freedoom2.cfg"))
    g.set_window_visible(False)
    g.set_sound_enabled(False)
    g.set_audio_buffer_enabled(False)
    g.set_sectors_info_enabled(True)
    g.init()
    try:
        info = load_map_info([os.path.join(WAD_DIR, "freedoom2.wad")], "MAP01")
        nav = NavMap(g.get_state().sectors, info)
        [ex] = nav.features_of("exit")
        path = nav.find_path((-192, -192), ex.approach)
        assert path is not None and len(path.door_sectors) >= 1
    finally:
        g.close()


# ------------------------------------------------------------------ angles
def test_bearing_convention():
    assert bearing_to(0, 0, 0, 100, 0) == pytest.approx(0)
    assert bearing_to(0, 0, 0, 0, 100) == pytest.approx(90)  # positive = left
    assert bearing_to(0, 0, 90, 100, 0) == pytest.approx(-90)
    assert norm_angle(270) == pytest.approx(-90)
    assert abs(bearing_to(0, 0, 10, -100, 0)) == pytest.approx(170)
    assert aim_tolerance(100) > aim_tolerance(1000) >= 1.5
    assert math.isclose(norm_angle(-190), 170)


def test_frontier_behind_a_door_and_marking():
    """Standing at a closed door, the unexplored room behind it is the frontier (even
    though it is very close); marking it explored removes it; bad marks are ignored."""
    sectors, info = two_rooms()
    nav = NavMap(sectors, info)
    nav.mark_seen(240, 128)  # standing right in front of the door
    path = nav.nearest_frontier(240, 128, angle_deg=0)
    assert path is not None and path.door_sectors == [1]
    gx, gy = path.points[-1]
    assert gx > 272  # behind the door, in room B
    nav.mark_explored(gx, gy, radius_cells=1)
    assert not nav.frontier_mask()[nav.to_cell(gx, gy)]
    nav.mark_unreachable(-10_000, -10_000, radius_cells=3)
    assert all(0 <= r < nav.shape[0] and 0 <= c < nav.shape[1] for r, c in nav.blocked_cells)
