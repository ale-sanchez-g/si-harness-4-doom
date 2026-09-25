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

## Front matter: `actions` and `facts`

A playbook may start with a small header:

```
---
actions: attack, pickup, explore, goto_exit, retreat, dodge, turn
facts: true
---
```

- `actions` limits the menu the model chooses from (it is still reduced to what
  makes sense each turn: no `attack` without an enemy, no `retreat` without a threat).
- `facts: true` makes the harness end every report with a pre-computed checklist,
  for example `FACTS: DANGER no | LOW HEALTH no | ENEMIES 1 | HINT no | ITEMS 2 | EXIT no`.
  Small models read literal values reliably but mis-derive them from prose.

Two playbooks ship with the harness:

| Playbook  | For                          | Style                                                      |
|-----------|------------------------------|------------------------------------------------------------|
| `default` | ~3B+ models (`granite4.2:3b`) | full rules, facts and examples, all 11 actions            |
| `small`   | ~0.3-1.5B models (`granite4:1b-h`, `granite4:350m-h`) | 7 actions, FACTS line, checklist-style `Thought` |

## `# TURN REMINDER`

Everything after a `# TURN REMINDER` heading is left out of the system prompt
and appended to **every** situation report instead. Small models weight the
end of the prompt most, so a one-line summary of the rules here helps
`granite4.2:3b` a lot. Keep it short: it costs tokens every turn.

`small.md` has no reminder on purpose. The 340M model copied it word for word
into its `Thought` ("ENEMIES 1+ -> attack E1") instead of reading the FACTS
line; without it, the FACTS line is the last thing it reads.

## What the model sees each turn

```
TURN 9 | freedoom2 MAP01 | game time 9.9s
YOUR LAST TURNS (the past, may be out of date):
  T7 pickup I1 -> completed: picked up the bullet clip
  T8 pickup I1 -> interrupted: stopped on the way to the bullet clip: a Zombieman came into view
NOW:
YOU: health 100 (good), armor 0, weapon pistol (64 ammo), kills 2
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

## Tips for ~1B and smaller models (`small.md`)

Measured with `granite4:1b-h` and `granite4:350m-h`:

- **Hand them the facts.** The 1B model argued "health 100 is low, retreat" and
  called any visible enemy DANGER. Pre-computed literal values (`facts: true`)
  that the `Thought` copies up to the first matching rule fixed both.
- **Offer fewer actions.** Seven instead of eleven, and none that make no sense
  this turn (the harness drops `retreat`/`dodge` when there is nothing to flee from).
- **Give nothing to parrot.** The 340M model copies whatever pattern it saw last:
  a turn reminder, the previous turn's summary. Keep the FACTS line last.
- **Let the harness break loops.** The 340M model can pick the same useless action
  over and over. The loop breaker hints after three repeats with no effect, then
  takes the action off the menu.
