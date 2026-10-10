---
name: setup
description: Set up the English coach: ask for the user's native language and optional thresholds, then save them. Use when the user wants to configure the coach or after the coach's first-run hint.
argument-hint: "[native language]"
disable-model-invocation: true
---

Configure the English coach.

1. If `$ARGUMENTS` names a language, use it as the native language. Otherwise ask the user: "What is your native language? (This lets the coach spot transfer errors such as false friends and prepositions. You can also say 'skip'.)"
2. Optionally ask whether they dictate messages with a speech-to-text tool. If so, ask for the marker text the tool adds to dictated messages (for example `[dictated]`; it can be at the start or the end), so dictated and typed messages are reviewed and counted separately. Skip this if they do not dictate.
3. Optionally ask whether they want to change the minimum message length (default 6 words) or the maximum number of tips per message (default 2). Skip this if they seem satisfied with the defaults.
4. Save each value with the Bash tool, one command per setting:
   - `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" config set native_language "<language>"`
   - `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" config set dictation_marker "<marker>"`
   - `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" config set min_words <n>`
   - `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" config set max_tips <n>`
5. Run `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/english-coach.py" config` and show the result. Say that changes apply from the next message.

Do not assume or guess the user's native language from their name, writing, or anything else; only use what they tell you.
