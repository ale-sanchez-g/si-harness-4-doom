from conftest import enemy, item

from doom_harness.actions import TurnView
from doom_harness.config import HarnessConfig
from doom_harness.memory import Memory, StepRecord
from doom_harness.prompts import load_playbook, situation_report, system_prompt, where


def test_where_phrasing():
    assert where(0, 123) == "120 away, straight ahead"
    assert where(40, 300) == "300 away, 40 deg to your left"
    assert where(-40, 300) == "300 away, 40 deg to your right"
    assert where(170, 55) == "60 away, behind you"


def test_system_prompt_fills_every_placeholder(obs_enemy, obs_basic):
    playbook = load_playbook(HarnessConfig().playbook_path())
    assert "Rules in order" in playbook.reminder and "NOW" in playbook.reminder
    text = system_prompt(playbook, obs_enemy)
    assert "TURN REMINDER" not in text
    for placeholder in ("{goal}", "{actions}", "{answer_format}", "{scenario_notes}"):
        assert placeholder not in text
    assert obs_enemy["episode"]["goal"] in text
    assert "- attack E#" in text
    basic = system_prompt(playbook, obs_basic)
    assert "explore:" not in basic  # not allowed in the basic scenario
    assert "One monster" in basic  # scenario notes were merged


def test_situation_report_contents(obs_enemy):
    memory = Memory()
    memory.add(StepRecord(1, "explore", "none", "interrupted", "enemy spotted: Zombieman", moved=300))
    text = situation_report(TurnView(obs_enemy), memory, turn=2)
    assert text.startswith("TURN 2 |")
    assert "E1 Zombieman" in text and "ENEMIES IN VIEW: 1 (attack them)" in text
    assert "YOU: health 100 (good)," in text
    assert "I1 bullet clip" in text and "USEFUL ITEMS: 2 (" in text
    assert "T1 explore -> interrupted: enemy spotted: Zombieman" in text
    # history comes before the current state, which starts with NOW:
    assert text.index("YOUR LAST TURNS") < text.index("NOW:") < text.index("ENEMIES IN VIEW: 1")
    assert "Available actions now: attack, pickup, explore" in text
    assert "+map01" not in text  # level title banner filtered out


def test_just_happened_only_shows_the_last_command(obs_enemy):
    memory = Memory()
    obs = dict(obs_enemy, recent_events=[{"tic": 1, "type": "kill", "text": "Zombieman died"}])
    assert "JUST HAPPENED" not in situation_report(TurnView(obs), memory, 3)
    memory.last_events = [{"tic": 5, "type": "kill", "text": "Zombieman died"},
                          {"tic": 5, "type": "message", "text": "You need a blue key to open this door"},
                          {"tic": 5, "type": "pickup", "text": "picked up bullet clip"},
                          {"tic": 5, "type": "message", "text": "Picked up some bullets."}]
    text = situation_report(TurnView(obs), memory, 3)
    assert "JUST HAPPENED: You need a blue key to open this door; Picked up some bullets." in text
    assert "Zombieman died" not in text


def test_danger_and_locked_doors_are_reported(make_obs):
    obs = make_obs(enemies=[enemy()],
                   projectiles=[{"id": 9, "name": "DoomImpBall", "distance": 120, "bearing": 2.0, "incoming": True}],
                   doors=[{"distance": 200, "bearing": 30.0, "key": "blue", "locked": True, "x": 0, "y": 0}])
    text = situation_report(TurnView(obs), Memory(), 5)
    assert "DANGER: a DoomImpBall is flying at you" in text
    assert "LOCKED, you need the blue key" in text
    assert "HINT: A projectile is flying at you: dodge!" in text
    assert "HINT: A blue door is locked" in text


def test_memory_detects_stuck_and_repeats(make_obs):
    m = Memory()
    for t in range(1, 4):
        m.add(StepRecord(t, "explore", "none", "failed", "stuck", moved=5))
    assert m.is_stuck()
    assert any("stuck" in h for h in m.hints(make_obs(enemies=[])))
    m.add(StepRecord(4, "pickup", "I1", "failed", "no path", target_id=77))
    m.add(StepRecord(5, "pickup", "I1", "failed", "no path", target_id=77))
    assert m.repeated_failure() is not None
    assert 77 in m.banned_ids(turn=6)
    assert 77 not in m.banned_ids(turn=40)


def test_hurt_by_invisible_enemy_hint(make_obs):
    m = Memory()
    m.add(StepRecord(1, "explore", "none", "interrupted", "took 10 damage", damage_taken=10))
    hints = m.hints(make_obs(enemies=[]))
    assert any("turn around" in h.lower() for h in hints)
    assert not any("behind" in h for h in m.hints(make_obs(enemies=[enemy()])))


