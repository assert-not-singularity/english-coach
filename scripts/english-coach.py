#!/usr/bin/env python3
"""English coach backend. The inline mod (hooks/register.tsx) calls it; it holds the prompt, validation,
storage, statistics and settings so that logic lives in one place.

Usage:
  english-coach.py prepare            stdin {"prompt"} -> {"coach", "system", "user"}: coach this message?
  english-coach.py check              stdin like ingest: {"escalate", "model"}: does a dictated review need a second opinion?
  english-coach.py ingest             stdin {"prompt", "reply"}: validate and store a review, print the fixes
  english-coach.py report             per-category weekly trend, clean-message share, habit phrases
  english-coach.py watch [add W R [note] | remove W]   recurring mistakes: list, add, remove
  english-coach.py forget [last|message|<id>]  delete the latest correction without a trace
  english-coach.py overview [written|dictated|both]   the data of the overview pane as JSON
  english-coach.py summary-input|summary-store [mode]   the written pattern summaries of the overview pane (the mod calls the model)
  english-coach.py backfill [--dry-run]  one-time: sentence counts for older messages, from your own Claude Code session logs
  english-coach.py audit [ack <id>]   second opinions of the larger model on the tips (needs `config set audit flagged|all`)
  english-coach.py phrases [ignore|unignore <id|last>]   native phrases suggested so far; ignore one for good
  english-coach.py recent [n]         latest findings with ids
  english-coach.py dismiss <id|last> [reason]   not my mistake: excluded from statistics, never flagged again
  english-coach.py undismiss <id|last>          restore a dismissed finding
  english-coach.py off | on           pause / resume
  english-coach.py setup              interactive setup (native language, thresholds)
  english-coach.py config [set KEY VALUE]   show or change settings
"""
import difflib, json, os, random, re, sqlite3, sys, time
from collections import Counter
from pathlib import Path

DATA = Path(os.environ.get("ENGLISH_COACH_DIR", Path.home() / ".local/share/english-coach"))
DB = DATA / "log.db"
PAUSED = DATA / "paused"


CONFIG_FILE = Path(os.environ.get("ENGLISH_COACH_CONFIG_DIR",
                                   Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "english-coach")) / "config.json"
DEFAULTS = {"native_language": None, "dictation_marker": None, "strip_marker": True, "min_words": 6, "max_tips": 2,
            "icon_written": "\u270e", "icon_dictated": "\U0001F399\uFE0E", "show_style": False, "escalate_model": "sonnet", "summary_model": "sonnet", "auto_summary": True, "summary_every": 10,
            "remind_after": 2, "resolve_after": 5, "audit": "off", "audit_model": "sonnet"}   # pen; studio microphone, text style
STRING_KEYS = ("native_language", "dictation_marker", "icon_written", "icon_dictated", "escalate_model", "summary_model", "audit", "audit_model")   # text settings; None = not set


def load_config():
    """Settings from CONFIG_FILE merged over DEFAULTS; a broken file falls back to defaults."""
    cfg = dict(DEFAULTS)
    try:
        for k, v in json.loads(CONFIG_FILE.read_text()).items():
            if k in DEFAULTS and (type(v) in (type(DEFAULTS[k]), type(None)) or (k in STRING_KEYS and isinstance(v, str))):
                cfg[k] = v
    except Exception:
        pass
    for k in STRING_KEYS:
        if isinstance(cfg[k], str) and not cfg[k].strip():
            cfg[k] = None
    return cfg


CFG = load_config()
NATIVE = CFG["native_language"]          # None = unknown: no assumption is made about the user's language
MIN_WORDS, MAX_CHARS = max(1, int(CFG["min_words"])), 2000
MAX_TIPS = max(1, int(CFG["max_tips"]))
AUDIT = (CFG["audit"] or "off").lower() if (CFG["audit"] or "off").lower() in ("off", "flagged", "all") else "off"
REMIND_AFTER = max(1, int(CFG["remind_after"]))     # flags of the same mistake before the reminder line appears
RESOLVE_AFTER = max(1, int(CFG["resolve_after"]))   # correct uses in a row before a mistake leaves the watchlist
BTW_COOLDOWN = 3 * 86400
WINDOW = 30 * 86400
CATEGORIES = ["article", "preposition", "word-order", "verb-tense", "verb-form", "agreement",
              "plural-countability", "word-choice", "collocation", "pronoun", "conjunction", "phrasing", "other"]
CATEGORY_GUIDE = """Category definitions (pick exactly one; apply the FIRST rule that fits):
- article: missing, extra or wrong a/an/the/zero article
- plural-countability: plural or uncountable noun problems (informations, a advice, much/many)
- agreement: subject-verb or determiner-noun agreement
- verb-tense: wrong tense or aspect (I am since two years -> I have been ... for)
- verb-form: wrong form of the verb itself (infinitive vs -ing, irregular forms, missing auxiliary)
- preposition: wrong or missing preposition after a verb, noun or adjective (explain me -> explain to me)
- pronoun: wrong pronoun, reflexive or relative pronoun
- conjunction: wrong connector between clauses (because/since/while/although)
- word-order: right words in an unnatural order (adverb or object placement, question order)
- word-choice: a single wrong word, including false friends (a word that looks like one in the user's language)
- collocation: right words that do not go together as a fixed pair (make vs do, strong vs heavy)
- phrasing: the grammar is fine but a native would word the whole clause differently
- other: only if nothing above fits
The transfer flag is separate: it never changes the category."""
ENGINE_FRAMED = re.compile(r"^<(bash-|command-|local-command|task-notification|system-reminder)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts(id INTEGER PRIMARY KEY, ts REAL, words INTEGER, did_well TEXT, mode TEXT DEFAULT 'written', sentences INTEGER);
CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY, prompt_id INTEGER, ts REAL, category TEXT,
  transfer INTEGER, original TEXT, fix TEXT, explanation TEXT, shown INTEGER DEFAULT 0, mode TEXT DEFAULT 'written',
  severity TEXT DEFAULT 'error', dismissed INTEGER DEFAULT 0, dismiss_reason TEXT, sent_idx INTEGER);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS summaries(mode TEXT, category TEXT, n INTEGER, text TEXT, ts REAL, PRIMARY KEY(mode, category));
CREATE TABLE IF NOT EXISTS habits(id INTEGER PRIMARY KEY, wrong TEXT, right TEXT, hits INTEGER DEFAULT 0, last_hit REAL,
  streak INTEGER DEFAULT 0, correct_uses INTEGER DEFAULT 0, status TEXT DEFAULT 'active', source TEXT DEFAULT 'auto', note TEXT,
  UNIQUE(wrong, right));
