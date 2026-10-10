---
name: coach
description: Show English coach statistics, list recent tips, dismiss a tip that was not the user's mistake, or pause/resume the coach. Use only when the user asks for their English coach report or tips, wants to dismiss or restore a tip, or wants to turn the coach on or off.
argument-hint: "[report | recent [n] | dismiss <id|last> [reason] | undismiss <id|last> | phrases [ignore|unignore <id|last>] | audit [ack <id>] | on | off]"
disable-model-invocation: true
---

Run exactly one command with the Bash tool. Tool output is often collapsed in the terminal, so paste the command's output into your reply verbatim (in a code block), with no other commentary.

- no argument or `report`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" report`
- `recent` or `recent <n>`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" recent <n>` lists the latest tips with ids.
- `dismiss <id>` or `dismiss last`, with an optional reason (`asr` = transcription error, `false`, `quote`, `taste`): `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" dismiss <id|last> <reason>`. A dismissed tip is excluded from all statistics and the same phrase is never flagged again.
- `undismiss <id>` or `undismiss last`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" undismiss <id|last>` restores it.
- `phrases`, `phrases ignore <id|last>` or `phrases unignore <id>`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" phrases <args>` lists the native phrases suggested so far, or stops one from being suggested again (or takes that back).
- `audit` or `audit ack <id>`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" audit <args>` lists how a larger model rated the tips and which ones it doubts (needs `config set audit flagged`), or keeps a doubted tip.
- `off`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" off`, then say "Coach paused."
- `on`: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" on`, then say "Coach resumed."

Requested action: $ARGUMENTS
