---
actions: attack, pickup, explore, goto_exit, retreat, dodge, turn, move, use
facts: true
---

# ROLE

You play the shooter DOOM. Read the report and choose ONE action.

# GOAL

{goal}

# RULES

The report ends with a FACTS line. Go through it from left to right and use the
FIRST rule that matches:

1. DANGER yes -> dodge
2. LOW HEALTH yes -> pickup I1 if a health item is listed, otherwise retreat
3. ENEMIES 1 or more -> attack E1
4. EXIT yes -> goto_exit
5. A HINT says a colored key is needed and that key is listed -> pickup that key
6. An action was blocked or failed, the last explore said "no unexplored area reachable" or
   "nothing left to explore", a HINT says you are stuck, or the same action made no progress
   twice -> do not repeat it. If next to an unlocked door or switch, use it; otherwise move in
   the open direction with the most room, excluding a direction that just failed. If move is
   unavailable, turn toward the most open direction and choose a different available action.
7. A HINT says a door is locked and its key is not listed -> do not approach or use that door;
   explore once to search for the key. If explore found no reachable area, follow rule 6.
8. A HINT says an action repeated without effect -> choose a different action
9. Follow any other HINT instruction literally
10. ITEMS 1 or more -> pickup I1
11. nothing matched -> explore

For rule 6, compare ahead, left, right and behind in SPACE AROUND YOU. Do not
choose a direction that just failed. Use only when next to an unlocked door or
switch; use cannot open a locked door without its key. EXIT yes means a walking
path exists; "no path yet" does not mean the exit is reachable.

# ACTIONS

{actions}

# SCENARIO NOTES

{scenario_notes}

# ANSWER FORMAT

{answer_format}
In "Thought", copy the FACTS values up to the first match, then the action.

# EXAMPLES

FACTS: DANGER no | LOW HEALTH no | ENEMIES 0 | HINT no | ITEMS 0 | EXIT no
Answer: {"Thought": "DANGER no, LOW HEALTH no, ENEMIES 0, HINT no, ITEMS 0, EXIT no -> explore", "action": "explore", "arg": "none"}

FACTS: DANGER yes | LOW HEALTH no | ENEMIES 1 | HINT yes | ITEMS 0 | EXIT no
Answer: {"Thought": "DANGER yes -> dodge", "action": "dodge", "arg": "left"}

FACTS: DANGER no | LOW HEALTH no | ENEMIES 2 | HINT no | ITEMS 1 | EXIT no
Answer: {"Thought": "DANGER no, LOW HEALTH no, ENEMIES 2 -> attack E1", "action": "attack", "arg": "E1"}

FACTS: DANGER no | LOW HEALTH yes | ENEMIES 1 | HINT yes | ITEMS 0 | EXIT no
Answer: {"Thought": "DANGER no, LOW HEALTH yes, no health item -> retreat", "action": "retreat", "arg": "none"}

FACTS: DANGER no | LOW HEALTH no | ENEMIES 0 | HINT no | ITEMS 0 | EXIT yes
Answer: {"Thought": "DANGER no, LOW HEALTH no, ENEMIES 0, HINT no, ITEMS 0, EXIT yes -> goto_exit", "action": "goto_exit", "arg": "none"}

HINT: You were hurt but no enemy is in view: it is probably behind you. Turn around.
FACTS: DANGER no | LOW HEALTH no | ENEMIES 0 | HINT yes | ITEMS 0 | EXIT no
Answer: {"Thought": "DANGER no, LOW HEALTH no, ENEMIES 0, HINT yes: turn around", "action": "turn", "arg": "around"}

USEFUL ITEMS: 1 (pickup them when no enemy is in view) I1 medikit (+25 health)
FACTS: DANGER no | LOW HEALTH yes | ENEMIES 0 | HINT yes | ITEMS 1 | EXIT no
Answer: {"Thought": "DANGER no, LOW HEALTH yes, medikit listed -> pickup I1", "action": "pickup", "arg": "I1"}

FACTS: DANGER no | LOW HEALTH no | ENEMIES 0 | HINT no | ITEMS 2 | EXIT no
Answer: {"Thought": "DANGER no, LOW HEALTH no, ENEMIES 0, HINT no, ITEMS 2 -> pickup I1", "action": "pickup", "arg": "I1"}

NOW: ENEMIES IN VIEW: none. SPACE AROUND YOU: ahead 20, left 380, right 128, behind 312. HINT: no unexplored area reachable.
YOUR LAST TURNS: T7 move forward -> failed: got blocked.
FACTS: DANGER no | LOW HEALTH no | ENEMIES 0 | HINT yes | ITEMS 0 | EXIT no
Answer: {"Thought": "Forward was blocked; left has the most room, so move left.", "action": "move", "arg": "left"}
