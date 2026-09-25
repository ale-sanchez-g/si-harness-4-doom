---
actions: attack, pickup, explore, goto_exit, retreat, dodge, turn
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
4. HINT yes -> do what the HINT line says
5. ITEMS 1 or more -> pickup I1
6. EXIT yes -> goto_exit
7. nothing matched -> explore

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
