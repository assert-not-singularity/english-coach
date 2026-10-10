# english-coach

Quiet English feedback on every prompt you type, for non-native speakers who use
[Claude Code](https://claude.com/claude-code) daily. A small model (Haiku) reviews each prompt in a
separate background call, and the fixes appear directly under your message. Nothing is added to the
main conversation's context.

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

- **Fixes under your message.** A background review starts when you send a message. A second or two
  later the fixes show under it: the original in red, the fix in green, the category, and a short
  explanation.
- **Only real language problems.** Grammar, word choice, and transfer errors from your native
  language. Typos (including real-word typos such as "now" for "not"), letter case, punctuation,
  informal chat style, terse phrasing, jargon, and anything in code, quotes, or quote blocks are
  ignored. A phrase corrected in the last ten minutes is not corrected again, so pasting a tip back
  into a message does not produce a new one. Fixes that only add words around terse phrasing are
  dropped.
- **Careless mode.** Messages that were clearly dashed off (many typos, fragments, swearing, and
  starting lowercase or without closing punctuation) get no tip and are excluded from the
  statistics. Start a message with `~` to skip analysis entirely.
- **Dictated messages.** If your speech-to-text tool can add a marker to what it types (for example
  `[dictated]`), set it with `config set dictation_marker "[dictated]"` or in `/english-coach:setup`.
  A message containing the marker, anywhere and in any case, is reviewed in speech mode: likely
  mis-transcriptions (homophones such as their/there/they're, weather/whether, now/no), missing
  punctuation, filler words and run-ons are ignored, and only the grammar and word choice you
  produced is judged. The marker is removed before Claude reads your message
  (`config set strip_marker off` keeps it). Dictated messages are counted apart from typed ones:
  the main report covers written text only, followed by a short dictated section. The coach only
  sees the transcript, so pronunciation and fluency are out of its reach.
- **Errors and style.** Each finding has a severity. An error is something a native editor would
  mark wrong. A style item is correct English that a native would word differently, for example a
  valid but unnatural collocation or a redundancy; there is at most one per message. Style items are
  stored and listed in the report under "Native polish", never counted as errors, and not drawn under
  your message unless you turn on `show_style`. Register, formality, politeness and rare words are
  not flagged at all.
- **Garbled dictation gets a second opinion.** When the first review marks a dictated passage as unclear,
  meaning it makes little sense as a sentence and is probably a mis-transcription, the review is repeated
  once by a larger model (`escalate_model`, default `sonnet`; `none` turns it off) and only that answer
  is stored. Typed messages and clear dictated ones never trigger it. This is experimental: the small
  model flags only some garbled passages as unclear.
- **False friends.** With a native language set, the reviewer gets a short hint list of words that look
  like a word of that language but mean something else (`data/false_friends.json`, currently German).
  It is a hint, never a rule: the reviewer decides in context, so correct uses are left alone. To add a
  language or entries, follow the format of the file.
- **Forgetting a tip.** For an editing slip that was never a real mistake, `/english-coach:forget` deletes
  the latest tip without a trace. Unlike `dismiss`, the phrase can be flagged again later.
- **Watchlist.** A mistake that is flagged again and again (counted per message, by the changed words,
  for example `defer → derive`) gets an extra "keeps coming back" line from the second time on
  (`remind_after`). When you then use the right word correctly several times in a row
  (`resolve_after`, default 5), it leaves the watchlist; a relapse puts it back. Active entries are also
  shown to the reviewer. Add your own with `watch add <wrong> <right> [note]`.
- **Dismissing tips.** Some tips are not your mistake: a transcription error, a quote, a matter of taste.
  `dismiss` marks a tip with a reason; it stays in the database but leaves all statistics, and the
  same phrase is never flagged again. `undismiss` takes it back.
- **Weakness tracking.** Findings are stored under a fixed set of categories (article, preposition,
  word order, …). Recurring areas are shown first and given extra attention by the reviewer. A
  progress note ("Btw, your … is getting better") appears rarely, only for an improved category that
  your latest message handled well.

## Commands

```
/english-coach:setup           # native language, minimum words, tips per message
/english-coach:coach report    # weekly sparkline per category, share of clean messages, habit phrases,
                               # and (if a native language is set) slips that copy its patterns
/english-coach:coach recent    # the latest tips with ids
/english-coach:coach dismiss 12 asr   # tip 12 was not my mistake (asr, false, quote or taste)
/english-coach:coach dismiss last     # ... every tip of the last message
/english-coach:coach undismiss 12     # take it back
/english-coach:forget          # delete the latest tip without a trace (also: message, or an id)
/english-coach:coach watch     # mistakes that keep coming back; add <wrong> <right> [note] / remove <wrong>
/english-coach:coach off       # pause
/english-coach:coach on        # resume
```

Settings (`config set <key> <value>`): `native_language`, `dictation_marker` (none by default),
`strip_marker` (on), `min_words` (default 6), `max_tips` (default 2), `icon_written` (✎) and
`icon_dictated` (🎙︎): the icon in front of each fix, `remind_after` (2) and `resolve_after` (5) for the watchlist, `escalate_model` (sonnet) and `show_style` (off): draw style items under
your message, in a quieter style labelled "not an error". Both defaults are text-style symbols, so the pair
looks alike and follows the text color; set your own if you prefer, for example Nerd Font icons with
`config set icon_written $'\uf040'` and `config set icon_dictated $'\uf130'`.

## Privacy

Prompts are sent to Claude (Haiku) through your own Claude Code session, the same destination as the
prompt itself. Locally, only the corrected fragments, explanations, word counts, and whether a message was typed or dictated are stored,
never whole prompts, in `~/.local/share/english-coach/log.db` (override with `ENGLISH_COACH_DIR`).

## Requirements and limits

- A Claude Code version with mods (function hooks); developed on 2.1.294. The mods API is early
  access and can change between releases, in which case an update of this plugin may be needed.
- `python3` on the `PATH`.
- Each review is a small extra request that counts against your usage; a second review by the larger
  model happens only for unclear dictated passages.
- Review quality is that of a small model: it misses some errors, and now and then flags something a
  native would let pass.

## Development

```
claude plugin validate .                  # manifests and the mod
claude plugin test .                      # the mod's tests (mocked engine)
python3 -m unittest discover -s tests -p "test_*.py"   # the backend: guards, storage, dismiss (no model calls)
python3 tests/eval.py                     # prompt regression eval: the real review pipeline on fixture
                                          # messages, several runs each, pass rate per case
python3 tests/eval.py --model sonnet     # the same cases with another model (--no-escalation: no second review)
python3 tests/eval.py --script new=scripts/english-coach.py --script old=/path/to/old.py
                                          # compare a candidate against another version
```

The eval calls Haiku through the `claude` CLI against throwaway data, never your real database. Run
it before changing the prompt or the validation: the model's output is probabilistic, so it reports
rates, and cases marked informational (known weak spots) do not fail the run.

## Acknowledgments

This plugin adapts ideas from [claude-language-coach](https://github.com/sergiojrdotnet/claude-language-coach)
by Sergio Alves Junior (MIT): reviewing each prompt out of band with a small model, drawing the fixes
under the prompt through a `ui.render` hook on the message row, calibrating the reviewer with an
explicit "do not flag" list, treating errors that copy the user's native language as their own kind,
explanations that teach a reusable pattern, and discarding any fix whose original text does not occur
verbatim in the prompt. The code here is an independent implementation.
