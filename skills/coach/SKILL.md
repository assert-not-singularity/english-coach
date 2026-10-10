---
name: coach
description: Show English coach statistics (progress per category, habit phrases) or pause/resume the coach. Use only when the user asks for their English coach report, or to turn the coach on or off.
argument-hint: "[report|on|off]"
disable-model-invocation: true
---

Run exactly one command with the Bash tool and show its output verbatim, with no commentary:

- no argument or `report`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" report`
- `off`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" off`, then say "Coach paused."
- `on`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" on`, then say "Coach resumed."

Requested action: $ARGUMENTS
