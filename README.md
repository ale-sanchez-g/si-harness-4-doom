# si-harness-4-doom

**A harness that lets a small local LLM play DOOM.** A 3-billion-parameter model
(IBM Granite 4.2 3B, served by [Ollama](https://ollama.com)) cannot aim or steer
from pixels. It *can* read a short report, follow a written playbook, and pick
the right move. This project builds everything around the model so that is
enough to play:

- a **Doom game server in Docker** with an HTTP API: ViZDoom (the Doom engine
  used for AI research) plus Freedoom, both bundled;
- a **harness**: the instructions, the prompt, the action space, memory and
  guard rails that turn the model's answers into Doom commands;
- a **live viewer** to watch the model play and read its reasoning.

![The viewer: live game, the model's reasoning, the explored map and the decision log](docs/viewer.png)

---

## Quick start

Requirements: Docker with Compose v2. About 4 GB of RAM for the model; a GPU is
optional but makes turns much faster.

```bash
git clone https://github.com/ale-sanchez-g/si-harness-4-doom.git
cd si-harness-4-doom
cp .env.example .env          # optional: pick model, scenario, episodes...
docker compose up --build
```

Then open **http://localhost:8000** to watch.

On the first run the harness downloads `granite4.2:3b` (about 2.2 GB) into the
`ollama` volume. Then it plays Freedoom Phase 2 from MAP01, moving on to the next
map whenever it finds the exit (3 episodes by default), and prints every decision:

```
T002 | llm 12.8s | attack E1          | completed: killed Zombieman (3 shots) | hp 100 ar 0 kills 1
       thought: There is 1 enemy in view, so I must attack it according to rule 3.
T003 | llm 13.5s | pickup I1          | interrupted: stopped on the way to the bullet clip: a Zombieman came into view | hp 100 ar 0 kills 1
```

The first decision is slow (the model loads and reads the playbook once); after
that Ollama reuses the cached prompt and each turn only processes a short report.

| You have                   | Run                                                                              |
|----------------------------|----------------------------------------------------------------------------------|
| CPU only                   | `docker compose up --build` (or `make up`)                                       |
| NVIDIA GPU                 | `make gpu` (needs the NVIDIA Container Toolkit)                                  |
| Mac, or Ollama already installed | `ollama pull granite4.2:3b`, then `make host-ollama` (Docker on a Mac cannot use the GPU; native Ollama can) |
| No LLM, just the game      | `make scripted` runs the rule-based baseline                                     |

More `make` targets: `play`, `eval`, `bench`, `check`, `prompt`, `logs`, `down`.
Examples: `make play ARGS="--scenario defend_the_center --episodes 5"`, or
set `OLLAMA_MODEL=qwen3:4b` in `.env` to try another model.

---

## How it works

```
┌─────────────────────────────── harness (doom_harness) ───────────────────────────────┐
│  observation ──► TurnView ──► situation report ─┐                                     │
│  (JSON)         tags E1/I1,                      ├─► Ollama /api/chat ──► {"Thought",  │
│                 allowed actions                  │   format = JSON schema   "action",  │
│  playbook.md ──► system prompt (cached) ─────────┘   built for this turn     "arg"}    │
│                                                                    │                   │
│  memory + hints ◄── result ◄── POST /api/command ◄── validate/repair ◄─┘ (fallback:    │
│                                                                          scripted rule)│
└─────────────────────────────────────────┬─────────────────────────────────────────────┘
                                          │ HTTP / JSON
┌──────────────────────── doom server (doom_server) ───────────────────────────────────┐
│ ViZDoom (sync mode, frozen between calls) ─ perception ─ nav grid + A* ─ commands     │
│ /api/*  ·  /docs (OpenAPI)  ·  viewer at /  (MJPEG stream, top-down map, agent log)   │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### 1. Doom becomes turn-based
ViZDoom runs in synchronous mode: the world only moves while a command runs, so
the game waits however long the model thinks, whether 0.5 s on a GPU or 10 s on
a laptop CPU. One turn is one HTTP call.

### 2. The server describes the world in words and numbers
Instead of pixels, every response carries a structured observation:

- **enemies / items / barrels / projectiles** in view, from ViZDoom's object labels.
  Each has a name, distance, bearing (degrees, positive = left) and a short
  description ("imp, throws fireballs"); items also get a walking distance, and
  unreachable ones are marked;
- **walls**: how far you can walk in 8 directions, and what blocks you (wall, door, step);
- **map knowledge**: closed doors (and which key they need), unused switches, and the
  **exit** once seen. These come from the level's WAD line specials;
- **events**: pickups, kills, keys, game messages ("You need a blue key...").

### 3. Actions are intentions, not keypresses
The model picks from about 11 actions: `attack E1`, `pickup I2`, `explore`,
`goto_exit`, `retreat`, `dodge`, `turn`, `move`, `use`, `switch_weapon`, `wait`.
The server executes each one tic by tic:

| Command     | What the server does                                                                 |
|-------------|--------------------------------------------------------------------------------------|
| `attack`    | turns to the target every tic, fires when aimed, picks the best weapon, closes in on far targets |
| `goto`      | shortest path on a navigation grid (door, key and ledge aware), opens doors on the way, unsticks itself |
| `explore`   | walks to the nearest unexplored area (frontier exploration); presses unused switches when nothing is left |
| `goto_exit` | walks to the exit and activates it (switch, walk-over or shootable)                   |

Long commands stop early when something important happens: a **new enemy
appears**, or you take **damage from something unseen**. The model gets to
react, as a human would.

### 4. The playbook: the instructions around the model
`harness/playbooks/default.md` is the model's rulebook: role, goal, an
ordered list of decision rules, facts, the action list, answer format and
examples. The system prompt is built from it once per episode and never changes,
so Ollama keeps it in its KV cache. Each turn only adds a situation report of
about 200 tokens:

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

Everything after `# TURN REMINDER` in the playbook is appended to every report.
Small models pay most attention to the end of the prompt. Scenario-specific notes
live in `harness/playbooks/scenarios/<scenario>.md`.

### 5. Constrained decoding, per turn
The harness sends Ollama a **JSON schema built for this turn**. `action` is an
enum of only the actions possible right now (no `attack` without an enemy), and
`arg` is an enum of the current targets (`E1`, `I2`), directions and owned
weapons. Invalid answers cannot be generated. The reply is still validated
and repaired (a bad tag falls back to the nearest target), retried once, and
otherwise handled by the scripted baseline.

### 6. Guard rails
Short-term memory shows the model its last turns and adds **HINT** lines when it
is stuck, repeating a failing action, attacking something it cannot hit, or being
hurt by an unseen enemy. Unreachable items are hidden for a while after a failed
pickup.

### 7. Observability
- the viewer at `http://localhost:8000` shows the live game, the model's latest
  thought and action, the explored map and the decision log;
- `runs/<timestamp>_<scenario>_<model>/` holds `steps.jsonl` (prompt, raw model
  output, command, result and latency per turn), `episodes.json`, `summary.json`
  and the exact system prompt used.

---

## Tuning the harness

This is the fun part. The playbook is mounted into the container, so edit it and
re-run with no rebuild:

```bash
$EDITOR harness/playbooks/default.md   # or copy it to playbooks/aggressive.md
make prompt                            # print exactly what the model sees
make eval ARGS="--playbook aggressive" # score it on fixed situations, no game needed (~2 min on CPU)
make play ARGS="--playbook aggressive --episodes 5"
make bench                             # LLM vs scripted baseline over several scenarios
```

`eval` replays 11 hand-made situations with a known correct move ("fireball
incoming", "low health and a medikit", "a new zombie right after a kill", "the
exit is known"...) and reports accuracy and latency. It is the quickest way to
see whether a playbook edit or a different model (`--model qwen3:4b`) helps.
See [`harness/playbooks/README.md`](harness/playbooks/README.md) for the
playbook format.

Lessons learned while building this, tested against `granite4.2:3b` and all
encoded in the defaults:

1. **Ollama sorts structured-output keys alphabetically.** A schema asking for
   `{"thought", "action"}` comes back as `{"action", ..., "thought"}`, so the model
   decides *before* it reasons. The reasoning key is therefore `Thought`: a capital
   letter sorts before `action`.
2. **Turn off "thinking" for game loops.** `granite4.2` thinks by default and
   spent 300+ tokens (about 40 s on CPU) per move. The harness sends `think: false`
   to models that support thinking and asks for a one-sentence `Thought` instead.
   Set `HARNESS_THINK=true` on a fast GPU if you want to experiment.
3. **Key the rules to the report's headings**, and put counts next to them:
   "ENEMIES IN VIEW is 1 or more -> attack" plus `ENEMIES IN VIEW: 1 (attack them)`.
4. **Put history first and the present last.** With the last turns printed after
   the current state, the model reasoned "I already killed the zombie" while a new
   one stood in front of it. The report now ends with a `NOW:` block.
5. **Only show fresh news.** A leftover "Zombieman died" event caused the same mistake.
6. **One example per rule.** The model skipped the item rule until the playbook
   had an example of that exact situation.
7. **Describe, don't prescribe.** A monster note saying "sidestep them" made the
   model dodge when the rules said retreat; a hint saying "goto_exit when you are
   ready" made it decide it was not ready yet.

### Results so far

Measured in a 4-core CPU sandbox without a GPU.

**Decision quality** (`make eval`: 11 situations, each asked twice), `granite4.2:3b`:
**21/22 (96%)** rule-correct decisions, median **9 s per decision**. A GPU or
Apple Silicon brings this well under 2 s.

**Scripted baseline** (`make bench`, 2 episodes each; the same rules without an LLM),
to show what the server's commands make possible:

| Scenario            | Success | Notes                                              |
|---------------------|---------|----------------------------------------------------|
| `freedoom2` MAP01   | 2/2     | finds the exit in about 40 turns, 9 kills          |
| `basic`             | 2/2     | 2 turns                                            |
| `my_way_home`       | 2/2     | finds the armor in the maze in 9 turns             |
| `deadly_corridor`   | 1/2     | skill 5, as designed                               |
| `defend_the_center` | n/a     | survival scenario: 26 kills before running dry     |
| `defend_the_line`   | n/a     | 32 kills before being overrun                      |
| `health_gathering`  | n/a     | still alive after 150 turns                        |
| `take_cover`        | 0/2     | dodging needs quicker reactions than turn-based play |

**Granite 4.2 3B playing Freedoom MAP01** (one full run, default playbook):
it **reached the exit in 38 turns** with 9 kills and 100 health, taking only 5
damage in the whole level. All 38 decisions came from the model, with no
fallbacks and no repaired answers: 14 attacks, 13 pickups, 5 explores, 4 dodges,
1 turn-around, then `goto_exit`. On the 4-core CPU this took 14 minutes of
wall-clock time, almost all of it spent on the model (median 16.6 s per
decision, while other jobs shared the CPU). A representative moment:

```
T035 | llm 16.6s | dodge right        | ... | hp 95 ar 1 kills 9
       thought: DANGER line indicates a DoomImpBall is flying; I must dodge. HINT says 'dodge left'
                failed twice, so try a different dodge direction.
T036 | llm 16.5s | turn around        | completed: turned left 180 degrees | hp 95 ar 1 kills 9
T037 | llm 15.6s | pickup I1          | completed: picked up the medikit (+25 health) | hp 100 ar 2 kills 9
T038 | llm 14.6s | goto_exit          | episode_over: exit | hp 100 ar 3 kills 9
```

---

## Scenarios

| Scenario                   | What it is                                                     | Notes                              |
|----------------------------|----------------------------------------------------------------|------------------------------------|
| `freedoom2` (default)      | Freedoom Phase 2 campaign, MAP01-MAP32                         | doors, keys, switches, exit        |
| `freedoom1`                | Freedoom Phase 1 campaign, E1M1-E4M9                           |                                    |
| `doom2` / `doom`           | the original games                                             | put your own `doom2.wad` / `doom.wad` in `./wads` |
| `basic`                    | one monster in front of you                                    | sanity check                       |
| `defend_the_center`        | monsters come from all sides; you cannot move                  | limited ammo                       |
| `defend_the_line`          | hold the line against approaching monsters                     |                                    |
| `deadly_corridor`          | reach the armor at the end of a guarded corridor               | hard                               |
| `health_gathering(_supreme)` | acid floor, survive by collecting medikits                   | no enemies                         |
| `my_way_home`              | find the armor in a maze                                       | navigation                         |
| `take_cover`               | dodge fireballs, no weapon                                     |                                    |

Pick with `HARNESS_SCENARIO` / `HARNESS_MAP` in `.env`, or `--scenario/--map`.
With `HARNESS_CAMPAIGN=true` (the Docker default) an episode that reaches the exit
is followed by the next map; a death restarts `HARNESS_MAP`.

---

## The Doom HTTP API

Anything can play through the API, not just this harness. Interactive docs are
at **http://localhost:8000/docs**.

| Method & path             | Purpose                                                               |
|---------------------------|-----------------------------------------------------------------------|
| `POST /api/episode`       | start an episode `{scenario, map, skill, seed, timeout}` → observation |
| `GET  /api/observation`   | the current observation                                               |
| `POST /api/command`       | run a high-level command → `{status, reason, events, changes, observation}` |
| `GET  /api/commands`      | command catalogue with parameters                                     |
| `POST /api/step`          | low-level: hold raw buttons for N tics (RL-style clients)             |
| `GET  /api/scenarios`     | available scenarios                                                   |
| `GET  /api/frame.jpg`, `/api/map.png`, `/api/stream.mjpg` | current frame, top-down map, live video |
| `GET  /api/status`        | cheap status for dashboards                                           |
| `POST/GET /api/agent/log` | post or read the agent's decisions (shown in the viewer)             |
| `PUT  /api/settings`      | change playback speed at runtime (`playback_fps`, 0 = max)            |

```bash
curl -s -X POST localhost:8000/api/episode -H 'content-type: application/json' \
     -d '{"scenario": "freedoom2", "map": "MAP01"}' | jq .enemies
curl -s -X POST localhost:8000/api/command -H 'content-type: application/json' \
     -d '{"command": "explore", "duration": 5}' | jq '{status, reason, changes}'
curl -s -X POST localhost:8000/api/command -H 'content-type: application/json' \
     -d '{"command": "attack", "target_id": 121}' | jq .reason
```

`status` is one of `completed`, `interrupted` (with the reason, e.g. `a DoomImp came into view`),
`failed` (e.g. `no known path (blocked by a locked door...)`) or `episode_over`.

---

## Configuration

Everything is in `.env` (see `.env.example`). The most useful settings:

| Variable               | Default          | Meaning                                                           |
|------------------------|------------------|-------------------------------------------------------------------|
| `OLLAMA_MODEL`         | `granite4.2:3b`  | any Ollama chat model                                             |
| `HARNESS_POLICY`       | `llm`            | `llm` or `scripted` (baseline, no model)                          |
| `HARNESS_PLAYBOOK`     | `default`        | file in `harness/playbooks/`                                      |
| `HARNESS_SCENARIO` / `HARNESS_MAP` | `freedoom2` / `MAP01` | what to play                                       |
| `HARNESS_EPISODES`, `HARNESS_MAX_STEPS` | `3`, `400` | how long to play                                       |
| `HARNESS_CAMPAIGN`     | `true`           | after an exit, continue with the next map                         |
| `HARNESS_REASONING`    | `true`           | ask for a one-sentence `Thought` before each action               |
| `HARNESS_THINK`        | auto (off)       | Ollama's `think` flag for reasoning models                        |
| `HARNESS_TEMPERATURE`  | `0.2`            | sampling temperature                                              |
| `DOOM_PLAYBACK_FPS`    | `35`             | 35 = watchable real time, 0 = as fast as possible                 |
| `DOOM_SKILL`           | `3`              | 1 (easy) to 5 (nightmare) for campaigns; ViZDoom scenarios keep their own |
| `DOOM_KNOWLEDGE`       | `fair`           | `fair`: the exit is known once seen; `full`: known from the start |
| `DOOM_HEARING_RANGE`   | `0`              | report out-of-sight monsters within this range (0 = off)          |

---

## Running without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "./server[dev]" -e "./harness[dev]"
python -m doom_server &                                   # http://localhost:8000
ollama pull granite4.2:3b                                 # native Ollama
python -m doom_harness play --scenario freedoom2 --episodes 1
```

Tests (the server tests drive the real engine headlessly, including a full
MAP01 run to the exit through the API):

```bash
cd server && pytest -q
cd harness && pytest -q
```

## Project layout

```
server/doom_server/   session.py (ViZDoom lifecycle, per-tic tracking), perception.py,
                      commands.py (macro actions), navigation.py (grid, paths,
                      exploration), wad.py (line specials), api.py, static/index.html
harness/doom_harness/ agent.py (turn loop), actions.py (action space + schema),
                      prompts.py, policies.py (LLM + scripted), memory.py, llm.py,
                      evals.py (decision-quality checks), cli.py
harness/playbooks/    the instructions: default.md + scenarios/*.md
docker-compose*.yml   doom + ollama + harness (GPU / host-Ollama overrides)
```

## Credits and licenses

- [ViZDoom](https://github.com/Farama-Foundation/ViZDoom) (MIT) provides the Doom
  engine and the classic AI scenarios.
- [Freedoom](https://freedoom.github.io) (BSD-3-Clause) provides the free game data
  bundled with ViZDoom. No commercial WADs are included; bring your own for `doom`/`doom2`.
- [IBM Granite 4.2](https://ollama.com/library/granite4.2) (Apache 2.0) is the default model.
- This project: MIT, see `LICENSE`.
