"""Top-down map rendering for the web viewer (walls, explored area, path, player)."""

from __future__ import annotations

import io
import math
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw

from . import knowledge as K

if TYPE_CHECKING:
    from .session import DoomSession

KEY_RGB = {"blue": (70, 110, 255), "red": (235, 60, 60), "yellow": (240, 210, 40), None: (230, 140, 30)}


def render_map(session: "DoomSession", size: int = 640) -> bytes:
    nav, snap, tr = session.nav, session.snapshot, session.tracker
    if nav is None or snap is None or tr is None:
        img = Image.new("RGB", (size, size // 2), (16, 16, 20))
        ImageDraw.Draw(img).text((10, 10), "no episode running", fill=(200, 200, 200))
        return _png(img)

    minx, maxx = float(nav.lx.min()), float(nav.lx.max())
    miny, maxy = float(nav.ly.min()), float(nav.ly.max())
    span = max(maxx - minx, maxy - miny, 1.0)
    scale = (size - 24) / span
    width = int((maxx - minx) * scale) + 24
    height = int((maxy - miny) * scale) + 24

    def P(x: float, y: float) -> tuple[float, float]:
        return 12 + (x - minx) * scale, height - 12 - (y - miny) * scale

    img = Image.new("RGB", (width, height), (14, 14, 18))

    # Explored area as a translucent overlay built from the navigation grid.
    h, w = nav.shape
    grid = np.zeros((h, w, 4), dtype=np.uint8)
    grid[nav.inside] = (40, 40, 48, 255)
    grid[nav.seen & nav.inside] = (38, 74, 52, 255)
    overlay = Image.fromarray(grid[::-1], "RGBA")
    ow, oh = max(1, int(round(w * nav.cell * scale))), max(1, int(round(h * nav.cell * scale)))
    overlay = overlay.resize((ow, oh), Image.NEAREST)
    ox, oy = P(nav.x0, nav.y0 + h * nav.cell)
    img.paste(overlay, (int(round(ox)), int(round(oy))), overlay)

    d = ImageDraw.Draw(img)
    for i in range(len(nav.x1)):
        color = (190, 190, 200) if nav.static_block[i] else (85, 85, 100)
        d.line([P(nav.x1[i], nav.y1[i]), P(nav.x2[i], nav.y2[i])], fill=color, width=1)

    full = session.settings.knowledge == "full"
    for f in nav.features:
        if not (f.seen or full):
            continue
        if f.kind == "door" and f.sector is not None and nav.is_closed_door(f.sector):
            d.line([P(f.x1, f.y1), P(f.x2, f.y2)], fill=KEY_RGB.get(f.key, KEY_RGB[None]), width=3)
        elif f.kind == "switch":
            x, y = P(f.x, f.y)
            d.rectangle([x - 3, y - 3, x + 3, y + 3], outline=(200, 80, 220) if not f.used else (90, 60, 100))
        elif f.kind == "exit":
            x, y = P(f.x, f.y)
            d.rectangle([x - 5, y - 5, x + 5, y + 5], fill=(40, 230, 90))

    # The game thread may be mutating these while we draw: work on copies.
    trail, path = list(tr.trail), list(session.last_path)
    seen_items, present = list(tr.seen_items.items()), set(tr.items)
    if len(trail) > 1:
        d.line([P(x, y) for x, y in trail], fill=(60, 110, 200), width=1)
    if len(path) > 1:
        d.line([P(x, y) for x, y in path], fill=(0, 220, 255), width=2)

    for oid, it in seen_items:
        if oid in present:
            x, y = P(it["x"], it["y"])
            color = (240, 220, 60) if K.item_info(it["name"]).kind != "key" else KEY_RGB.get(
                K.KEY_COLORS.get(it["name"]), (255, 255, 255))
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=color)
    for lab in snap.labels:
        if lab.object_category == "Monster":
            x, y = P(lab.object_position_x, lab.object_position_y)
            d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(240, 40, 40))

    # Player: a triangle pointing where we look.
    x, y = P(snap.x, snap.y)
    a = math.radians(snap.angle)
    r = 8
    tip = (x + math.cos(a) * r * 1.4, y - math.sin(a) * r * 1.4)
    left = (x + math.cos(a + 2.5) * r, y - math.sin(a + 2.5) * r)
    right = (x + math.cos(a - 2.5) * r, y - math.sin(a - 2.5) * r)
    d.polygon([tip, left, right], fill=(255, 80, 60), outline=(255, 255, 255))
    d.text((8, 6), f"{session.map}  explored {nav.explored_fraction() * 100:.0f}%", fill=(210, 210, 220))
    return _png(img)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