CREATE TABLE IF NOT EXISTS audits(finding_id INTEGER PRIMARY KEY, verdict TEXT, category TEXT, reason TEXT, ts REAL, ack INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit_misses(id INTEGER PRIMARY KEY, prompt_id INTEGER, ts REAL, category TEXT, original TEXT, fix TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS phrases(id TEXT PRIMARY KEY, shown INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, uses INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active', last_shown REAL);
"""

def split_marker(text):
    """(text without the dictation marker, whether it was there). Dictation tools add the marker after every chunk,
    so it can sit anywhere: all occurrences go, and one occurrence makes the whole message dictated. Text without the
    marker is returned untouched; line breaks are never collapsed (quote blocks and code fences depend on them)."""
    t, marker = text.strip(), CFG["dictation_marker"]
    pattern = re.compile(re.escape(marker), re.I) if marker else None
    if pattern is None or not pattern.search(t):
        return t, False
    return re.sub(r"[ \t]{2,}", " ", pattern.sub(" ", t)).strip(), True


def _contains_phrase(words, phrase):
    return bool(phrase) and any(words[i:i + len(phrase)] == phrase for i in range(len(words) - len(phrase) + 1))


def false_friend_hint(native, text=""):
    """A hint, never a rule: words that look like a word of the user's language but mean something else. Only the entries whose
    word occurs in the message are passed on, so most reviews get no hint and a long list never dilutes the prompt. The reviewer
    still decides in context. Data lives in data/false_friends.json."""
    try:
        entries = json.loads((Path(__file__).resolve().parent.parent / "data" / "false_friends.json").read_text())
        items = entries.get((native or "").strip().lower(), [])
    except Exception:
        return ""
    words = _words(text)
    items = [e for e in items if _contains_phrase(words, _words(e["word"]))]
    if not items:
        return ""
    listed = "; ".join(f'"{e["word"]}" ({native} "{e["looks_like"]}" = {e["means"]})' for e in items)
    return (f"\nKnown false friends for {native} speakers; flag one only when it is used with the {native} meaning, never when it is "
            f"used correctly in English: {listed}.")


def phrase_block(phrases):
    """The optional phrase-suggestion section of the review prompt; empty when no phrase is up for suggestion."""
    if not phrases:
        return ""
    listed = "\n".join(f'- {e["id"]}: "{e["phrase"]}" ({e["meaning"]})' for e in phrases)
    return ("\n\nPHRASE SUGGESTION (optional and rare): the expressions below are natural in English chat between colleagues. Report one in "
            '"phrases" only if the message contains plain wording that a native colleague would naturally say with that expression, as '
            '{"id":"<id>","original":"<the exact words of the message it would replace, copied verbatim>"}. At most one. Most messages get none: '
            "never force a fit, never pick one the message already uses, and never for text in code or quotes. A phrase is no correction: "
            "do not mention it in \"fixes\".\n" + listed)


def system_prompt(speech=False, text="", phrases=()):
    n = NATIVE
    who = f"a non-native English speaker (native language: {n})" if n else "a non-native English speaker"
    transfer = (f"{n} transfer errors (false friends, literal calques, prepositions or verb patterns copied from {n}); " if n else "")
    transfer_field = f"true if the error copies a {n} pattern" if n else "always false"
    unclear_field = ',"unclear":false' if speech else ""
    unclear_spec = ('- unclear: true only if the passage in "original" makes little sense as a sentence (garbled, for example a speech-recognition '
                    'mishearing), whatever its grammar; otherwise false\n') if speech else ""
    speech_rules = ("SPEECH MODE: this text was produced by speech recognition, the user dictated it. Ignore likely mis-transcriptions "
                    "(homophones and sound-alikes such as their/there/they're or weather/whether, misheard or split words), missing or odd "
                    "punctuation and capitalization, filler words, false starts and run-on sentences. The tool also removes spoken pauses, so a topic named first and then a question or comment about it (\"What you are doing right now, is this in line with the plan?\", \"The retry logic, do we keep it?\"), a restart or an afterthought is ordinary spoken syntax: never flag its word order or a pronoun that picks the topic up again. Punctuation that is spoken aloud (\"in quotation marks\", \"quote unquote\", \"in brackets\", \"air quotes\") announces that the next word is quoted or set apart, and an aside dropped into the middle of a clause (\"have not been, in quotation marks, realized\") is ordinary speech: never move it, rewrite it, or flag the word order around it. Judge only the grammar and word choice "
                    "the speaker produced, never register or formality. If a phrase makes little sense, first check whether a similar-sounding phrase would make it a sensible sentence (\"to define\" for \"still fine\"): that is a transcription error, never flag it. Never set \"careless\".\n\n" if speech else "")
    hint = false_friend_hint(n, text)
    return f"""You are an English writing coach inside a coding tool. The user is {who} who talks to a coding assistant in English, in informal, task-focused messages, and wants to improve the language. Feedback is shown as a small dim note, so a pedantic or wrong correction costs more than a missed one.
Calibrate like a careful native-speaking senior engineer reading a colleague's informal message: flag what they would actually correct, nothing else.

FLAG: wrong word or word form; grammar (agreement, verb forms and tenses, missing or wrong articles/determiners, plurals and countability, word order, wrong prepositions, an adverb where an adjective belongs such as "totally nonsense"); {transfer}phrasing a native would clearly rewrite because it sounds foreign.
DO NOT FLAG: typos and misspellings (including a slip that happens to form another real word, such as "now" for "not" or "form" for "from"), letter case, punctuation, informal tone, questions written in statement word order or with trailing ellipses (normal in chat), elliptical suggestions or questions built on a gerund or noun phrase ("Maybe running it nightly?"), slang, fragments, dropped subjects, terse imperatives, technical jargon, product/tool names, code, paths, URLs, anything in backticks or quotation marks, words from the user's own language used on purpose, valid stylistic choices that are just as natural, or the content/approach itself.
Register, formality and word rarity are NEVER errors: an unusual, archaic, formal or playful word that is used correctly (for example \"thrice\" or \"whilst\") is not a mistake, and neither is a wording that is merely less common than another. Informal conventions are NOT errors either: a statement with a question mark ("The service also had retries...?"), trailing ellipses, and sentence fragments are normal informal English; never "fix" them into formal questions.
When in doubt, do not flag. A missed tip is better than a pedantic one.
Before emitting a fix, drop it unless: a native editor would mark the original as an error (not taste), the fix keeps meaning and tone, and the original is ordinary prose.

CARELESS FLAG: set "careless" true only if the message was clearly dashed off without any care for language (many typos, missing capitalization and punctuation, broken fragments, swearing, frustration). A message that is capitalized and punctuated, or that contains deliberate or playful mistakes, is NOT careless. Always list the fixes you find regardless of this flag: the caller decides whether to show them.

{speech_rules}Output ONLY one raw JSON object, no fences:
{{"careless":false,"fixes":[{{"original":"","fix":"","category":"","transfer":false,"severity":"error"{unclear_field},"explanation":""}}],"did_well":[],"phrases":[]}}
- severity: "error" when a native editor would mark the original as wrong; "style" when it is correct but a native would clearly say it another way (a collocation or construction that is valid yet unnatural, or a redundancy). At most ONE style item per message, and only when the better wording is clearly more natural. Register, formality, politeness and word rarity are never style items.
{unclear_spec}- original: exact substring copied verbatim from the message, only the few words with the error
- fix: the single corrected replacement: the smallest change that reads naturally. Keep the user's concise chat style: never add back dropped subjects or other elements, and prefer the short wording a native would use in chat over a formal, wordy construction
- category: exactly one of {', '.join(CATEGORIES)} (see definitions below)
- transfer: {transfer_field}
- explanation: max 25 words, neutral and encouraging; the reusable pattern with a tiny correct example; never restate the error; plain words, no grammar jargon
- at most 3 fixes, most valuable first; clean message: {{"fixes":[],"did_well":[...]}}
- did_well: categories (same list) in which the message used a non-trivial construction correctly; empty if unsure

{CATEGORY_GUIDE}{hint}{phrase_block(phrases)}"""


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=5)
    c.executescript(SCHEMA)
    for table, column, declaration in (("prompts", "mode", "TEXT DEFAULT 'written'"), ("findings", "mode", "TEXT DEFAULT 'written'"),
                                       ("findings", "severity", "TEXT DEFAULT 'error'"), ("findings", "dismissed", "INTEGER DEFAULT 0"),
                                       ("findings", "dismiss_reason", "TEXT"), ("prompts", "sentences", "INTEGER"),
                                       ("findings", "sent_idx", "INTEGER")):
        if column not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:   # databases from before this column
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return c


def get_state(c, key, default=0.0):
    r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return float(r[0]) if r else default


def set_state(c, key, value):
    c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))


def recurrence(c):
    return dict(c.execute("SELECT category, COUNT(*) FROM findings WHERE ts>? AND mode='written' AND severity='error' AND dismissed=0 GROUP BY category",
                          (time.time() - WINDOW,)).fetchall())


def rate(c, since, until, cat):
    """(findings per 100 words, words) in [since, until)."""
    words = c.execute("SELECT COALESCE(SUM(words),0) FROM prompts WHERE ts>=? AND ts<? AND mode='written'", (since, until)).fetchone()[0]
    n = c.execute("SELECT COUNT(*) FROM findings WHERE category=? AND ts>=? AND ts<? AND mode='written' AND severity='error' AND dismissed=0", (cat, since, until)).fetchone()[0]
    return (100 * n / words if words else None), words


def transfer_rate(c, since, until):
    """(transfer errors per 100 words, words) in [since, until): slips that copy the user's native language."""
    words = c.execute("SELECT COALESCE(SUM(words),0) FROM prompts WHERE ts>=? AND ts<? AND mode='written'", (since, until)).fetchone()[0]
    n = c.execute("SELECT COUNT(*) FROM findings WHERE transfer=1 AND ts>=? AND ts<? AND mode='written' AND severity='error' AND dismissed=0", (since, until)).fetchone()[0]
    return (100 * n / words if words else None), words


def improved(c):
    """Categories with a rate in the last 14 days below half of the 14 days before (enough data only)."""
    now, d = time.time(), 14 * 86400
    out = {}
    for cat in CATEGORIES:
        old, w_old = rate(c, now - 2 * d, now - d, cat)
        new, w_new = rate(c, now - d, now + 1, cat)
        if old and new is not None and w_old >= 300 and w_new >= 300 and new < 0.5 * old:
            out[cat] = (old, new)
    return out


# A line that is only a timestamp of a chat export: "20m ago", "3 hours ago", "14:32", "Yesterday at 3 PM", "Today at 14:32".
CHAT_STAMP = re.compile(r"^\s*(?:\d+\s*(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?|w|wks?|weeks?)\s+ago"
                        r"|(?:(?:today|yesterday)\s+(?:at\s+)?)?\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?"
                        r"|(?:today|yesterday)(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?)\s*$", re.I)


def looks_pasted(prompt):
    """A message that contains a pasted chat: two or more lines that are only a timestamp, as chat exports and copied threads
    have. Those words are other people's (or older), so the message is not reviewed at all."""
    return sum(1 for line in prompt.splitlines() if CHAT_STAMP.match(line)) >= 2


def coachable(prompt):
    words = len(re.findall(r"\w+", prompt))
    return (words >= MIN_WORDS and len(prompt) <= MAX_CHARS and not prompt.startswith(("/", "!", "~"))
            and not ENGINE_FRAMED.match(prompt) and not looks_pasted(prompt))


def setup_hint(c):
    """One-time reminder to set the native language (None once shown or when a language is set)."""
    if NATIVE is None and not get_state(c, "hint_shown"):
        set_state(c, "hint_shown", 1)
        return "Tip: run /english-coach:setup to tell the coach your native language, so it can spot transfer errors (shown once)."
    return None


def maybe_btw(c):
    """Sparse progress note: an improved category AND the latest analyzed message handled it well."""
    if time.time() - get_state(c, "last_btw") <= BTW_COOLDOWN:
        return None
    r = c.execute("SELECT did_well, ts FROM prompts WHERE mode='written' ORDER BY id DESC LIMIT 1").fetchone()
    well = set(json.loads(r[0])) if r and r[0] and r[1] > get_state(c, "last_careless") else set()
    for cat, (old, new) in improved(c).items():
        if cat in well:
            set_state(c, "last_btw", time.time())
            return f"Btw: your {cat} is getting better ({old:.1f} → {new:.1f} slips per 100 words)."
    return None














def json_objects(text):
    """All top-level JSON objects in text (model may add stray prose)."""
    out, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            esc = (not esc and ch == "\\")
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"' and depth:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    out.append(json.loads(text[start:i + 1]))
                except Exception:
                    pass
    return out


def skeleton(s):
    return re.sub(r"[\W_]+", "", s.lower())


def clean(v, limit):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", v)).strip()[:limit] if isinstance(v, str) else ""


QUOTED = re.compile(r'```.*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|^[ \t]*>[^\n]*$', re.S | re.M)
RECENT = 600   # seconds: a phrase corrected this recently and seen again is most likely a quote of the tip


def recent_phrases(c):
    """(originals, fixes) of corrections made in the last RECENT seconds, lower-cased."""
    originals, fixes = set(), set()
    for orig, fix in c.execute("SELECT original, fix FROM findings WHERE ts > ?", (time.time() - RECENT,)):
        originals.add(orig.lower())
        fixes.add(fix.lower())
    return originals, fixes


def _contains(hay, needle):
    return len(needle) >= 2 and any(hay[i:i + len(needle)] == needle for i in range(len(hay) - len(needle) + 1))


def repeats_recent(original, fix, recent):
    """True when this finding repeats a correction made a moment ago, also when the span is shorter or longer than before
    (the user pasted or paraphrased it): same words, either direction, against the earlier originals; a piece of an earlier
    fix counts too (the corrected text is being quoted)."""
    originals, fixes = recent
    if original.lower() in originals or original.lower() in fixes or fix.lower() in originals:
        return True
    o = _words(original)
    return any(_contains(_words(r), o) or _contains(o, _words(r)) for r in originals) or any(_contains(_words(f), o) for f in fixes)


def is_slip(a, b):
    """True when two texts differ only by one word that is a one-letter slip of the other (one substituted letter or
    one swapped neighbouring pair): a typo, not grammar."""
    wa, wb = a.lower().split(), b.lower().split()
    if len(wa) != len(wb) or not wa:
        return False
    differing = [(x, y) for x, y in zip(wa, wb) if x != y]
    return len(differing) == 1 and _word_slip(*differing[0])


def _word_slip(a, b):
    a, b = a.strip(".,;:!?"), b.strip(".,;:!?")
    if len(a) != len(b) or len(a) < 2 or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])


def _soundex(word):
    """Soundex key of one word, after dropping apostrophes and a few silent leading letters (wr-, kn-, gn-, ps-)."""
    w = re.sub(r"(?<=.)gh", "", re.sub(r"[^a-z]", "", word.lower()))   # right -> rit
    for silent in ("wr", "kn", "gn", "ps"):
        if w.startswith(silent):
            w = w[1:]
            break
    if not w:
        return ""
    codes = {c: d for d, letters in {"1": "bfpv", "2": "cgjkqsxz", "3": "dt", "4": "l", "5": "mn", "6": "r"}.items() for c in letters}
    key, last = w[0].upper(), codes.get(w[0], "")
    for ch in w[1:]:
        code = codes.get(ch, "")
        if code and code != last:
            key += code
        if ch not in "hw":   # h and w do not separate equal codes
            last = code
    return key + "0"   # not truncated: informations and information must stay different


def sounds_alike(a, b):
    """True when two texts differ only by one word that sounds like the other (their/there/they're, weather/whether):
    in dictated text that is a mis-transcription, not a mistake of the speaker."""
    wa, wb = a.lower().split(), b.lower().split()
    if len(wa) != len(wb) or not wa:
        return False
    differing = [(x, y) for x, y in zip(wa, wb) if x != y]
    return len(differing) == 1 and _soundex(differing[0][0]) != "" and _soundex(differing[0][0]) == _soundex(differing[0][1])


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))


