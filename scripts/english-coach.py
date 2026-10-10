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
  english-coach.py recent [n]         latest findings with ids
  english-coach.py dismiss <id|last> [reason]   not my mistake: excluded from statistics, never flagged again
  english-coach.py undismiss <id|last>          restore a dismissed finding
  english-coach.py off | on           pause / resume
  english-coach.py setup              interactive setup (native language, thresholds)
  english-coach.py config [set KEY VALUE]   show or change settings
"""
import difflib, json, os, re, sqlite3, sys, time
from pathlib import Path

DATA = Path(os.environ.get("ENGLISH_COACH_DIR", Path.home() / ".local/share/english-coach"))
DB = DATA / "log.db"
PAUSED = DATA / "paused"


CONFIG_FILE = Path(os.environ.get("ENGLISH_COACH_CONFIG_DIR",
                                   Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "english-coach")) / "config.json"
DEFAULTS = {"native_language": None, "dictation_marker": None, "strip_marker": True, "min_words": 6, "max_tips": 2,
            "icon_written": "\u270e", "icon_dictated": "\U0001F399\uFE0E", "show_style": False, "escalate_model": "sonnet",
            "remind_after": 2, "resolve_after": 5}   # pen; studio microphone, text style
STRING_KEYS = ("native_language", "dictation_marker", "icon_written", "icon_dictated", "escalate_model")   # text settings; None = not set


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
CREATE TABLE IF NOT EXISTS prompts(id INTEGER PRIMARY KEY, ts REAL, words INTEGER, did_well TEXT, mode TEXT DEFAULT 'written');
CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY, prompt_id INTEGER, ts REAL, category TEXT,
  transfer INTEGER, original TEXT, fix TEXT, explanation TEXT, shown INTEGER DEFAULT 0, mode TEXT DEFAULT 'written',
  severity TEXT DEFAULT 'error', dismissed INTEGER DEFAULT 0, dismiss_reason TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS habits(id INTEGER PRIMARY KEY, wrong TEXT, right TEXT, hits INTEGER DEFAULT 0, last_hit REAL,
  streak INTEGER DEFAULT 0, correct_uses INTEGER DEFAULT 0, status TEXT DEFAULT 'active', source TEXT DEFAULT 'auto', note TEXT,
  UNIQUE(wrong, right));
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


def system_prompt(speech=False, text=""):
    n = NATIVE
    who = f"a non-native English speaker (native language: {n})" if n else "a non-native English speaker"
    transfer = (f"{n} transfer errors (false friends, literal calques, prepositions or verb patterns copied from {n}); " if n else "")
    transfer_field = f"true if the error copies a {n} pattern" if n else "always false"
    unclear_field = ',"unclear":false' if speech else ""
    unclear_spec = ('- unclear: true only if the passage in "original" makes little sense as a sentence (garbled, for example a speech-recognition '
                    'mishearing), whatever its grammar; otherwise false\n') if speech else ""
    speech_rules = ("SPEECH MODE: this text was produced by speech recognition, the user dictated it. Ignore likely mis-transcriptions "
                    "(homophones and sound-alikes such as their/there/they're or weather/whether, misheard or split words), missing or odd "
                    "punctuation and capitalization, filler words, false starts and run-on sentences. Judge only the grammar and word choice "
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
{{"careless":false,"fixes":[{{"original":"","fix":"","category":"","transfer":false,"severity":"error"{unclear_field},"explanation":""}}],"did_well":[]}}
- severity: "error" when a native editor would mark the original as wrong; "style" when it is correct but a native would clearly say it another way (a collocation or construction that is valid yet unnatural, or a redundancy). At most ONE style item per message, and only when the better wording is clearly more natural. Register, formality, politeness and word rarity are never style items.
{unclear_spec}- original: exact substring copied verbatim from the message, only the few words with the error
- fix: the single corrected replacement: the smallest change that reads naturally. Keep the user's concise chat style: never add back dropped subjects or other elements, and prefer the short wording a native would use in chat over a formal, wordy construction
- category: exactly one of {', '.join(CATEGORIES)} (see definitions below)
- transfer: {transfer_field}
- explanation: max 25 words, neutral and encouraging; the reusable pattern with a tiny correct example; never restate the error; plain words, no grammar jargon
- at most 3 fixes, most valuable first; clean message: {{"fixes":[],"did_well":[...]}}
- did_well: categories (same list) in which the message used a non-trivial construction correctly; empty if unsure

{CATEGORY_GUIDE}{hint}"""


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=5)
    c.executescript(SCHEMA)
    for table, column, declaration in (("prompts", "mode", "TEXT DEFAULT 'written'"), ("findings", "mode", "TEXT DEFAULT 'written'"),
                                       ("findings", "severity", "TEXT DEFAULT 'error'"), ("findings", "dismissed", "INTEGER DEFAULT 0"),
                                       ("findings", "dismiss_reason", "TEXT")):
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


def coachable(prompt):
    words = len(re.findall(r"\w+", prompt))
    return (words >= MIN_WORDS and len(prompt) <= MAX_CHARS and not prompt.startswith(("/", "!", "~"))
            and not ENGINE_FRAMED.match(prompt))


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


