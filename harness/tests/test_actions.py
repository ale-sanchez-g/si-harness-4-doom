from conftest import enemy, item

from doom_harness.actions import TurnView


def test_tags_follow_distance_order(obs_enemy):
    view = TurnView(obs_enemy)
    assert [t.tag for t in view.enemies] == ["E1"]
    assert view.enemies[0].info["name"] == "Zombieman"
    assert [t.tag for t in view.items] == ["I1", "I2"]
    dists = [t.info["path_distance"] for t in view.items]
    assert dists == sorted(dists)


def test_attack_and_pickup_only_when_possible(make_obs):
    calm = TurnView(make_obs(enemies=[], items=[], known_items=[]))
    assert "attack" not in calm.action_names
    assert "pickup" not in calm.action_names
    assert "explore" in calm.action_names
    # nothing to flee from: small models otherwise "retreat" from an empty room
    assert "retreat" not in calm.action_names and "dodge" not in calm.action_names
    fireball = {"id": 9, "name": "DoomImpBall", "distance": 120, "bearing": 0.0, "incoming": True}
    assert "dodge" in TurnView(make_obs(enemies=[], projectiles=[fireball])).action_names
    busy = TurnView(make_obs(enemies=[enemy()], items=[item()]))
    assert {"attack", "pickup", "retreat", "dodge"} <= set(busy.action_names)


def test_useless_and_unreachable_items_are_hidden(make_obs):
    view = TurnView(make_obs(items=[item(id_=1, useful=False), item(id_=2, path_distance=None),
                                    item(id_=3)], known_items=[]))
    assert [t.id for t in view.items] == [3]


def test_banned_items_are_hidden(make_obs):
    view = TurnView(make_obs(items=[item(id_=1), item(id_=2, path_distance=400)], known_items=[]),
                    banned_ids={1})
    assert [t.id for t in view.items] == [2]
    assert view.items[0].tag == "I1"


def test_scenario_restricts_actions(obs_basic):
    view = TurnView(obs_basic)
    # basic only allows attack/move/face/wait on the server
    assert set(view.action_names) <= {"attack", "move", "wait"}
    assert "explore" not in view.action_names


def test_schema_enums_match_view(obs_enemy):
    view = TurnView(obs_enemy)
    schema = view.schema(reasoning=True)
    # Ollama sorts keys alphabetically; "Thought" must still come first.
    assert schema["required"] == ["Thought", "action", "arg"]
    assert sorted(schema["properties"]) == list(schema["properties"])
    assert schema["properties"]["Thought"]["maxLength"] == 300  # the action can never be crowded out
    assert schema["properties"]["action"]["enum"] == view.action_names
    args = schema["properties"]["arg"]["enum"]
    assert args[0] == "none" and "E1" in args and "I1" in args and "around" in args
    assert "Thought" not in view.schema(reasoning=False)["properties"]


def test_resolve_maps_tags_to_object_ids(obs_enemy):
    view = TurnView(obs_enemy)
    r = view.resolve({"action": "attack", "arg": "e1"}, attack_seconds=3.0)
    assert r.command == {"command": "attack", "target_id": obs_enemy["enemies"][0]["id"], "duration": 3.0}
    r = view.resolve({"action": "pickup", "arg": "I2"})
    assert r.command["command"] == "goto" and r.command["target_id"] == view.items[1].id


def test_resolve_repairs_bad_arguments(obs_enemy):
    view = TurnView(obs_enemy)
    r = view.resolve({"action": "attack", "arg": "left"})
    assert r.arg == "E1" and r.notes
    r = view.resolve({"action": "turn", "arg": "E1"})
    assert r.command == {"command": "turn", "direction": "around"}
    r = view.resolve({"action": "move", "arg": "backward"})
    assert r.command["direction"] == "backward" and r.command["distance"] > 0


def test_resolve_synonyms_and_rejects_impossible(make_obs):
    view = TurnView(make_obs(enemies=[enemy()], items=[], known_items=[]))
    assert view.resolve({"action": "shoot", "arg": "E1"}).action == "attack"
    assert view.resolve({"action": "open"}).action == "use"
    assert view.resolve({"action": "pickup", "arg": "I1"}) is None  # no items listed
    assert view.resolve({"action": "fly"}) is None


def test_barrels_next_to_enemies_are_targets(make_obs):
    barrel = {"id": 55, "name": "ExplosiveBarrel", "distance": 400, "bearing": 5.0, "enemies_nearby": 2}
    view = TurnView(make_obs(enemies=[enemy()], hazards=[barrel]))
    assert [t.tag for t in view.enemies] == ["E1", "B1"]
    r = view.resolve({"action": "attack", "arg": "B1"})
    assert r.command["target_id"] == 55


def test_switch_weapon_needs_two_weapons(make_obs, obs_start):
    assert "switch_weapon" not in TurnView(obs_start).action_names  # fist + pistol only
    player = dict(obs_start["player"])
    player["weapons"] = player["weapons"] + [{"slot": 3, "name": "shotgun", "ammo": 8, "usable": True}]
    view = TurnView(make_obs(player=player))
    assert "switch_weapon" in view.action_names
    r = view.resolve({"action": "switch_weapon", "arg": "bfg"})
    assert r.command == {"command": "select_weapon", "weapon": "shotgun"}


def test_attack_requires_a_weapon_that_can_fire(make_obs, obs_start):
    empty = dict(obs_start["player"], weapons=[{"slot": 2, "name": "pistol", "ammo": 0, "usable": False}])
    view = TurnView(make_obs(enemies=[enemy()], player=empty))
    assert "attack" not in view.action_names
    assert "explore" in view.action_names
