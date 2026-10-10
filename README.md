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
- **Pasted chats.** A message that contains a pasted chat thread (two or more lines that are only a timestamp, such as
  `20m ago`, `3 hours ago`, `14:32` or `Yesterday at 3 PM`) is not reviewed at all, so other people's wording never reaches your
  tips or statistics. Your own text in the same message is skipped with it; start with `~` or leave out the paste if you want it reviewed.
- **Dictated messages.** If your speech-to-text tool can add a marker to what it types (for example
  `[dictated]`), set it with `config set dictation_marker "[dictated]"` or in `/english-coach:setup`.
  A message containing the marker, anywhere and in any case, is reviewed in speech mode: likely
  mis-transcriptions (homophones such as their/there/they're, weather/whether, now/no), missing
  punctuation, filler words, run-ons and spoken restarts or topic-first questions ("The retry logic, do we keep it?") are ignored, and only the grammar and word choice you
  produced is judged. The marker is removed before Claude reads your message
  (`config set strip_marker off` keeps it). Dictated messages are counted apart from typed ones:
  the main report covers written text only, followed by a short dictated section. The coach only
  sees the transcript, so pronunciation and fluency are out of its reach.
- **Slips.** A correction that only adds one small word that was missing is a slip, not a gap in your English: a
  forgotten "I" or "is" when typing, or any small word missing from dictated text, since recognizers drop weak words. Slips are
  stored and shown in `recent` and as one line in the report, but not drawn under your message and not counted as errors. If the
  same omission happens again, it becomes an error with the usual reminder, because a repeated omission points to a gap. A missing
  preposition or article in typed text always stays an error. In dictated text, a word you added or swapped for one that does not sound
  alike ("should have been looked", "wondering that") is your own wording, so it is a normal error.
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
- **Overview pane.** `/english-coach-overview` opens a pane beside the transcript with buttons to switch
  between written, spoken and both. Its headline is length-independent: **errors per 100 words** and the
  share of **sentences without an error** (the share of clean messages falls with length simply because
  long messages offer more places for a slip, so it is listed apart, by length bucket). Below: "What to work
  on", one bar per category (errors of the last 8 weeks, most frequent first) with a one-line pattern, and
  per category a `details` button (two more sentences) and an `examples` button (the changed words), the
  watchlist as cards, and collapsible sections for slips and "native polish". Weekly trend strips appear
  once there are two weeks of data. Press `r` to refresh. The numbers come from `english-coach.py overview <mode>` as JSON.
- **Pattern summaries.** The larger model (`summary_model`, default `sonnet`; `opus` takes about as long)
  receives your corrections grouped by category (your own short fragments, their fixes and explanations, the
  same ones the reviews already sent) and writes, per category, a headline with the general pattern, a short
  detail, and a "what to work on" list. The three views are written in parallel (about 15 to 30 seconds), the
  answer is stored locally, and the pane shows the last report while a new one is prepared. It is prepared
  automatically in the background, a minute after a session starts and then daily, and when you open the
  overview and none exists. It needs 10 or more errors in a view, and later `summary_every` (10) new ones since
  the last report; at most every six hours (ten minutes after opening the pane); `config set auto_summary off`
  stops that. The button "✦ Summarize patterns" (`p`) writes it on demand.
- **Sentence counts for older messages.** Messages stored before sentence counting existed can be filled in once with
  `english-coach.py backfill` (`--dry-run` first). It reads your own Claude Code session logs on your machine,
  matches each message by time and word count, and writes back numbers only; no text is stored or printed.
- **Phrase suggestions.** Now and then, when what you wrote has a plainer spot where a native colleague would
  naturally use a set expression ("cut to the chase", "a rule of thumb", "to be on the safe side"), a line in the
  accent color with a ✦ shows the phrase, its meaning, an example and the words it would replace. It is not a
  correction and never counts as an error. The review model decides what fits from a handful of phrases per message
  (`data/phrases.json`, about 50, the same for every native language). How often one appears depends on fitting
  occasions, never the calendar: a phrase is shown on its first fitting occasion, then after 1, 3, 7 and 7 further fitting
  occasions have passed, and it retires after five showings or once you used it twice yourself. At least four messages lie
  between two suggestions, and a message with a correction on the same words gets none. `phrases` lists them and
  `phrases ignore <id|last>` stops one for good.
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
/english-coach-overview       # an interactive pane: written, spoken or both, with trends and examples
/english-coach:coach recent    # the latest tips with ids
/english-coach:coach dismiss 12 asr   # tip 12 was not my mistake (asr, false, quote or taste)
/english-coach:coach dismiss last     # ... every tip of the last message
/english-coach:coach undismiss 12     # take it back
/english-coach:forget          # delete the latest tip without a trace (also: message, or an id)
/english-coach:coach phrases   # native phrases suggested so far; ignore <id|last> / unignore <id>
/english-coach:coach watch     # mistakes that keep coming back; add <wrong> <right> [note] / remove <wrong>
/english-coach:coach off       # pause
/english-coach:coach on        # resume
```

Settings (`config set <key> <value>`): `native_language`, `dictation_marker` (none by default),
`strip_marker` (on), `min_words` (default 6), `max_tips` (default 2), `icon_written` (✎) and
`icon_dictated` (🎙︎): the icon in front of each fix, `remind_after` (2) and `resolve_after` (5) for the watchlist, `escalate_model` (sonnet), `summary_model` (sonnet) and `show_style` (off): draw style items under
your message, in a quieter style labelled "not an error". Both defaults are text-style symbols, so the pair
looks alike and follows the text color; set your own if you prefer, for example Nerd Font icons with
`config set icon_written $'\uf040'` and `config set icon_dictated $'\uf130'`.

## Privacy

Prompts are sent to Claude (Haiku) through your own Claude Code session, the same destination as the
prompt itself. Locally, only the corrected fragments, explanations, word and sentence counts, and whether a message was typed or dictated are stored,
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