def occurs_in(original, context_words):
    """True when `original` quotes the context: a run of at least three words shared with it that covers at least half
    of the flagged words. The flagged span is often a little longer than the quote ("What is <quoted title>")."""
    w = _words(original)
    if len(w) < 3 or not context_words:
        return False
    best, previous = 0, [0] * (len(context_words) + 1)
    for i in range(1, len(w) + 1):
        current = [0] * (len(context_words) + 1)
        for j in range(1, len(context_words) + 1):
            if w[i - 1] == context_words[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best >= 3 and 2 * best >= len(w)


def suppressed_phrases(c):
    """Lower-cased originals the user dismissed: these are never flagged again."""
    return {r[0] for r in c.execute("SELECT lower(original) FROM findings WHERE dismissed=1")}


def validate(answer, prompt, recent=(frozenset(), frozenset()), speech=False, context="", suppressed=frozenset()):
    """Keep only fixes whose original really occurs in the prompt and which change more than case/spacing."""
    low, spans, fixes = prompt.lower(), [], []
    context_words = _words(context)
    quoted = [m.span() for m in QUOTED.finditer(prompt)]
    for f in answer.get("fixes", []):
        if not isinstance(f, dict):
            continue
        orig, fix = clean(f.get("original"), 200), clean(f.get("fix"), 200)
        if not orig or not fix or skeleton(orig) == skeleton(fix):
            continue
        a = low.find(orig.lower())
        b = a + len(orig)
        if a == -1 or any(a < e and s < b for s, e in spans):
            continue
        if orig.lower() in suppressed:                          # the user said this is not a mistake of theirs
            continue
        if is_slip(orig, fix) or (speech and sounds_alike(orig, fix)):   # a typo, or a speech-recognition mishearing
            continue
        if context_words and occurs_in(orig, context_words):    # the user quotes what the assistant just wrote
            continue
        if repeats_recent(orig, fix, recent):                   # the same error was just corrected: a paste or paraphrase, not new writing
            continue
        if any(qs <= a and b <= qe for qs, qe in quoted):      # quoted or code text is somebody else's words
            continue
        if fix.lower().startswith(orig.lower()) or fix.lower().endswith(orig.lower()):
            continue                                            # only adds words around terse phrasing: not an error
        spans.append((a, b))
        cat = f.get("category") if f.get("category") in CATEGORIES else "other"
        if cat == "word-order" and sorted(_words(orig)) != sorted(_words(fix)):   # word order moves the same words; a fix that adds or drops one is something else
            cat = "phrasing"
        severity = "style" if f.get("severity") == "style" else "error"
        fixes.append((cat, int(bool(f.get("transfer"))), orig, fix, clean(f.get("explanation"), 300), severity))
    errors = [x for x in fixes if x[5] == "error"]
    styles = [x for x in fixes if x[5] == "style"][:1]            # at most one style item per message
    return (errors + styles)[:3]




LEAD_WORDS = {"the", "this", "that", "these", "those", "and", "but", "for", "with", "when", "then", "if", "it", "in", "on", "at", "we", "you", "so", "also", "see"}


def context_terms(context, text, limit=6):
    """Names and identifiers from the recent chat that the message seems to refer to (spoken, so possibly spelled out or misheard):
    `backticked` terms, snake_case, CamelCase and names of capitalised words. Only the ones that share words with the message are
    kept, so the reviewer gets a short, relevant hint and never the chat itself."""
    if not context or not text:
        return []
    found = {}
    for m in re.finditer(r"`([^`\n]{2,40})`", context):
        found[m.group(1).strip()] = 1
    for m in re.finditer(r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b", context):
        found[m.group(0)] = 1
    for m in re.finditer(r"\b[A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})+\b", context):
        words = m.group(0).split()
        while words and words[0].lower() in LEAD_WORDS:
            words = words[1:]
        if len(words) >= 2:
            found[" ".join(words)] = 1
    message = set(_words(text))
    scored = []
    for term in found:
        parts = [p.lower() for p in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", term)]
        if not parts or (len(parts) == 1 and len(parts[0]) < 6):
            continue
        hit = sum(1 for p in parts if p in message or (len(p) >= 4 and any(difflib.SequenceMatcher(None, p, w).ratio() >= 0.85 for w in message)))
        if hit >= max(1, (len(parts) + 1) // 2):
            scored.append((-hit / len(parts), -hit, term))
    return [t for _, _, t in sorted(scored)][:limit]


def build_system(c, speech=False, text="", context=""):
    top = [k for k, _ in sorted(recurrence(c).items(), key=lambda kv: -kv[1])[:3]]
    terms = context_terms(context, text)
    hint = (f"\nNames and identifiers from the recent conversation that the message may be quoting or reading out (possibly spelled out, "
            f"split into words or misheard); never report them as errors: {', '.join(terms)}.") if terms else ""
    return (system_prompt(speech, text, phrase_candidates(c, text)) + hint + (f"\nPay slightly more attention to this learner's recurring areas: {', '.join(top)}." if top else "")
            + watch_prompt(c))


def user_block(prompt):
    return f"<message>\n{prompt}\n</message>\nReview the message above. Reply with the JSON object only."


def answer_from(text):
    """The last JSON object in a model reply that carries a 'fixes' list, or None."""
    answers = [o for o in json_objects(text) if isinstance(o.get("fixes"), list)]
    return answers[-1] if answers else None


def habit_keys(original, fix):
    """The atomic changes of a correction as (wrong, right) pairs in lower-case words, e.g. ("defer", "derive") and
    ("out of", "from"). Keys do not depend on the sentence, so the same mistake matches in every message. An insertion or
    deletion is keyed with its neighbouring words ("explain me" -> "explain to me"); moved words are ignored."""
    a, b = _words(original), _words(fix)
    ops = [o for o in difflib.SequenceMatcher(None, a, b).get_opcodes() if o[0] != "equal"]
    removed = {t for tag, i1, i2, j1, j2 in ops if tag in ("delete", "replace") for t in a[i1:i2]}
    added = {t for tag, i1, i2, j1, j2 in ops if tag in ("insert", "replace") for t in b[j1:j2]}
    keys = []
    for tag, i1, i2, j1, j2 in ops:
        if i2 - i1 > 3 or j2 - j1 > 3:
            continue
        if tag == "insert" and set(b[j1:j2]) <= removed:       # a word that only moved
            continue
        if tag == "delete" and set(a[i1:i2]) <= added:
            continue
        if tag == "replace":
            key = (" ".join(a[i1:i2]), " ".join(b[j1:j2]))
        else:                                                   # insert or delete: keep a neighbour on each side
            lo, hi = max(0, i1 - 1), min(len(a), i2 + 1)
            key = (" ".join(a[lo:hi]), " ".join(b[max(0, j1 - (i1 - lo)):j2 + (hi - i2)]))
        if key[0] != key[1] and key not in keys:
            keys.append(key)
    if not keys and a != b and len(a) <= 8:                    # a short rewrite: track the whole phrase
        keys = [(" ".join(a), " ".join(b))]
    return keys[:3]


def uses_right(tokens, right):
    """The right word or phrase occurs in the message (a single word also in its inflected forms: derive/derived)."""
    r = right.split()
    if len(r) > 1:
        return any(tokens[i:i + len(r)] == r for i in range(len(tokens) - len(r) + 1))
    stem = right[:-1] if right.endswith("e") and len(right) > 4 else right
    return any(t == right or (len(stem) >= 4 and t.startswith(stem)) for t in tokens)


def update_habits(c, text, stored):
    """Count this message's corrections per habit and its correct uses. `stored` = [(original, fix, explanation)] of the
    error findings kept for this message. Returns ({lower original: reminder}, milestone note or None)."""
    now, tokens, hit_keys, reminders = time.time(), _words(text), set(), {}
    for orig, fix, expl in stored:
        for key in habit_keys(orig, fix):
            if key in hit_keys:                                  # one count per message, however often it occurs in it
                continue
            hit_keys.add(key)
            row = c.execute("SELECT id, hits FROM habits WHERE wrong=? AND right=?", key).fetchone()
            if row is None:
                c.execute("INSERT INTO habits(wrong, right, hits, last_hit, note) VALUES(?,?,1,?,?)", (*key, now, expl))
                hits = 1
            else:
                hits = row[1] + 1
                c.execute("UPDATE habits SET hits=?, last_hit=?, streak=0, status='active', note=COALESCE(NULLIF(?, ''), note) WHERE id=?",
                          (hits, now, expl, row[0]))
            if hits >= REMIND_AFTER and orig.lower() not in reminders:
                reminders[orig.lower()] = f"This one keeps coming back: \"{key[1]}\" instead of \"{key[0]}\"."
    milestone = None
    for hid, wrong, right, streak in c.execute("SELECT id, wrong, right, streak FROM habits WHERE status='active'").fetchall():
        if (wrong, right) in hit_keys or not uses_right(tokens, right):
            continue
        streak += 1
        done = streak >= RESOLVE_AFTER
        c.execute("UPDATE habits SET streak=?, correct_uses=correct_uses+1, status=? WHERE id=?", (streak, "resolved" if done else "active", hid))
        if done and milestone is None:
            milestone = f"\"{right}\" has been right {streak} times in a row, so it leaves your watchlist (was: \"{wrong}\")."
    return reminders, milestone


def recount_habits(c):
    """After a dismissal or restore: hits = the non-dismissed error findings that still map to the habit."""
    counts = {}
    for orig, fix in c.execute("SELECT original, fix FROM findings WHERE severity='error' AND dismissed=0"):
        for key in habit_keys(orig, fix):
            counts[key] = counts.get(key, 0) + 1
    for hid, wrong, right, source in c.execute("SELECT id, wrong, right, source FROM habits").fetchall():
        if source == "auto":
            c.execute("UPDATE habits SET hits=? WHERE id=?", (counts.get((wrong, right), 0), hid))


def watch_prompt(c):
    rows = c.execute("SELECT wrong, right FROM habits WHERE status='active' AND (hits>=? OR source='manual') "
                     "ORDER BY hits DESC LIMIT 5", (REMIND_AFTER,)).fetchall()
    if not rows:
        return ""
    return ("\nThis user keeps making these mistakes; look for them and flag one when it is clearly wrong in context "
            "(not when the word is used correctly): " + "; ".join(f'"{w}" instead of "{r}"' for w, r in rows) + ".")


# Small words whose omission is almost always haste (typed or dictated); in dictated text any small word may be a recognition error.
SUBJECT_AUX = {"i", "you", "he", "she", "it", "we", "they", "is", "are", "was", "were", "am", "do", "does", "did", "will", "would"}
FUNCTION_WORDS = SUBJECT_AUX | {"me", "him", "her", "us", "them", "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "by",
                                "be", "been", "has", "have", "had", "and", "or", "but", "that", "this", "these", "those", "if", "as", "so"}


def is_small_word_slip(original, fix, speech):
    """One small word missing from the text (typed: only subject pronouns and auxiliaries, whose omission is haste; dictated: any
    small word, since recognizers drop weak words). A performance slip, not a gap. In dictated text a word the speaker added, or
    swapped for one that does not sound alike, is their own wording and stays an error."""
    a, b = _words(original), _words(fix)
    ops = [o for o in difflib.SequenceMatcher(None, a, b).get_opcodes() if o[0] != "equal"]
    if len(ops) != 1:
        return False
    tag, i1, i2, j1, j2 = ops[0]
    removed, added = a[i1:i2], b[j1:j2]
    words = FUNCTION_WORDS if speech else SUBJECT_AUX
    if tag == "insert":                                    # the fix adds a word that is missing from the text
        return len(added) == 1 and added[0] in words
    if tag == "delete":                                    # the text has a word the fix removes
        return len(removed) == 1 and removed[0] in words and not speech
    return (tag == "replace" and speech and len(removed) == 1 and len(added) == 1 and removed[0] in words and added[0] in words
            and sounds_alike(removed[0], added[0]))


def classify_slips(c, fixes, speech):
    """Errors that are only a small forgotten or misrecognized word become severity `slip` (stored, not shown, not counted as
    errors), unless the same omission already happened REMIND_AFTER-1 times: a repeated one points to a gap and stays an error."""
    out = []
    for cat, tr, orig, fix, expl, sev in fixes:
        if sev == "error" and is_small_word_slip(orig, fix, speech):
            before = 0
            for key in habit_keys(orig, fix):
                row = c.execute("SELECT hits FROM habits WHERE wrong=? AND right=?", key).fetchone()
                before = max(before, row[0] if row else 0)
            if before < REMIND_AFTER - 1:
                sev = "slip"
        out.append((cat, tr, orig, fix, expl, sev))
    return out


SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+|\n+")


def sentence_spans(text):
    """(start, end) of each sentence: split after . ! ? or at a line break; fenced code and blockquote lines do not count."""
    skip = [m.span() for m in re.finditer(r"```.*?```|^[ \t]*>[^\n]*$", text, re.S | re.M)]
    spans, start = [], 0
    for m in list(SENTENCE_BREAK.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        if end > start and re.search(r"\w", text[start:end]) and not any(a <= start and end <= b for a, b in skip):
            spans.append((start, end))
        if m:
            start = m.end()
    return spans


def sentence_index(spans, text, original):
    """Index of the sentence in which `original` starts, or None when it cannot be located."""
    pos = text.lower().find(original.lower())
    if pos < 0:
        return None
    return next((i for i, (a, b) in enumerate(spans) if a <= pos < b or (pos < a and i == 0)), None)


def sloppy_form(prompt):
    """Necessary condition for careless mode: the message *looks* dashed off (starts lowercase or has no closing
    punctuation). Keeps a single model judgement from silencing feedback on a carefully written message."""
    text = prompt.strip()
    first = next((ch for ch in text if ch.isalpha()), "")
    return first.islower() or not text.endswith((".", "!", "?", "…", ")", '"'))


PHRASE_AFTER = (0, 1, 3, 7, 7)   # fitting occasions to let pass before the next showing, by how often the phrase was shown
PHRASE_SHOWINGS = 5               # a phrase retires after this many showings
PHRASE_USES = 2                   # ... or after the user used it this many times by themselves
PHRASE_GAP = 4                    # messages between two suggestions
PHRASE_OFFERED = 8                # phrases the reviewer is asked about per message


def phrase_list():
    try:
        return json.loads((Path(__file__).resolve().parent.parent / "data" / "phrases.json").read_text())
    except Exception:
        return []


def _phrase_in(entry, text):
    return bool(re.search(r"\b(?:" + (entry.get("pattern") or re.escape(entry["phrase"])) + r")\b", text, re.I))


def phrase_status(c):
    return {r[0]: r for r in c.execute("SELECT id, shown, skipped, uses, status, last_shown FROM phrases")}


def phrase_candidates(c, text):
    """A random handful of phrases still up for suggestion, minus the ones the message already uses. Which of them fit is
    the reviewer's call; the schedule below decides whether a fit is shown."""
    state = phrase_status(c)
    open_ = [e for e in phrase_list() if state.get(e["id"], (0, 0, 0, 0, "active"))[4] == "active" and not _phrase_in(e, text)]
    forced = [e for e in open_ if e["id"] in os.environ.get("ENGLISH_COACH_OFFER", "").split(",")]   # for the eval: make sure these are asked about
    rest = [e for e in open_ if e not in forced]
    return forced + random.sample(rest, min(max(0, PHRASE_OFFERED - len(forced)), len(rest)))


def note_phrase_uses(c, text):
    """Count the phrases the user wrote by themselves; after PHRASE_USES such messages a phrase counts as learned."""
    state = phrase_status(c)
    for e in phrase_list():
        if _phrase_in(e, text):
            row = state.get(e["id"], (e["id"], 0, 0, 0, "active", None))
            uses = row[3] + 1
            status = "learned" if row[4] == "active" and uses >= PHRASE_USES else row[4]
            c.execute("INSERT INTO phrases(id, shown, skipped, uses, status, last_shown) VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET uses=excluded.uses, status=excluded.status", (e["id"], row[1], row[2], uses, status, row[5]))


def pick_phrase(c, prompt, answer, prompt_id, fixes):
    """The one phrase suggestion to show for this message, or None. The reviewer says which phrases fit; each fit is an
    occasion. A phrase is shown on its first occasion, then after 1, 3, 7 and 7 occasions let pass, never by calendar, and retires after
    PHRASE_SHOWINGS showings. Messages with a correction on the same words, quoted text, or too soon after the last suggestion get none."""
    entries = {e["id"]: e for e in phrase_list()}
    low, quoted = prompt.lower(), [m.span() for m in QUOTED.finditer(prompt)]
    reported = answer.get("phrases") if isinstance(answer.get("phrases"), list) else []
    if get_state(c, "last_phrase_prompt", 0) and prompt_id - get_state(c, "last_phrase_prompt", 0) < PHRASE_GAP:
        return None
    state = phrase_status(c)
    for item in reported:
        entry = entries.get(item.get("id")) if isinstance(item, dict) else None
        original = clean(item.get("original"), 120) if entry else ""
        if not entry or not original or state.get(entry["id"], (0, 0, 0, 0, "active"))[4] != "active" or _phrase_in(entry, prompt):
            continue
        a = low.find(original.lower())
        b = a + len(original)
        if a == -1 or any(qs <= a and b <= qe for qs, qe in quoted) or any(f[2].lower() in low and a < low.find(f[2].lower()) + len(f[2]) and low.find(f[2].lower()) < b for f in fixes):
            continue
        _, shown, skipped, uses, status, _last = state.get(entry["id"], (entry["id"], 0, 0, 0, "active", None))
        if skipped < PHRASE_AFTER[min(shown, len(PHRASE_AFTER) - 1)]:   # a fitting occasion that is let pass
            c.execute("INSERT INTO phrases(id, shown, skipped, uses, status) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET skipped=skipped+1",
                      (entry["id"], shown, 1, uses, status))
            continue
        shown += 1
        c.execute("INSERT INTO phrases(id, shown, skipped, uses, status, last_shown) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET shown=excluded.shown, skipped=0, status=excluded.status, last_shown=excluded.last_shown",
                  (entry["id"], shown, 0, uses, "retired" if shown >= PHRASE_SHOWINGS else "active", time.time()))
        set_state(c, "last_phrase_prompt", prompt_id)
        set_state(c, "last_phrase_id", entry["id"])
        return {"id": entry["id"], "phrase": entry["phrase"], "meaning": entry["meaning"], "example": entry["example"], "original": original}
    return None


def store_answer(c, prompt, answer, mode="written", context=""):
    """Validate and store a review. Returns (careless, [(category, transfer, original, fix, explanation, severity)],
    {"reminders": {lower original: text}, "milestone": text or None, "phrase": suggestion or None})."""
    now = time.time()
    if mode == "written" and answer.get("careless") is True and sloppy_form(prompt):   # sloppy mode: no tips, excluded from all statistics
        set_state(c, "last_careless", now)
        c.commit()
        return True, [], {"reminders": {}, "milestone": None, "phrase": None, "ids": {}, "prompt_id": 0}
    well = [x for x in answer.get("did_well", []) if x in CATEGORIES]
    spans = sentence_spans(prompt)
    pid = c.execute("INSERT INTO prompts(ts, words, did_well, mode, sentences) VALUES(?,?,?,?,?)",
                    (now, len(re.findall(r"\w+", prompt)), json.dumps(well), mode, len(spans) or 1)).lastrowid
    fixes = validate(answer, prompt, recent_phrases(c), speech=(mode == "dictated"), context=context,
                     suppressed=suppressed_phrases(c))
    fixes = classify_slips(c, fixes, speech=(mode == "dictated"))
    ids = {}
    for cat, tr, orig, fix, expl, severity in fixes:
        ids[orig.lower()] = c.execute("INSERT INTO findings(prompt_id, ts, category, transfer, original, fix, explanation, shown, mode, severity, sent_idx) "
                                      "VALUES(?,?,?,?,?,?,?,1,?,?,?)", (pid, now, cat, tr, orig, fix, expl, mode, severity, sentence_index(spans, prompt, orig))).lastrowid
    reminders, milestone = update_habits(c, prompt, [(o, f, e) for _, _, o, f, e, sev in fixes if sev in ("error", "slip")])
    note_phrase_uses(c, prompt)
    phrase = pick_phrase(c, prompt, answer, pid, fixes)
    c.commit()
    return False, fixes, {"reminders": reminders, "milestone": milestone, "phrase": phrase, "ids": ids, "prompt_id": pid}


def prepare_cmd():
    """Mod path, step 1. stdin: {"prompt", "context": recent assistant text, optional}.
    stdout: {"coach": bool, "text": the prompt without the dictation marker, "mode": "written"|"dictated",
             "strip": whether the mod should hand the model the text without the marker, "system", "user"}."""
    try:
        data = json.load(sys.stdin)
        prompt, context = (data.get("prompt") or "").strip(), data.get("context") or ""
    except Exception:
        prompt, context = "", ""
    text, speech = split_marker(prompt)
    out = {"coach": False, "text": text, "mode": "dictated" if speech else "written",
           "strip": bool(speech and CFG["strip_marker"])}
    if not PAUSED.exists() and coachable(text):
        out.update(coach=True, system=build_system(db(), speech, text, context), user=user_block(text))
    out["escalate_model"] = CFG["escalate_model"] if speech else None
    print(json.dumps(out))


def check_cmd():
    """Mod path, between prepare and ingest. stdin like ingest's. stdout: {"escalate": bool, "model": str}.
    A dictated message whose first review marks a surviving finding as `unclear` (the passage is garbled, likely a
    mis-transcription) gets a second review by the larger model before anything is stored."""
    model = CFG["escalate_model"]
    out = {"escalate": False, "model": model or "", "note": ""}
    try:
        data = json.load(sys.stdin)
        prompt, answer = (data.get("prompt") or "").strip(), answer_from(data.get("reply") or "")
    except Exception:
        prompt, answer = "", None
    if model and model.lower() != "none" and data.get("mode") == "dictated" and prompt and answer:
        c = db()
        kept = {f[2] for f in validate(answer, prompt, recent_phrases(c), speech=True, context=data.get("context") or "",
                                       suppressed=suppressed_phrases(c))}
        passages = [clean(f.get("original"), 200) for f in answer.get("fixes", [])
                    if isinstance(f, dict) and f.get("unclear") is True and clean(f.get("original"), 200) in kept]
        out["escalate"] = bool(passages)
        if passages:
            out["note"] = ("\n\nA first review found these passages hard to make sense of: " + "; ".join(f'"{p}"' for p in passages)
                           + ". If one of them is most likely a speech-recognition mishearing (similar-sounding words that would make "
                           "sense), it is not a mistake of the speaker: do not report it.")
    print(json.dumps(out))


def ingest_cmd():
    """Mod path, step 2. stdin: {"prompt": text without marker, "mode", "reply", "context": recent assistant text}. Stores the review;
    stdout: {"careless", "btw", "hint", "fixes": [...]}. Findings are stored as already shown, because the mod
    draws them inline."""
    try:
        data = json.load(sys.stdin)
        prompt, answer = (data.get("prompt") or "").strip(), answer_from(data.get("reply") or "")
        mode = "dictated" if data.get("mode") == "dictated" else "written"
        context = data.get("context") or ""   # the assistant's last replies; used for this call only, never stored
    except Exception:
        prompt, answer, mode, context = "", None, "written", ""
    if not prompt or answer is None:
        print(json.dumps({"ok": False}))
        return
    c = db()
    set_state(c, "context_chars_last", len(context))        # numbers only: lets a missing context (a quote guard that cannot work) be noticed
    set_state(c, "context_empty", get_state(c, "context_empty") + (0 if context else 1))
    set_state(c, "context_reviews", get_state(c, "context_reviews") + 1)
    careless, fixes, extras = store_answer(c, prompt, answer, mode, context)
    rec = recurrence(c)
    errors = sorted((f for f in fixes if f[5] == "error"), key=lambda f: -rec.get(f[0], 0))[:MAX_TIPS]   # recurring areas first
    styles = [f for f in fixes if f[5] == "style"] if CFG["show_style"] else []                          # stored either way
    fixes = errors + styles
    btw = extras["milestone"] or (None if careless or mode != "written" else maybe_btw(c))
    hint = setup_hint(c)
    c.commit()
    icon = CFG["icon_dictated" if mode == "dictated" else "icon_written"] or DEFAULTS["icon_dictated" if mode == "dictated" else "icon_written"]
    audit = audit_request(prompt, context, mode, fixes, extras) if not careless else None
    print(json.dumps({"ok": True, "careless": careless, "btw": btw, "hint": hint, "icon": icon, "phrase": extras["phrase"], "audit": audit,
                      "fixes": [{"category": cat, "transfer": bool(tr), "original": o, "fix": f, "explanation": e, "severity": sev,
                                "reminder": extras["reminders"].get(o.lower()), "parts": diff_parts(o, f), "id": extras["ids"].get(o.lower())}
                                for cat, tr, o, f, e, sev in fixes]}))


def diff_parts(original, fix, context=2):
    """The correction as word segments for display: {"original": [[text, changed], ...], "fix": [...]}. Words that did not change
    stay, up to `context` of them on each side of a change ("…" for the rest), so a long fragment with a one-word fix shows
    that word and its neighbours, and only the changed words are coloured."""
    a, b = original.split(), fix.split()
    norm = lambda t: re.sub(r"[^\w']+", "", t.lower())
    ops = [o for o in difflib.SequenceMatcher(None, [norm(t) for t in a], [norm(t) for t in b], autojunk=False).get_opcodes() if o[0] != "equal"]

    def side(tokens, which):
        changed, near = [False] * len(tokens), [False] * len(tokens)
        for tag, i1, i2, j1, j2 in ops:
            lo, hi = (i1, i2) if which == 0 else (j1, j2)
            for k in range(lo, hi):
                changed[k] = True
            for k in range(max(0, lo - context), min(len(tokens), hi + context)):   # also around a pure insertion or deletion
                near[k] = True
        if not any(near):
            return [[" ".join(tokens), True]]
        parts, run, gap = [], [], False
        for k, token in enumerate(tokens):
            if not near[k]:
                gap = True
                continue
            if gap and (run or parts):
                if run:
                    parts.append([" ".join(run), run_changed]); run = []
                parts.append(["…", False])
            elif gap:
                parts.append(["…", False])
            gap = False
            if run and changed[k] != run_changed:
                parts.append([" ".join(run), run_changed]); run = []
            run_changed = changed[k] if not run else run_changed
            run.append(token)
        if run:
            parts.append([" ".join(run), run_changed])
        if gap:
            parts.append(["…", False])
        return parts

    return {"original": side(a, 0), "fix": side(b, 1)}


AUDIT_VERDICTS = ("ok", "not-error", "style", "wrong-fix", "wrong-category")


def audit_system(speech, scope):
    who = f"a non-native English speaker (native language: {NATIVE})" if NATIVE else "a non-native English speaker"
    speech_note = ("\nThis message was DICTATED: speech-recognition mishearings, missing punctuation, filler words, false starts, run-ons and the pauses a "
                   "speech tool removes (a topic named first, then a question about it) are not the speaker's errors.") if speech else ""
    missed = (' Also list up to 2 clear errors the coach missed, as "missed": original copied verbatim from the message, fix, category, reason. List none '
              "unless a native editor would certainly mark it; the same limits about what is not an error apply.") if scope == "all" else ""
    missed_field = ',"missed":[{"original":"","fix":"","category":"","reason":""}]' if scope == "all" else ""
    return f"""You are a senior editor giving a second opinion on the tips of a junior English coach. The writer is {who}. For each tip, decide whether a native-speaking senior engineer would really mark the original as an error. Use the whole message and the recent chat, which the coach saw only in part.

Verdicts:
- ok: a real error and the fix is right.
- not-error: correct, natural English, or only taste, register, informal chat style, a quote or pasted text, a typo, or a speech-recognition artifact.
- style: correct, but a native would word it differently.
- wrong-fix: a real problem, but the fix changes the meaning or is itself wrong.
- wrong-category: a real error with a right fix, but the category is wrong; give the right one (one of {', '.join(CATEGORIES)}).{speech_note}

When unsure, answer ok: the coach is allowed to be right. Keep each reason to one short sentence.{missed}
Output ONLY one raw JSON object, no fences: {{"verdicts":[{{"id":0,"verdict":"ok","category":"","reason":""}}]{missed_field}}}"""


def audit_request(prompt, context, mode, fixes, extras):
    """The second-opinion request the mod sends to the larger model after a review, or None when the audit is off or has nothing to
    check. It carries the whole message and the chat context for this one call only; only the verdicts are stored."""
    tips = [{"id": extras["ids"][o.lower()], "original": o, "fix": f, "category": cat, "severity": sev, "explanation": e}
            for cat, tr, o, f, e, sev in fixes if sev in ("error", "style") and o.lower() in extras["ids"]]
    if AUDIT == "off" or (AUDIT == "flagged" and not tips):
        return None
    block = {"message": prompt, "recent_chat": (context or "")[-600:], "tips": tips}
    return {"model": CFG["audit_model"] or "sonnet", "scope": AUDIT, "prompt_id": extras["prompt_id"],
            "system": audit_system(mode == "dictated", AUDIT), "user": json.dumps(block, ensure_ascii=False)}


def audit_store_cmd():
    """stdin {"reply", "prompt", "prompt_id"}: store the larger model's verdicts (never the message) for this review's tips, and, in
    scope `all`, the errors it says the coach missed (fragments that really occur in the message)."""
    try:
        data = json.load(sys.stdin)
        prompt, pid = (data.get("prompt") or "").strip(), int(data.get("prompt_id") or 0)
        answers = [o for o in json_objects(data.get("reply") or "") if isinstance(o.get("verdicts"), list)]
        answer = answers[-1] if answers else None
    except Exception:
        answer = None
    if answer is None or not pid:
        print(json.dumps({"ok": False}))
        return
    c, now, stored, missed = db(), time.time(), 0, 0
    for v in answer["verdicts"]:
        if not isinstance(v, dict) or v.get("verdict") not in AUDIT_VERDICTS:
            continue
        try:
            fid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        if not c.execute("SELECT 1 FROM findings WHERE id=? AND prompt_id=?", (fid, pid)).fetchone():
            continue                                         # only tips of this very review
        cat = v.get("category") if v.get("verdict") == "wrong-category" and v.get("category") in CATEGORIES else ""
        c.execute("INSERT OR REPLACE INTO audits(finding_id, verdict, category, reason, ts, ack) VALUES(?,?,?,?,?,0)",
                  (fid, v["verdict"], cat, clean(v.get("reason"), 200), now))
        stored += 1
    if AUDIT == "all":
        low, quoted = prompt.lower(), [m.span() for m in QUOTED.finditer(prompt)]
        for m in (answer.get("missed") if isinstance(answer.get("missed"), list) else [])[:2]:
            if not isinstance(m, dict):
                continue
            orig, fix = clean(m.get("original"), 200), clean(m.get("fix"), 200)
            a = low.find(orig.lower()) if orig else -1
            if a == -1 or not fix or skeleton(orig) == skeleton(fix) or any(qs <= a and a + len(orig) <= qe for qs, qe in quoted):
                continue
            c.execute("INSERT INTO audit_misses(prompt_id, ts, category, original, fix, reason) VALUES(?,?,?,?,?,?)",
                      (pid, now, m.get("category") if m.get("category") in CATEGORIES else "other", orig, fix, clean(m.get("reason"), 200)))
            missed += 1
    c.commit()
    print(json.dumps({"ok": True, "verdicts": stored, "missed": missed}))


def audit_data(c, marks, modes):
    """The audit block of the overview: how often the second opinion agrees, and the tips it doubts that are still open."""
    rows = c.execute(f"SELECT a.finding_id, a.verdict, a.category, a.reason, a.ack, f.original, f.fix, f.severity, f.dismissed, f.category FROM audits a "
                     f"JOIN findings f ON f.id=a.finding_id WHERE f.mode IN ({marks}) ORDER BY a.finding_id DESC", modes).fetchall()
    agree = lambda verdict, severity: verdict == "ok" or (verdict == "style" and severity == "style")
    live = [r for r in rows if not r[8]]
    suspects = [{"id": r[0], "verdict": r[1], "category": r[2], "reason": r[3], "original": r[5], "fix": r[6], "current": r[9],
                 "core": context_change(r[5], r[6])} for r in live if not agree(r[1], r[7]) and not r[4]][:8]
    misses = c.execute("SELECT COUNT(*) FROM audit_misses").fetchone()[0]
    return {"scope": AUDIT, "audited": len(live), "agree": sum(1 for r in live if agree(r[1], r[7])), "suspects": suspects, "misses": misses}


def audit_cmd(args):
    """audit: how the larger model rates the tips and which ones it doubts. audit ack <id>: keep a doubted tip (it leaves the list)."""
    c = db()
    if args[:1] == ["ack"] and len(args) > 1 and args[1].isdigit():
        n = c.execute("UPDATE audits SET ack=1 WHERE finding_id=?", (int(args[1]),)).rowcount
        c.commit()
        print(f"Kept tip {args[1]}; it leaves the list." if n else f"No audit verdict for tip {args[1]}.")
        return
    data = audit_data(c, "?,?", ("written", "dictated"))
    if not data["audited"]:
        print("No second opinions yet." + ("" if AUDIT != "off" else " Switch it on with: config set audit flagged  (or all)."))
        return
    print(f"Second opinion agrees with {data['agree']} of {data['audited']} tips ({round(100 * data['agree'] / data['audited'])}%). Missed errors noted: {data['misses']}.")
    for s in data["suspects"]:
        extra = f" (suggested category: {s['category']})" if s["category"] else ""
        print(f"  {s['id']:4} {s['original']} -> {s['fix']}   [{s['verdict']}]{extra}  {s['reason']}")
    if data["suspects"]:
        print("\nNot an error? dismiss <id> [taste|false|asr|quote]    keep the tip: audit ack <id>")


def core_change(original, fix):
    """The changed words of a correction, with a little context when the change itself is tiny ("i" -> "i'll" says nothing, "i just try"
    -> "i'll just try" does). Falls back to a shortened phrase when there is no clean change."""
    a, b = _words(original), _words(fix)
    ops = [o for o in difflib.SequenceMatcher(None, a, b).get_opcodes() if o[0] != "equal"]
    if len(ops) == 1:
        tag, i1, i2, j1, j2 = ops[0]
        changed = " ".join(a[i1:i2] + b[j1:j2])
        tiny = len(changed.replace(" ", "")) <= 6 or any(t in FUNCTION_WORDS or "'" in t for t in a[i1:i2] + b[j1:j2])
        if tiny:
            lo, hi = max(0, i1 - 2), min(len(a), i2 + 2)
            return {"wrong": " ".join(a[lo:hi]), "right": " ".join(b[max(0, j1 - (i1 - lo)):j2 + (hi - i2)])}
    keys = habit_keys(original, fix)
    if keys:
        # prefer the change that is about more than a small word ("not changing" -> "doesn't change" over "or mouse" -> "or the mouse")
        informative = [k for k in keys if not set(k[0].split()) ^ set(k[1].split()) <= FUNCTION_WORDS]
        key = (informative or keys)[0]
        return {"wrong": key[0], "right": key[1]}
    return {"wrong": original[:60], "right": fix[:60]}


def context_change(original, fix, context=2):
    """The changed stretch of a correction with `context` words on each side, for lists where a lone word pair ("take" -> "use")
    would say nothing."""
    a, b = original.split(), fix.split()
    norm = lambda t: re.sub(r"[^\w']+", "", t.lower())
    ops = [o for o in difflib.SequenceMatcher(None, [norm(t) for t in a], [norm(t) for t in b], autojunk=False).get_opcodes() if o[0] != "equal"]
    if not ops:
        return {"wrong": original, "right": fix}
    i1, i2, j1, j2 = ops[0][1], ops[-1][2], ops[0][3], ops[-1][4]
    lead, tail = min(context, i1, j1), min(context, len(a) - i2, len(b) - j2)
    return {"wrong": " ".join(a[i1 - lead:i2 + tail]), "right": " ".join(b[j1 - lead:j2 + tail])}


def summary_due_cmd(args=()):
    """Which modes should get a fresh written summary now, for the mod's checks: stdout {"modes": [...]}. A mode is due when it has at
    least 10 errors, no summary yet or `summary_every` new errors since the last one, and no attempt in the last 6 hours (10 minutes when
    the overview pane was just opened: `--open`, so a first report is prepared as soon as someone looks)."""
    cooldown = 600 if "--open" in args else 6 * 3600
    out = {"modes": []}
    if CFG["auto_summary"]:
        c, now = db(), time.time()
        for mode in ("both", "written", "dictated"):
            modes = ("written", "dictated") if mode == "both" else (mode,)
            marks = ",".join("?" * len(modes))
            errors = c.execute(f"SELECT COUNT(*) FROM findings WHERE mode IN ({marks}) AND severity='error' AND dismissed=0", modes).fetchone()[0]
            row = c.execute("SELECT n FROM summaries WHERE mode=? AND category='_focus'", (mode,)).fetchone()
            attempted = get_state(c, f"summary_attempt_{mode}")
            if errors >= 10 and (row is None or errors - row[0] >= max(1, int(CFG["summary_every"]))) and now - attempted > cooldown:
                out["modes"].append(mode)
    print(json.dumps(out))


def summary_input_cmd(args):
    """Input for the written category summaries: stdout {"model", "system", "prompt"}; the mod sends it to the larger model and passes the
    reply to `summary-store`. The corrections are the learner's own short fragments, the same ones the reviews already sent."""
    mode = args[0] if args and args[0] in ("written", "dictated", "both") else "both"
    modes = ("written", "dictated") if mode == "both" else (mode,)
    marks = ",".join("?" * len(modes))
    c = db()
    set_state(c, f"summary_attempt_{mode}", time.time())      # a failed attempt is not repeated for six hours
    c.commit()
    groups = {}
    for cat, orig, fix, expl, tr in c.execute(
            f"SELECT category, original, fix, explanation, transfer FROM findings WHERE mode IN ({marks}) AND severity='error' AND dismissed=0 "
            "ORDER BY id DESC LIMIT 400", modes):
        if len(groups.setdefault(cat, [])) < 10:
            groups[cat].append({"original": orig, "fix": fix, "explanation": expl, "copies_native_language": bool(tr)})
    system = ("You are an English coach summarizing the recurring problems of ONE learner"
              + (f" whose native language is {NATIVE}" if NATIVE else "") + ". You get their corrections grouped by category; each has "
              "the learner's words, the fix and a short explanation. For EACH category write one or two plain sentences that state the general "
              "pattern and the rule. For each category give a HEADLINE (one sentence of at most 100 characters: the rule in plain words) and a DETAIL "
              "(at most two sentences, up to 250 characters: what the learner tends to do and what English needs instead, with at most two tiny "
              "examples in quotes). Do not mention counts, do not praise, do not list all examples, and do not invent a pattern when the examples "
              "show none (then the headline says they look like one-off slips). Then give up to three 'focus' sentences of at most 140 characters: "
              "the most useful things to work on, most important first. Reply with ONLY a JSON object: "
              "{\"categories\": {\"<category>\": {\"headline\": \"<text>\", \"detail\": \"<text>\"}}, \"focus\": [\"<text>\"]}.")
    print(json.dumps({"model": CFG["summary_model"] or "sonnet", "system": system,
                      "prompt": json.dumps(groups, ensure_ascii=False), "categories": sorted(groups)}))


def summary_store_cmd(args):
    """stdin {"reply": model text}. Stores the category summaries and the focus list for the given mode."""
    mode = args[0] if args and args[0] in ("written", "dictated", "both") else "both"
    try:
        answer = next(o for o in reversed(json_objects(json.load(sys.stdin).get("reply") or "")) if isinstance(o.get("categories"), dict))
    except (StopIteration, ValueError):
        print(json.dumps({"ok": False}))
        return
    overview = overview_data(mode)
    totals = {cat["category"]: cat["total"] for cat in overview["categories"]}
    c = db()
    stored = 0
    for cat, value in answer["categories"].items():
        if isinstance(value, str):                                   # an older or looser answer: the text is the headline
            value = {"headline": value, "detail": ""}
        if cat in totals and isinstance(value, dict) and isinstance(value.get("headline"), str) and value["headline"].strip():
            text = json.dumps({"headline": clean(value["headline"], 140), "detail": clean(value.get("detail") or "", 320)}, ensure_ascii=False)
            c.execute("INSERT OR REPLACE INTO summaries(mode, category, n, text, ts) VALUES(?,?,?,?,?)", (mode, cat, totals[cat], text, time.time()))
            stored += 1
    focus = [clean(f, 300) for f in answer.get("focus", []) if isinstance(f, str) and f.strip()][:3]
    if focus:
        c.execute("INSERT OR REPLACE INTO summaries(mode, category, n, text, ts) VALUES(?,?,?,?,?)", (mode, "_focus", overview["errors"], json.dumps(focus), time.time()))
    c.commit()
    print(json.dumps({"ok": True, "stored": stored, "focus": len(focus)}))


def overview_data(mode="both"):
    """Everything the overview pane draws, as plain data. `mode`: written, dictated or both. Statistics count errors only;
    slips and style items are listed apart. Weekly lists run oldest to newest, 8 weeks, None where there was no data."""
    modes = ("written", "dictated") if mode == "both" else ("dictated" if mode == "dictated" else "written",)
    marks = ",".join("?" * len(modes))
    c = db()
    now, W, WEEKS = time.time(), 7 * 86400, 8

    def bucket(w):                     # w = 0 is the most recent 7 days
        return now - (w + 1) * W, now - w * W + (1 if w == 0 else 0)

    def words_between(lo, hi):
        return c.execute(f"SELECT COALESCE(SUM(words),0) FROM prompts WHERE mode IN ({marks}) AND ts>=? AND ts<?", (*modes, lo, hi)).fetchone()[0]

    def count(sql_where, params=()):
        return c.execute(f"SELECT COUNT(*) FROM findings WHERE mode IN ({marks}) AND dismissed=0 AND {sql_where}", (*modes, *params)).fetchone()[0]

    messages, words, first = c.execute(f"SELECT COUNT(*), COALESCE(SUM(words),0), MIN(ts) FROM prompts WHERE mode IN ({marks})", modes).fetchone()
    clean = []
    for w in range(WEEKS - 1, -1, -1):
        lo, hi = bucket(w)
        n = c.execute(f"SELECT COUNT(*) FROM prompts WHERE mode IN ({marks}) AND ts>=? AND ts<?", (*modes, lo, hi)).fetchone()[0]
        k = c.execute(f"SELECT COUNT(*) FROM prompts p WHERE mode IN ({marks}) AND ts>=? AND ts<? AND NOT EXISTS "
                      "(SELECT 1 FROM findings f WHERE f.prompt_id=p.id AND f.severity='error' AND f.dismissed=0)", (*modes, lo, hi)).fetchone()[0]
        clean.append(k / n if n else None)

    def rates(where, params=()):
        out = []
        for w in range(WEEKS - 1, -1, -1):
            lo, hi = bucket(w)
            wd = words_between(lo, hi)
            out.append(100 * count(f"{where} AND ts>=? AND ts<?", (*params, lo, hi)) / wd if wd else None)
        return out

    def trend(where, params=()):
        old_w, new_w = words_between(now - 8 * W, now - 4 * W), words_between(now - 4 * W, now + 1)
        if old_w < 200 or new_w < 200:
            return "n/a"
        old = 100 * count(f"{where} AND ts>=? AND ts<?", (*params, now - 8 * W, now - 4 * W)) / old_w
        new = 100 * count(f"{where} AND ts>=? AND ts<?", (*params, now - 4 * W, now + 1)) / new_w
        return "down" if new < 0.7 * old else "up" if new > 1.3 * old else "flat"

    categories = []
    for (cat,) in c.execute(f"SELECT category FROM findings WHERE mode IN ({marks}) AND severity='error' AND dismissed=0 AND ts>? "
                            "GROUP BY category ORDER BY COUNT(*) DESC", (*modes, now - WEEKS * W)).fetchall():
        where, params = "severity='error' AND category=?", (cat,)
        recent = [{"original": o, "fix": f, "explanation": e, "transfer": bool(t), "core": core_change(o, f)} for o, f, e, t in c.execute(
            f"SELECT original, fix, explanation, transfer FROM findings WHERE mode IN ({marks}) AND severity='error' AND dismissed=0 "
            "AND category=? ORDER BY id DESC LIMIT 12", (*modes, cat))]
        seen = Counter((x["core"]["wrong"], x["core"]["right"]) for x in recent)
        examples = sorted(recent, key=lambda x: -seen[(x["core"]["wrong"], x["core"]["right"])])[:3]      # recurring changes first, then the newest
        row = c.execute("SELECT text, n FROM summaries WHERE mode=? AND category=?", (mode, cat)).fetchone()
        total = count(where, params)
        summary = None
        if row:
            try:
                summary = json.loads(row[0])
            except ValueError:
                summary = {"headline": row[0], "detail": ""}
            if not isinstance(summary, dict):
                summary = {"headline": str(summary), "detail": ""}
        categories.append({"category": cat, "total": total, "weekly": rates(where, params), "trend": trend(where, params), "examples": examples,
                           "summary": summary, "summary_stale": bool(row and row[1] != total)})

    def listing(severity, limit=3):
        return [{"original": o, "fix": f, "explanation": e, "core": context_change(o, f)} for o, f, e in c.execute(
            f"SELECT original, fix, explanation FROM findings WHERE mode IN ({marks}) AND severity=? AND dismissed=0 ORDER BY id DESC LIMIT ?",
            (*modes, severity, limit))]

    watch = [{"wrong": w, "right": r, "hits": h, "streak": st, "status": status, "note": note or "", "manual": src == "manual"}
             for w, r, h, st, status, note, src in c.execute(
                 "SELECT wrong, right, hits, streak, status, note, source FROM habits WHERE hits>=? OR source='manual' "
                 "ORDER BY (status='active') DESC, hits DESC LIMIT 8", (REMIND_AFTER,))]
    per_mode = {m: c.execute("SELECT COUNT(*) FROM prompts WHERE mode=?", (m,)).fetchone()[0] for m in ("written", "dictated")}
    errors_total, transfer_total = count("severity='error'"), count("severity='error' AND transfer=1")
    focus_row = c.execute("SELECT text, n FROM summaries WHERE mode=? AND category='_focus'", (mode,)).fetchone()
    focus, focus_stale = (json.loads(focus_row[0]), focus_row[1] != errors_total) if focus_row else ([], False)

    # accuracy that does not depend on how long the messages are: errors per 100 words, and the share of error-free sentences
    sentences = c.execute(f"SELECT COALESCE(SUM(sentences),0) FROM prompts WHERE mode IN ({marks}) AND sentences IS NOT NULL", modes).fetchone()[0]
    located = c.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT f.prompt_id, f.sent_idx FROM findings f JOIN prompts p ON p.id=f.prompt_id "
                        f"WHERE f.mode IN ({marks}) AND f.severity='error' AND f.dismissed=0 AND f.sent_idx IS NOT NULL AND p.sentences IS NOT NULL)", modes).fetchone()[0]
    unlocated = c.execute(f"SELECT COUNT(*) FROM findings f JOIN prompts p ON p.id=f.prompt_id WHERE f.mode IN ({marks}) AND f.severity='error' "
                          "AND f.dismissed=0 AND f.sent_idx IS NULL AND p.sentences IS NOT NULL", modes).fetchone()[0]
    error_sentences = min(sentences, located + unlocated)
    length_buckets = []
    for label, lo, hi in (("1-10 words", 1, 10), ("11-25 words", 11, 25), ("26-60 words", 26, 60), ("61+ words", 61, 10**6)):
        n, w = c.execute(f"SELECT COUNT(*), COALESCE(SUM(words),0) FROM prompts WHERE mode IN ({marks}) AND words BETWEEN ? AND ?", (*modes, lo, hi)).fetchone()
        if not n:
            continue
        clean_n = c.execute(f"SELECT COUNT(*) FROM prompts p WHERE mode IN ({marks}) AND words BETWEEN ? AND ? AND NOT EXISTS "
                            "(SELECT 1 FROM findings f WHERE f.prompt_id=p.id AND f.severity='error' AND f.dismissed=0)", (*modes, lo, hi)).fetchone()[0]
        errs = c.execute(f"SELECT COUNT(*) FROM findings f JOIN prompts p ON p.id=f.prompt_id WHERE f.mode IN ({marks}) AND f.severity='error' "
                         "AND f.dismissed=0 AND p.words BETWEEN ? AND ?", (*modes, lo, hi)).fetchone()[0]
        length_buckets.append({"label": label, "messages": n, "clean_share": clean_n / n, "per_100_words": 100 * errs / w if w else None})
    return {
        "mode": mode, "since": time.strftime("%Y-%m-%d", time.localtime(first)) if first else None,
        "messages": messages, "words": words, "errors": count("severity='error'"), "slips": count("severity='slip'"),
        "styles": count("severity='style'"), "clean_weekly": clean, "clean_now": next((v for v in reversed(clean) if v is not None), None),
        "categories": categories[:8], "transfer": {"native": NATIVE, "weekly": rates("severity='error' AND transfer=1"),
                                                   "trend": trend("severity='error' AND transfer=1"), "total": transfer_total,
                                                   "share": transfer_total / errors_total if errors_total else None},
        "weeks_with_data": sum(1 for v in clean if v is not None), "focus": focus, "focus_stale": focus_stale,
        "summary_state": ("not_enough" if errors_total < 10 else "none" if focus_row is None else "stale" if focus_stale else "ready"),
        "errors_needed": max(0, 10 - errors_total),
        "errors_per_100_words": 100 * errors_total / words if words else None, "rate_weekly": rates("severity='error'"),
        "rate_trend": trend("severity='error'"), "sentences": sentences,
        "error_free_sentences": (1 - error_sentences / sentences) if sentences else None, "length_buckets": length_buckets,
        "watchlist": watch, "slip_examples": listing("slip"), "style_examples": listing("style"), "messages_by_mode": per_mode, "audit": audit_data(c, marks, modes),
    }


def overview_cmd(args):
    mode = args[0] if args and args[0] in ("written", "dictated", "both") else "both"
    print(json.dumps(overview_data(mode), ensure_ascii=False))


def report():
    c = db()
    now, W, WEEKS = time.time(), 7 * 86400, 8
    tty = sys.stdout.isatty()
    paint = (lambda code, t: f"\x1b[{code}m{t}\x1b[0m") if tty else (lambda code, t: t)
    bars = "▁▂▃▄▅▆▇█"

    def spark(vals, scale):
        out = []
        for v in vals:
            out.append("·" if v is None else bars[0 if v == 0 else min(7, 1 + int(v / scale * 6.999))])
        return "".join(out)

    def bucket(w):   # w = 0 is the most recent 7 days
        return now - (w + 1) * W, now - w * W + (1 if w == 0 else 0)

    def dictated_section():
        dictated = c.execute("SELECT COUNT(*), COALESCE(SUM(words),0) FROM prompts WHERE mode='dictated'").fetchone()
        if dictated[0]:
            tips = c.execute("SELECT COUNT(*) FROM findings WHERE mode='dictated' AND severity='error' AND dismissed=0").fetchone()[0]
            clean = c.execute("SELECT COUNT(*) FROM prompts p WHERE mode='dictated' AND NOT EXISTS "
                              "(SELECT 1 FROM findings f WHERE f.prompt_id=p.id AND f.severity='error' AND f.dismissed=0)").fetchone()[0]
            top = c.execute("SELECT category, COUNT(*) FROM findings WHERE mode='dictated' AND severity='error' AND dismissed=0 GROUP BY category "
                            "ORDER BY COUNT(*) DESC LIMIT 3").fetchall()
            print(paint("2", "\nDictated messages (kept apart from the written statistics above)"))
            print(f"  {dictated[0]} messages, {tips} tips ({100 * tips / max(1, dictated[1]):.1f} per 100 words), "
                  f"{clean / dictated[0]:.0%} without a slip" + ("; most often " + ", ".join(f"{k} ({n})" for k, n in top) if top else ""))

    total_msgs, first = c.execute("SELECT COUNT(*), MIN(ts) FROM prompts WHERE mode='written'").fetchone()
    if not total_msgs:
        print("No written messages yet. Tips and stats appear after a few messages.")
        dictated_section()
        return
    total_tips = c.execute("SELECT COUNT(*) FROM findings WHERE mode='written' AND severity='error' AND dismissed=0").fetchone()[0]
    since = time.strftime("%Y-%m-%d", time.localtime(first))
    print(paint("1", "English coach"), paint("2", f"· {total_msgs} messages checked since {since}, {total_tips} tips"))

    # headline: share of messages that needed no tip at all
    clean = []
    for w in range(WEEKS - 1, -1, -1):
        lo, hi = bucket(w)
        n = c.execute("SELECT COUNT(*) FROM prompts WHERE ts>=? AND ts<? AND mode='written'", (lo, hi)).fetchone()[0]
        k = c.execute("SELECT COUNT(*) FROM prompts p WHERE ts>=? AND ts<? AND mode='written' AND NOT EXISTS "
                      "(SELECT 1 FROM findings f WHERE f.prompt_id=p.id AND f.severity='error' AND f.dismissed=0)", (lo, hi)).fetchone()[0]
        clean.append(k / n if n else None)
    last = next((v for v in reversed(clean) if v is not None), None)
    print(f"\nMessages without a slip  {spark(clean, 1.0)}  " +
          ("" if last is None else f"{last:.0%} this week"))

    # per category: weekly slips per 100 words on one shared scale
    cats = [r[0] for r in c.execute(
        "SELECT category FROM findings WHERE ts>? AND mode='written' AND severity='error' AND dismissed=0 GROUP BY category ORDER BY COUNT(*) DESC",
        (now - WEEKS * W,)).fetchall()]
    series = {cat: [rate(c, *bucket(w), cat)[0] for w in range(WEEKS - 1, -1, -1)] for cat in cats}
    scale = max([v for vs in series.values() for v in vs if v] or [1.0])
    print(paint("2", "\nSlips per 100 words, 8 weeks (shared scale)"))

    def trend(old, w_old, new, w_new):
        if w_old < 200 or w_new < 200 or new is None:
            return paint("2", "·")
        if new < 0.7 * (old or 0):
            return paint("32", "↓")
        if new > 1.3 * (old or 0):
            return paint("31", "↑")
        return "→"

    for cat in cats:
        old, w_old = rate(c, now - 8 * W, now - 4 * W, cat)
        new, w_new = rate(c, now - 4 * W, now + 1, cat)
        print(f"  {cat:20} {spark(series[cat], scale)}  {(new or 0.0):4.1f} {trend(old, w_old, new, w_new)}")

    if NATIVE:   # only when a native language is configured
        tseries = [transfer_rate(c, *bucket(w))[0] for w in range(WEEKS - 1, -1, -1)]
        old, w_old = transfer_rate(c, now - 8 * W, now - 4 * W)
        new, w_new = transfer_rate(c, now - 4 * W, now + 1)
        print(paint("2", f"\nOf these, patterns copied from {NATIVE}"))
        if any(tseries):
            print(f"  {'transfer':20} {spark(tseries, scale)}  {(new or 0.0):4.1f} {trend(old, w_old, new, w_new)}")
        else:
            print("  none in the last 8 weeks")

    rows = c.execute("SELECT original, fix, category, COUNT(*) n FROM findings WHERE severity='error' AND dismissed=0 "
                     "GROUP BY lower(original), lower(fix) HAVING n>1 ORDER BY n DESC LIMIT 6").fetchall()
    if rows:
        print(paint("2", "\nHabit phrases"))
        for o, f, cat, n in rows:
            print(f"  {n}×  {o} → {f}  " + paint("2", cat))

    watch = c.execute("SELECT wrong, right, hits, streak, status FROM habits WHERE hits>=? OR source='manual' "
                      "ORDER BY (status='active') DESC, hits DESC LIMIT 6", (REMIND_AFTER,)).fetchall()
    if watch:
        print(paint("2", "\nWatchlist (mistakes that keep coming back)"))
        for wrong, right, hits, streak, status in watch:
            state = "resolved" if status == "resolved" else (f"improving: {streak} correct in a row" if streak else "active")
            print(f"  {wrong} → {right}   {hits}×  " + paint("2", state))

    n_slip = c.execute("SELECT COUNT(*) FROM findings WHERE severity='slip' AND dismissed=0").fetchone()[0]
    if n_slip:
        print(paint("2", f"\nSlips: {n_slip} forgotten or misrecognized small words (haste, not gaps; not counted as errors)"))

    n_style = c.execute("SELECT COUNT(*) FROM findings WHERE severity='style' AND dismissed=0").fetchone()[0]
    if n_style:
        print(paint("2", f"\nNative polish: {n_style} style suggestions (correct English a native would word differently; not errors)"))
        for o, f in c.execute("SELECT original, fix FROM findings WHERE severity='style' AND dismissed=0 ORDER BY id DESC LIMIT 3"):
            print(f"  {o} → {f}")

    dictated_section()


def recent_cmd(args):
    """List the latest findings with their ids, so one can be dismissed."""
    n = int(args[0]) if args and args[0].isdigit() else 10
    c = db()
    rows = c.execute("SELECT id, mode, severity, dismissed, dismiss_reason, original, fix FROM findings "
                     "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        print("No findings yet.")
        return
    for fid, mode, sev, dis, reason, orig, fix in rows:
        tags = [t for t in ("dictated" if mode == "dictated" else "", sev if sev in ("style", "slip") else "",
                            f"dismissed: {reason or 'no reason'}" if dis else "") if t]
        print(f"{fid:>4}  {orig} → {fix}" + (f"   [{', '.join(tags)}]" if tags else ""))
    print("\nNot your mistake? dismiss <id> [asr|false|quote|taste]   all of the last message: dismiss last [reason]"
          "   undo: undismiss <id>")


def dismiss_cmd(args, undo=False):
    """Mark findings as not the user's mistake (excluded from all statistics, never flagged again), or restore them."""
    if not args:
        print("Usage: dismiss <id|last> [reason]   (reasons: asr = transcription error, false, quote, taste)")
        return
    c = db()
    if args[0] == "last":
        row = c.execute("SELECT prompt_id FROM findings WHERE prompt_id != 0 ORDER BY id DESC LIMIT 1").fetchone()
        ids = [r[0] for r in c.execute("SELECT id FROM findings WHERE prompt_id=? AND dismissed=?", (row[0], 1 if undo else 0))] if row else []
    elif args[0].isdigit():
        ids = [int(args[0])]
    else:
        print("Give a finding id (see `recent`) or `last`.")
        return
    reason = None if undo else (" ".join(args[1:]).strip()[:40] or None)
    changed = 0
    for fid in ids:
        row = c.execute("SELECT original, fix FROM findings WHERE id=?", (fid,)).fetchone()
        if row is None:
            print(f"No finding {fid}.")
            continue
        c.execute("UPDATE findings SET dismissed=?, dismiss_reason=? WHERE id=?", (0 if undo else 1, reason, fid))
        changed += 1
        print(("Restored" if undo else "Dismissed") + f" {fid}: {row[0]} → {row[1]}" + (f"  [{reason}]" if reason else ""))
    recount_habits(c)
    c.commit()
    if not changed:
        print("Nothing to " + ("restore." if undo else "dismiss."))


def forget_cmd(args):
    """Delete the latest correction (or a given id, or every correction of the latest message) without a trace: unlike
    `dismiss` the row is gone and the phrase can be flagged again. For editing slips and the like."""
    c = db()
    target = args[0] if args else "last"
    if target.isdigit():
        ids = [int(target)]
    elif target in ("last", "message"):
        row = c.execute("SELECT id, prompt_id FROM findings ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            ids = []
        elif target == "last":
            ids = [row[0]]
        else:
            ids = [r[0] for r in c.execute("SELECT id FROM findings WHERE prompt_id=?", (row[1],))]
    else:
        print("Usage: forget [last|message|<id>]")
        return
    removed = 0
    for fid in ids:
        row = c.execute("SELECT original, fix FROM findings WHERE id=?", (fid,)).fetchone()
        if row is None:
            print(f"No finding {fid}.")
            continue
        c.execute("DELETE FROM findings WHERE id=?", (fid,))
        removed += 1
        print(f"Removed {fid}: {row[0]} → {row[1]}")
    recount_habits(c)
    c.execute("DELETE FROM habits WHERE source='auto' AND hits=0 AND correct_uses=0")
    c.commit()
    if not removed:
        print("Nothing to remove.")


def watch_cmd(args):
    """List the watchlist, or `add <wrong> <right> [note]` / `remove <wrong>` a manual entry."""
    c = db()
    if args[:1] == ["add"] and len(args) >= 3:
        wrong, right, note = args[1].lower(), args[2].lower(), " ".join(args[3:])
        c.execute("INSERT INTO habits(wrong, right, hits, source, note) VALUES(?,?,0,'manual',?) "
                  "ON CONFLICT(wrong, right) DO UPDATE SET source='manual', status='active', note=excluded.note", (wrong, right, note))
        c.commit()
        print(f'Watching: "{wrong}" instead of "{right}"')
        return
    if args[:1] == ["remove"] and len(args) >= 2:
        n = c.execute("DELETE FROM habits WHERE wrong=?", (args[1].lower(),)).rowcount
        c.commit()
        print(f'Removed {n} entr{"y" if n == 1 else "ies"} for "{args[1].lower()}".')
        return
    rows = c.execute("SELECT wrong, right, hits, streak, status, source, last_hit FROM habits "
                     "WHERE hits>0 OR source='manual' ORDER BY (status='active') DESC, hits DESC").fetchall()
    if not rows:
        print("The watchlist is empty. Mistakes that come back are added automatically; add one yourself with: "
              "watch add <wrong> <right> [note]")
        return
    for wrong, right, hits, streak, status, source, last in rows:
        seen = time.strftime("%Y-%m-%d", time.localtime(last)) if last else "never"
        state = status if status == "resolved" else (f"improving, {streak} correct in a row" if streak else "active")
        print(f"  {wrong} → {right}   {hits}× flagged, last {seen}, {state}" + ("   (added by you)" if source == "manual" else ""))
    print("\nChange with: watch add <wrong> <right> [note]   watch remove <wrong>")


def phrases_cmd(args):
    """phrases: what was suggested so far. phrases ignore|unignore <id|last>: never suggest this one again (or take it back)."""
    c = db()
    entries = {e["id"]: e for e in phrase_list()}
    if args and args[0] in ("ignore", "unignore"):
        want = (" ".join(args[1:]) or "last").strip().lower()
        if want == "last":
            want = c.execute("SELECT value FROM state WHERE key='last_phrase_id'").fetchone()
            want = want[0] if want else ""
        entry = entries.get(want) or next((e for e in entries.values() if want and want in e["phrase"].lower()), None)
        if entry is None:
            print("No such phrase. Run `phrases` to see the ids.")
            return
        row = phrase_status(c).get(entry["id"], (entry["id"], 0, 0, 0, "active", None))
        status = "ignored" if args[0] == "ignore" else ("retired" if row[1] >= PHRASE_SHOWINGS else "active")
        c.execute("INSERT INTO phrases(id, shown, skipped, uses, status, last_shown) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status",
                  (entry["id"], row[1], row[2], row[3], status, row[5]))
        c.commit()
        print(f'"{entry["phrase"]}" will {"not be suggested again" if status == "ignored" else "be suggested again"}.')
        return
    state = phrase_status(c)
    rows = sorted((r for r in state.values() if r[1] or r[3] or r[4] != "active"), key=lambda r: -(r[5] or 0))
    if not rows:
        print("No phrase has been suggested yet. They appear now and then, when one fits what you wrote.")
        return
    for pid, shown, skipped, uses, status, last in rows:
        e = entries.get(pid)
        if e:
            print(f'{pid:22} "{e["phrase"]}"  shown {shown}x, used by you {uses}x, {status}')


def _as_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_as_text(b.get("text") if isinstance(b, dict) else b) for b in content
                        if isinstance(b, str) or (isinstance(b, dict) and b.get("type", "text") == "text"))
    return ""


def transcript_messages(root, since):
    """[(epoch, text)] of the messages the user sent, from Claude Code's own session logs (all projects) touched since `since`."""
    import datetime
    out = []
    for path in Path(root).glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            kind, stamp, text = o.get("type"), o.get("timestamp"), ""
            if kind == "user" and not o.get("isMeta"):
                text = _as_text(o.get("message", {}).get("content"))
                if text.lstrip().startswith("<"):
                    text = ""
            elif kind == "attachment" and o.get("attachment", {}).get("type") == "queued_command":
                text, stamp = _as_text(o["attachment"].get("prompt")), o["attachment"].get("timestamp") or stamp
            elif kind == "queue-operation" and o.get("operation") == "enqueue":
                text = _as_text(o.get("content"))
            if text.strip() and stamp:
                try:
                    out.append((datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp(), text))
                except ValueError:
                    pass
    return out


def backfill_cmd(args):
    """Fill in sentence counts and sentence positions for rows stored before they existed. Local and one-time: the coach never
    kept message text, so this reads your own Claude Code session logs, matches each row to its message by time and word count,
    and writes back numbers only. Nothing is printed or stored from the messages. `--dry-run` changes nothing."""
    dry = "--dry-run" in args
    c = db()
    rows = c.execute("SELECT id, ts, words, mode FROM prompts WHERE sentences IS NULL ORDER BY id").fetchall()
    if not rows:
        print("Nothing to backfill: every message already has its sentence count.")
        return
    root = os.environ.get("ENGLISH_COACH_TRANSCRIPTS", str(Path.home() / ".claude" / "projects"))
    messages = transcript_messages(root, rows[0][1] - 86400)
    cleaned = [split_marker(text)[0] for _, text in messages]
    counts = [len(re.findall(r"\w+", text)) for text in cleaned]
    used, matched, exact = set(), {}, 0
    for tolerance in (0, 2, 4):
        for pid, ts, words, mode in rows:
            if pid in matched:
                continue
            best = None
            for i, (stamp, _) in enumerate(messages):
                if i in used or not -2 <= ts - stamp <= 300 or abs(counts[i] - words) > tolerance:
                    continue
                score = abs(ts - stamp - 8) + 30 * abs(counts[i] - words)
                if best is None or score < best[1]:
                    best = (i, score)
            if best:
                used.add(best[0])
                matched[pid] = best[0]
                exact += tolerance == 0
    if not dry:
        for pid, i in matched.items():
            text = cleaned[i]
            spans = sentence_spans(text)
            c.execute("UPDATE prompts SET sentences=? WHERE id=?", (len(spans) or 1, pid))
            for fid, original in c.execute("SELECT id, original FROM findings WHERE prompt_id=?", (pid,)).fetchall():
                c.execute("UPDATE findings SET sent_idx=? WHERE id=?", (sentence_index(spans, text, original), fid))
        c.commit()
    print(("Would update" if dry else "Updated") + f" {len(matched)} of {len(rows)} messages ({exact} matched exactly, "
          f"{len(matched) - exact} by close word count); {len(rows) - len(matched)} not found in the session logs.")


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2) + "\n")


def config_cmd(args):
    cfg = load_config()
    if args[:1] == ["set"] and len(args) >= 3:
        key, value = args[1], " ".join(args[2:]).strip()
        if key not in DEFAULTS:
            sys.exit(f"Unknown setting '{key}'. Known: {', '.join(DEFAULTS)}")
        try:
            if key == "audit":
                if value.lower() not in ("off", "flagged", "all"):
                    sys.exit("audit must be off, flagged or all")
                cfg[key] = value.lower()
            elif key in STRING_KEYS:
                cfg[key] = None if value.lower() in ("", "none", "unset") else value
            elif key in ("strip_marker", "show_style", "auto_summary"):
                cfg[key] = value.lower() in ("1", "on", "true", "yes")
            else:
                cfg[key] = max(1, int(value))
        except ValueError:
            sys.exit(f"Invalid value for {key}: {value!r}")
        save_config(cfg)
        print(f"Saved {key} = {cfg[key]!r} in {CONFIG_FILE}")
        return
    for k in DEFAULTS:
        shown = cfg[k] if cfg[k] is not None else ("(not set: no language-specific feedback)" if k == "native_language" else "(not set)" if k == "dictation_marker" else "(default)")
        print(f"{k}: {shown}")
    print(f"\nChange with: english-coach.py config set <key> <value>   ({CONFIG_FILE})")


def setup():
    if not sys.stdin.isatty():
        print("Run this in a terminal, or use /english-coach:setup inside Claude Code.")
        return
    cfg = load_config()
    print("English coach setup (press Enter to keep the value in brackets)\n")
    cur = cfg["native_language"] or ""
    v = input(f"Your native language, e.g. German or Portuguese [{cur or 'not set'}]: ").strip()
    if v:
        cfg["native_language"] = v
    cur = cfg["dictation_marker"] or ""
    v = input(f"Marker your dictation tool adds to dictated text, e.g. [dictated] (Enter to skip) [{cur or 'none'}]: ").strip()
    if v:
        cfg["dictation_marker"] = v
    for key, label in (("min_words", "Minimum words per message to coach"), ("max_tips", "Maximum tips shown at once")):
        v = input(f"{label} [{cfg[key]}]: ").strip()
        if v.isdigit() and int(v) > 0:
            cfg[key] = int(v)
    save_config(cfg)
    print(f"\nSaved to {CONFIG_FILE}. Takes effect from the next message.")


if __name__ == "__main__":
    try:
        cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
        if cmd == "config":
            config_cmd(rest)
        elif cmd == "setup":
            setup()
        elif cmd == "prepare":
            prepare_cmd()
        elif cmd == "check":
            check_cmd()
        elif cmd == "ingest":
            ingest_cmd()
        elif cmd == "report":
            report()
        elif cmd == "forget":
            forget_cmd(rest)
        elif cmd == "summary-due":
            summary_due_cmd(rest)
        elif cmd == "summary-input":
            summary_input_cmd(rest)
        elif cmd == "summary-store":
            summary_store_cmd(rest)
        elif cmd == "backfill":
            backfill_cmd(rest)
        elif cmd == "watch":
            watch_cmd(rest)
        elif cmd == "phrases":
            phrases_cmd(rest)
        elif cmd == "audit":
            audit_cmd(rest)
        elif cmd == "audit-store":
            audit_store_cmd()
        elif cmd == "overview":
            overview_cmd(rest)
        elif cmd == "recent":
            recent_cmd(rest)
        elif cmd == "dismiss":
            dismiss_cmd(rest)
        elif cmd == "undismiss":
            dismiss_cmd(rest, undo=True)
        elif cmd == "off":
            DATA.mkdir(parents=True, exist_ok=True)
            PAUSED.touch()
        elif cmd == "on":
            PAUSED.unlink(missing_ok=True)
        else:
            print(__doc__)
    except Exception:
        pass
