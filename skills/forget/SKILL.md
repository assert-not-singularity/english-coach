---
name: forget
description: Delete the latest English coach correction from the database without a trace, for example after an editing slip that is not a real mistake. Use only when the user asks to remove, forget or undo the latest correction.
argument-hint: "[last | message | <id>]"
disable-model-invocation: true
---

Run exactly one command with the Bash tool. Tool output is often collapsed in the terminal, so paste the command's output into your reply verbatim (in a code block), with no other commentary:

`python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" forget $ARGUMENTS`

With no argument or `last` it removes the single latest correction. `message` removes every correction of the latest message, and a number removes that correction (ids are shown by `/english-coach:coach recent`). Unlike `dismiss`, nothing is kept: the row is deleted and the phrase can be flagged again.
