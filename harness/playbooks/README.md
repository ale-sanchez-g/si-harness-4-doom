# Playbooks: the instructions given to the model

A playbook is a Markdown file that becomes the model's **system prompt**. It is
the main place to change *how* the AI plays. This folder is mounted into the
harness container, so edits apply on the next run without rebuilding.

```bash
make prompt                                         # show the filled-in prompt + a live situation report
make play ARGS="--playbook my_playbook --episodes 3"
make bench ARGS="--playbook my_playbook"            # compare against the scripted baseline
```

## Placeholders

| Placeholder         | Replaced with                                                           |
|---------------------|-------------------------------------------------------------------------|
| `{goal}`            | the scenario's goal and tips (from the server)                          |
| `{actions}`         | the actions allowed in this scenario, one per line                      |
| `{scenario_notes}`  | `scenarios/<scenario>.md` if it exists, otherwise `(none)`              |
| `{answer_format}`   | the JSON format the model must answer in (matches the enforced schema)  |

## `# TURN REMINDER`

Everything after a `# TURN REMINDER` heading is left out of the system prompt
and appended to **every** situation report instead. Small models weight the
end of the prompt most, so a one-line summary of the rules here helps a lot.
Keep it short: it costs tokens every turn.

## What the model sees each turn

```
TURN 9 | freedoom2 MAP01 | game time 9.9s
YOUR LAST TURNS (the past, may be out of date):
  T7 pickup I1 -> completed: picked up the bullet clip
  T8 pickup I1 -> interrupted: stopped on the way to the bullet clip: a Zombieman came into view
NOW:
YOU: health 100, armor 0, weapon pistol (64 ammo), kills 2
ENEMIES IN VIEW: 1 (attack them)
  E1 Zombieman - 800 away, straight ahead - weak zombie with a rifle
USEFUL ITEMS: 2 (pickup them when no enemy is in view)
  I1 bullet clip - 270 away, 13 deg to your right
  I2 shotgun - 400 away, 38 deg to your left
SPACE AROUND YOU (units until blocked): ahead 348, left 32, right 440, behind 252
CLOSED DOOR: 480 away, 144 deg to your right
EXIT: not found yet (explored 25% of the map)
Decide from the NOW part only. Rules in order: DANGER -> dodge | health 30 or less -> ... | otherwise -> explore.
Available actions now: attack, pickup, explore, retreat, dodge, turn, move, use, wait.
What do you do?
```

The answer is constrained to `{"Thought": "...", "action": "...", "arg": "..."}`,
where `action` and `arg` can only take values that are valid on this turn.

## Tips that measurably helped `granite4.2:3b`

Measure every change with `make eval` (fixed situations, known answers).

- Write the rules as an **ordered list keyed to the report's headings**
  ("ENEMIES IN VIEW is 1 or more -> attack E1").
- Give **one example per rule**, especially for rules the model tends to skip,
  and one for the "history trap" (a new enemy right after a kill).
- Tell it to decide from the `NOW:` part only; ask it to cite the rule in its
  `Thought` ("Rule 3: ..."), which keeps it on track.
- **Describe, don't prescribe**, outside the rules: "when you are ready" in a hint
  or "sidestep them" in a monster note overrides the rule order for a 3B model.
- Avoid vague priorities ("stay safe"): the model over-applies them, for
  example retreating from a harmless zombie at full health.