QUOTED = re.compile(r'```.*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|^[ \t]*>.*$', re.S | re.M)
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
        severity = "style" if f.get("severity") == "style" else "error"
        fixes.append((cat, int(bool(f.get("transfer"))), orig, fix, clean(f.get("explanation"), 300), severity))
    errors = [x for x in fixes if x[5] == "error"]
    styles = [x for x in fixes if x[5] == "style"][:1]            # at most one style item per message
    return (errors + styles)[:3]




def build_system(c, speech=False, text=""):
    top = [k for k, _ in sorted(recurrence(c).items(), key=lambda kv: -kv[1])[:3]]
    return (system_prompt(speech, text) + (f"\nPay slightly more attention to this learner's recurring areas: {', '.join(top)}." if top else "")
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
    """One small word added or dropped (typed: only subject pronouns and auxiliaries, whose omission is haste; dictated: any
    small word, also swapped for another, since those are the typical recognition errors). A performance slip, not a gap."""
    a, b = _words(original), _words(fix)
    ops = [o for o in difflib.SequenceMatcher(None, a, b).get_opcodes() if o[0] != "equal"]
    if len(ops) != 1:
        return False
    tag, i1, i2, j1, j2 = ops[0]
    removed, added = a[i1:i2], b[j1:j2]
    words = FUNCTION_WORDS if speech else SUBJECT_AUX
    if tag == "insert":
        return len(added) == 1 and added[0] in words
    if tag == "delete":
        return len(removed) == 1 and removed[0] in words
    return tag == "replace" and speech and len(removed) == 1 and len(added) == 1 and removed[0] in words and added[0] in words


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


def sloppy_form(prompt):
    """Necessary condition for careless mode: the message *looks* dashed off (starts lowercase or has no closing
    punctuation). Keeps a single model judgement from silencing feedback on a carefully written message."""
    text = prompt.strip()
    first = next((ch for ch in text if ch.isalpha()), "")
    return first.islower() or not text.endswith((".", "!", "?", "…", ")", '"'))


def store_answer(c, prompt, answer, mode="written", context=""):
    """Validate and store a review. Returns (careless, [(category, transfer, original, fix, explanation, severity)],
    {"reminders": {lower original: text}, "milestone": text or None})."""
    now = time.time()
    if mode == "written" and answer.get("careless") is True and sloppy_form(prompt):   # sloppy mode: no tips, excluded from all statistics
        set_state(c, "last_careless", now)
        c.commit()
        return True, [], {"reminders": {}, "milestone": None}
    well = [x for x in answer.get("did_well", []) if x in CATEGORIES]
    pid = c.execute("INSERT INTO prompts(ts, words, did_well, mode) VALUES(?,?,?,?)",
                    (now, len(re.findall(r"\w+", prompt)), json.dumps(well), mode)).lastrowid
    fixes = validate(answer, prompt, recent_phrases(c), speech=(mode == "dictated"), context=context,
                     suppressed=suppressed_phrases(c))
    fixes = classify_slips(c, fixes, speech=(mode == "dictated"))
    for cat, tr, orig, fix, expl, severity in fixes:
        c.execute("INSERT INTO findings(prompt_id, ts, category, transfer, original, fix, explanation, shown, mode, severity) "
                  "VALUES(?,?,?,?,?,?,?,1,?,?)", (pid, now, cat, tr, orig, fix, expl, mode, severity))
    reminders, milestone = update_habits(c, prompt, [(o, f, e) for _, _, o, f, e, sev in fixes if sev in ("error", "slip")])
    c.commit()
    return False, fixes, {"reminders": reminders, "milestone": milestone}


def prepare_cmd():
    """Mod path, step 1. stdin: {"prompt"}.
    stdout: {"coach": bool, "text": the prompt without the dictation marker, "mode": "written"|"dictated",
             "strip": whether the mod should hand the model the text without the marker, "system", "user"}."""
    try:
        prompt = (json.load(sys.stdin).get("prompt") or "").strip()
    except Exception:
        prompt = ""
    text, speech = split_marker(prompt)
    out = {"coach": False, "text": text, "mode": "dictated" if speech else "written",
           "strip": bool(speech and CFG["strip_marker"])}
    if not PAUSED.exists() and coachable(text):
        out.update(coach=True, system=build_system(db(), speech, text), user=user_block(text))
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
    careless, fixes, extras = store_answer(c, prompt, answer, mode, context)
    rec = recurrence(c)
    errors = sorted((f for f in fixes if f[5] == "error"), key=lambda f: -rec.get(f[0], 0))[:MAX_TIPS]   # recurring areas first
    styles = [f for f in fixes if f[5] == "style"] if CFG["show_style"] else []                          # stored either way
    fixes = errors + styles
    btw = extras["milestone"] or (None if careless or mode != "written" else maybe_btw(c))
    hint = setup_hint(c)
    c.commit()
    icon = CFG["icon_dictated" if mode == "dictated" else "icon_written"] or DEFAULTS["icon_dictated" if mode == "dictated" else "icon_written"]
    print(json.dumps({"ok": True, "careless": careless, "btw": btw, "hint": hint, "icon": icon,
                      "fixes": [{"category": cat, "transfer": bool(tr), "original": o, "fix": f, "explanation": e, "severity": sev,
                                "reminder": extras["reminders"].get(o.lower())}
                                for cat, tr, o, f, e, sev in fixes]}))


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
            if key in STRING_KEYS:
                cfg[key] = None if value.lower() in ("", "none", "unset") else value
            elif key in ("strip_marker", "show_style"):
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
        elif cmd == "watch":
            watch_cmd(rest)
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
