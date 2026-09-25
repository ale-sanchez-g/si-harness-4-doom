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
    assert playbook.reminder.startswith("Rules in order")
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
    assert "E1 Zombieman" in text
    assert "I1 bullet clip" in text
    assert "T1 explore -> interrupted: enemy spotted: Zombieman" in text
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
