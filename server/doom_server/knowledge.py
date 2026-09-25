"""Static Doom knowledge: monsters, items, weapons and map line specials.

Everything the server tells an agent about "what a thing is" comes from here, so
new monsters/items only need an entry in these tables.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Player physics (Doom units / tics). One tic = 1/35 s.
# ---------------------------------------------------------------------------
TICRATE = 35
PLAYER_RADIUS = 16.0
PLAYER_HEIGHT = 56.0
MAX_STEP_HEIGHT = 24.0
USE_RANGE = 64.0


@dataclass(frozen=True)
class MonsterInfo:
    threat: int  # 1 (weak) .. 5 (boss)
    attack: str  # hitscan | projectile | melee | charge | summoner
    note: str


MONSTERS: dict[str, MonsterInfo] = {
    "Zombieman": MonsterInfo(1, "hitscan", "weak zombie with a rifle"),
    "ShotgunGuy": MonsterInfo(2, "hitscan", "shotgun zombie, deadly up close"),
    "ChaingunGuy": MonsterInfo(3, "hitscan", "chaingun zombie, kill it fast"),
    "WolfensteinSS": MonsterInfo(2, "hitscan", "SS soldier with a machine gun"),
    "DoomImp": MonsterInfo(2, "projectile", "throws fireballs, sidestep them"),
    "Demon": MonsterInfo(2, "melee", "fast biter, shoot it before it reaches you"),
    "Spectre": MonsterInfo(2, "melee", "invisible biter, shoot it before it reaches you"),
    "LostSoul": MonsterInfo(2, "charge", "flying skull that charges at you"),
    "Cacodemon": MonsterInfo(3, "projectile", "tough floating demon with lightning balls"),
    "HellKnight": MonsterInfo(3, "projectile", "tough, throws green fireballs"),
    "BaronOfHell": MonsterInfo(4, "projectile", "very tough, avoid or use strong weapons"),
    "Arachnotron": MonsterInfo(3, "projectile", "spider robot with a plasma gun"),
    "PainElemental": MonsterInfo(3, "summoner", "spits lost souls, kill it quickly"),
    "Revenant": MonsterInfo(3, "projectile", "skeleton with homing rockets, take cover"),
    "Fatso": MonsterInfo(4, "projectile", "mancubus, fires volleys of fireballs"),
    "Archvile": MonsterInfo(5, "hitscan", "revives monsters and burns you, top priority"),
    "Cyberdemon": MonsterInfo(5, "projectile", "boss with rockets, avoid unless well armed"),
    "SpiderMastermind": MonsterInfo(5, "hitscan", "boss with a super chaingun"),
    "CommanderKeen": MonsterInfo(1, "none", "harmless hanging figure"),
    "BossBrain": MonsterInfo(5, "none", "the final boss brain"),
    # Custom actors used by the bundled ViZDoom scenarios.
    "MarineChainsawVzd": MonsterInfo(2, "melee", "chainsaw marine, keep your distance"),
}
DEFAULT_MONSTER = MonsterInfo(2, "unknown", "hostile")


@dataclass(frozen=True)
class ItemInfo:
    kind: str  # health | armor | ammo | weapon | key | powerup
    label: str
    value: int = 0  # rough usefulness, higher = better


ITEMS: dict[str, ItemInfo] = {
    # health
    "HealthBonus": ItemInfo("health", "health bonus (+1)", 1),
    "Stimpack": ItemInfo("health", "stimpack (+10 health)", 3),
    "Medikit": ItemInfo("health", "medikit (+25 health)", 5),
    "Soulsphere": ItemInfo("health", "soulsphere (+100 health)", 9),
    "Megasphere": ItemInfo("health", "megasphere (200 health + armor)", 10),
    # armor
    "ArmorBonus": ItemInfo("armor", "armor bonus (+1)", 1),
    "GreenArmor": ItemInfo("armor", "green armor (100)", 6),
    "BlueArmor": ItemInfo("armor", "blue armor (200)", 8),
    # ammo
    "Clip": ItemInfo("ammo", "bullet clip", 2),
    "ClipBox": ItemInfo("ammo", "box of bullets", 4),
    "Shell": ItemInfo("ammo", "shotgun shells", 3),
    "ShellBox": ItemInfo("ammo", "box of shells", 5),
    "RocketAmmo": ItemInfo("ammo", "rocket", 3),
    "RocketBox": ItemInfo("ammo", "box of rockets", 5),
    "Cell": ItemInfo("ammo", "energy cell", 3),
    "CellPack": ItemInfo("ammo", "energy cell pack", 5),
    "Backpack": ItemInfo("ammo", "backpack (ammo)", 6),
    # weapons
    "Chainsaw": ItemInfo("weapon", "chainsaw", 5),
    "Shotgun": ItemInfo("weapon", "shotgun", 8),
    "SuperShotgun": ItemInfo("weapon", "super shotgun", 9),
    "Chaingun": ItemInfo("weapon", "chaingun", 8),
    "RocketLauncher": ItemInfo("weapon", "rocket launcher", 9),
    "PlasmaRifle": ItemInfo("weapon", "plasma rifle", 9),
    "BFG9000": ItemInfo("weapon", "BFG 9000", 10),
    # keys
    "BlueCard": ItemInfo("key", "blue keycard", 10),
    "YellowCard": ItemInfo("key", "yellow keycard", 10),
    "RedCard": ItemInfo("key", "red keycard", 10),
    "BlueSkull": ItemInfo("key", "blue skull key", 10),
    "YellowSkull": ItemInfo("key", "yellow skull key", 10),
    "RedSkull": ItemInfo("key", "red skull key", 10),
    # powerups
    "Berserk": ItemInfo("powerup", "berserk pack", 6),
    "InvulnerabilitySphere": ItemInfo("powerup", "invulnerability", 9),
    "BlurSphere": ItemInfo("powerup", "partial invisibility", 5),
    "RadSuit": ItemInfo("powerup", "radiation suit", 4),
    "Allmap": ItemInfo("powerup", "computer map", 3),
    "Infrared": ItemInfo("powerup", "light amplification", 2),
}

ITEM_CATEGORIES = {"Health", "Armor", "Ammo", "Weapon", "Key", "Powerup"}

KEY_COLORS = {
    "BlueCard": "blue",
    "BlueSkull": "blue",
    "YellowCard": "yellow",
    "YellowSkull": "yellow",
    "RedCard": "red",
    "RedSkull": "red",
}

# Explosive-category labels that are NOT dangerous projectiles.
HARMLESS_EFFECTS = {"BulletPuff", "Blood", "TeleportFog", "ItemFog", "SpawnFire"}


def monster_info(name: str) -> MonsterInfo:
    return MONSTERS.get(name, DEFAULT_MONSTER)


def item_info(name: str, category: str = "") -> ItemInfo:
    info = ITEMS.get(name)
    if info is not None:
        return info
    kind = category.lower() if category in ITEM_CATEGORIES else "item"
    return ItemInfo(kind, name, 1)


# ---------------------------------------------------------------------------
# Weapons. ViZDoom exposes WEAPONn / AMMOn per weapon *slot*.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WeaponSlot:
    slot: int
    name: str
    ammo: str | None  # ammo type name, None = melee
    min_range: float = 0.0  # don't fire closer than this (splash damage)
    max_range: float = 4096.0  # beyond this the weapon is ineffective


WEAPON_SLOTS: dict[int, WeaponSlot] = {
    1: WeaponSlot(1, "fist", None, max_range=64.0),
    2: WeaponSlot(2, "pistol", "bullets"),
    3: WeaponSlot(3, "shotgun", "shells", max_range=1200.0),
    4: WeaponSlot(4, "chaingun", "bullets"),
    5: WeaponSlot(5, "rocket_launcher", "rockets", min_range=160.0),
    6: WeaponSlot(6, "plasma_rifle", "cells"),
    7: WeaponSlot(7, "bfg", "cells", min_range=160.0),
}
WEAPON_BY_NAME = {w.name: w for w in WEAPON_SLOTS.values()}
# Preferred order when auto-selecting the best weapon for a fight.
WEAPON_PREFERENCE = [6, 4, 3, 5, 2, 7, 1]

# ---------------------------------------------------------------------------
# Vanilla Doom line specials (binary Doom-format maps).
# ---------------------------------------------------------------------------
# Doors opened by pressing USE on the door itself -> key colour needed (or None).
DOOM_MANUAL_DOORS: dict[int, str | None] = {
    1: None,
    26: "blue",
    27: "yellow",
    28: "red",
    31: None,
    32: "blue",
    33: "red",
    34: "yellow",
    117: None,
    118: None,
}
# Switches: activated with USE, they do something elsewhere (doors, lifts, stairs...).
DOOM_SWITCHES: set[int] = {
    7, 9, 14, 15, 18, 20, 21, 23, 29, 41, 42, 43, 45, 49, 50, 55, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 69, 70, 71, 99, 101, 102, 103, 111, 112, 113, 114, 115, 116, 122,
    123, 127, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140,
}
DOOM_LOCKED_SWITCHES: dict[int, str] = {
    99: "blue", 133: "blue", 134: "red", 135: "red", 136: "yellow", 137: "yellow",
}
DOOM_EXITS: dict[int, str] = {
    11: "switch",
    51: "switch",  # secret exit
    52: "walk",
    124: "walk",  # secret exit
    197: "shoot",
    198: "shoot",
}
DOOM_SECRET_EXITS = {51, 124, 198}
DOOM_TELEPORTS = {39, 97, 174, 195, 207, 208, 209, 210}

# Hexen-format / UDMF (ZDoom action specials) equivalents.
HEXEN_DOORS = {10, 11, 12, 13, 202}  # Door_Close/Open/Raise/LockedRaise, Generic_Door
HEXEN_EXITS = {243: "normal", 244: "secret"}  # Exit_Normal, Exit_Secret
HEXEN_TELEPORTS = {70, 71}
HEXEN_LOCK_COLORS = {1: "red", 2: "blue", 3: "yellow", 4: "red", 5: "blue", 6: "yellow",
                     129: "red", 130: "blue", 131: "yellow", 132: "red", 133: "blue", 134: "yellow"}

# Damaging floor specials (vanilla sector types).
DAMAGING_SECTOR_SPECIALS = {4, 5, 7, 11, 16}
