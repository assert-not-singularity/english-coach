# english-coach

Quiet English feedback on every prompt you type, for non-native speakers who use
[Claude Code](https://claude.com/claude-code) daily. A small model (Haiku) reviews each prompt in a
separate background call. Nothing is added to the main conversation's context.

## Install

```
/plugin marketplace add assert-not-singularity/english-coach
/plugin install english-coach@english-coach
/english-coach:setup
```

`/english-coach:setup` asks for your native language so the coach can spot transfer errors (false
friends, calques, prepositions). Nothing is assumed: until you set it, the coach gives generic
feedback and shows a one-time reminder. Settings live in `~/.config/english-coach/config.json`; from
a terminal, `python3 <plugin-dir>/scripts/english-coach.py setup` does the same interactively.

## How it works

- **Tips after the reply.** The analysis runs while Claude answers. The `Stop` hook waits for it (up
  to 25 s) and shows at most two tips under the answer. A tip that is not ready in time appears with
  your next prompt.
- **Only real language problems.** Grammar, word choice, and transfer errors from your native
  language. Typos, letter case, punctuation, informal chat style, jargon, and anything in code or
  quotes are ignored.
- **Careless mode.** Messages that were clearly dashed off (many typos, fragments, swearing) get no
  tip and are excluded from the statistics. Start a message with `~` to skip analysis entirely.
- **Weakness tracking.** Findings are stored under a fixed set of categories (article, preposition,
  word order, …). Recurring areas are prioritised in the tips and in the coach's attention. A
  progress note ("Btw, your … is getting better") appears rarely, only for an improved category that
  your latest message handled well.

## Commands

```
/english-coach:setup           # native language, minimum words, tips per message
/english-coach:coach report    # weekly sparkline per category, share of clean messages, habit phrases
/english-coach:coach off       # pause
/english-coach:coach on        # resume
```

Settings (`config set <key> <value>`): `native_language`, `min_words` (default 6), `max_tips`
(default 2), `color` (default off; ANSI colours in tips, not every UI renders them).

## Privacy

Prompts are sent to Claude (Haiku) through your own Claude Code login, the same destination as the
prompt itself. Locally, only the corrected fragments, explanations, and word counts are stored,
never whole prompts, in `~/.local/share/english-coach/log.db` (override with `ENGLISH_COACH_DIR`).

## Requirements and limits

- `python3` and the `claude` CLI on the `PATH`.
- Each review is a small extra request that counts against your usage.
- Claude Code labels hook output with "Stop says:" or "UserPromptSubmit says:"; the label cannot be
  changed. Tips cannot be placed inline under your prompt.