def test_exit_hint(make_obs):
    obs = make_obs(enemies=[], exit={"distance": 500, "bearing": 10, "path_distance": 800, "type": "switch",
                                     "secret": False, "x": 0, "y": 0})
    assert any("goto_exit" in h for h in Memory().hints(obs))
    assert "EXIT: 500 away" in situation_report(TurnView(obs), Memory(), 1)
    assert "goto_exit" in TurnView(obs).action_names
    obs["items"] = [item()]
    assert "goto_exit" in TurnView(obs).action_names


def test_futile_attack_hint(make_obs):
    m = Memory()
    for t in range(1, 4):
        m.add(StepRecord(t, "attack", "E1", "completed", "fired 4 shots, it is still alive", damage_dealt=0))
    assert any("did no damage" in h for h in m.hints(make_obs()))
    m.add(StepRecord(4, "attack", "E1", "completed", "fired 2 shots", damage_dealt=15))
    assert not any("did no damage" in h for h in m.hints(make_obs()))


def test_damaging_floor_hint_replaces_turn_around(make_obs, obs_start):
    m = Memory()
    m.add(StepRecord(1, "explore", "none", "completed", "explored", damage_taken=10))
    hints = m.hints(make_obs(enemies=[], player={**obs_start["player"], "on_damaging_floor": True}))
    assert any("damaging floor" in h for h in hints)
    assert not any("Turn around" in h for h in hints)


def test_playbook_front_matter_restricts_actions(obs_enemy, make_obs):
    from doom_harness.prompts import Playbook
    pb = Playbook.parse("---\nactions: attack, explore\n---\n# ROLE\nplay {goal}\n{actions}\n# TURN REMINDER\nbe brave")
    assert pb.actions == ["attack", "explore"] and pb.reminder == "be brave"
    assert "actions:" not in pb.template and pb.template.startswith("# ROLE")
    view = TurnView(obs_enemy, allowed=pb.actions)
    assert view.action_names == ["attack", "explore"]
    assert set(view.schema()["properties"]["action"]["enum"]) == {"attack", "explore"}
    prompt = system_prompt(pb, obs_enemy)
    assert "- attack E#" in prompt and "pickup" not in prompt
    # a menu with nothing available this turn falls back to what the scenario allows
    calm = TurnView(make_obs(enemies=[], items=[], known_items=[]), allowed=["attack", "pickup"])
    assert "explore" in calm.action_names


def test_small_playbook_loads():
    cfg = HarnessConfig(playbook="small")
    pb = load_playbook(cfg.playbook_path())
    assert pb.actions == ["attack", "pickup", "explore", "goto_exit", "retreat", "dodge", "turn"]
    # no TURN REMINDER on purpose: sub-1B models copy it instead of reading the FACTS line
    assert pb.facts and pb.reminder == ""
    assert not load_playbook(HarnessConfig().playbook_path()).facts  # default playbook: no FACTS


def test_facts_line(make_obs, obs_start):
    fireball = {"id": 9, "name": "DoomImpBall", "distance": 120, "bearing": 0.0, "incoming": True}
    obs = make_obs(enemies=[enemy()], items=[item()], projectiles=[fireball],
                   player={**obs_start["player"], "health": 20})
    text = situation_report(TurnView(obs), Memory(), 3, facts=True)
    assert "FACTS: DANGER yes | LOW HEALTH yes | ENEMIES 1 | HINT yes | ITEMS 1 | EXIT no" in text
    calm = situation_report(TurnView(make_obs(enemies=[], items=[], known_items=[])), Memory(), 3, facts=True)
    assert "FACTS: DANGER no | LOW HEALTH no | ENEMIES 0 | HINT no | ITEMS 0 | EXIT no" in calm
    assert "FACTS:" not in situation_report(TurnView(obs), Memory(), 3)


def test_loop_breaker(make_obs):
    m = Memory()
    for t in range(1, 4):
        m.add(StepRecord(t, "turn", "around", "completed", "turned left 180 degrees"))
    assert any("3 times in a row" in h for h in m.hints(make_obs(enemies=[])))
    assert m.looping_actions() == set()  # hint first, one more chance
    m.add(StepRecord(4, "turn", "around", "completed", "turned left 180 degrees"))
    assert m.looping_actions() == {"turn"}
    view = TurnView(make_obs(enemies=[], items=[], known_items=[]), blocked=m.looping_actions())
    assert "turn" not in view.action_names and "explore" in view.action_names
    # an action that does something is never a loop
    m.add(StepRecord(5, "explore", "none", "completed", "explored", moved=400))
    m.add(StepRecord(6, "explore", "none", "completed", "explored", moved=380))
    m.add(StepRecord(7, "explore", "none", "completed", "explored", moved=390))
    assert m.repeating(3) is None and m.looping_actions() == set()
    # never block the only choice left
    only = TurnView(make_obs(enemies=[], items=[], known_items=[]), allowed=["turn"], blocked={"turn"})
    assert only.action_names == ["turn"]
