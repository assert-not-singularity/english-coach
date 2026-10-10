#!/usr/bin/env python3
"""English coach backend. The inline mod (hooks/register.tsx) calls it; it holds the prompt, validation,
storage, statistics and settings so that logic lives in one place.

Usage:
  english-coach.py prepare            stdin {"prompt"} -> {"coach", "system", "user"}: coach this message?
  english-coach.py ingest             stdin {"prompt", "reply"}: validate and store a review, print the fixes
  english-coach.py report             per-category weekly trend, clean-message share, habit phrases
  english-coach.py off | on           pause / resume
  english-coach.py setup              interactive setup (native language, thresholds)
  english-coach.py config [set KEY VALUE]   show or change settings
"""
import json, os, re, sqlite3, sys, time
from pathlib import Path

DATA = Path(os.environ.get("ENGLISH_COACH_DIR", Path.home() / ".local/share/english-coach"))
DB = DATA / "log.db"
PAUSED = DATA / "paused"


CONFIG_FILE = Path(os.environ.get("ENGLISH_COACH_CONFIG_DIR",
                                   Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "english-coach")) / "config.json"
DEFAULTS = {"native_language": None, "dictation_marker": None, "strip_marker": True, "min_words": 6, "max_tips": 2}
STRING_KEYS = ("native_language", "dictation_marker")   # text settings; None = not set


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
  transfer INTEGER, original TEXT, fix TEXT, explanation TEXT, shown INTEGER DEFAULT 0, mode TEXT DEFAULT 'written');
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
"""

def split_marker(text):
    """(text without the dictation marker, whether it was there). Dictation tools add the marker after every chunk,
    so it can sit anywhere: all occurrences go, and one occurrence makes the whole message dictated."""
    t, marker = text.strip(), CFG["dictation_marker"]
    if not marker:
        return t, False
    cleaned = re.sub(r"\s+", " ", re.sub(re.escape(marker), " ", t, flags=re.I)).strip()
    return cleaned, cleaned != re.sub(r"\s+", " ", t)


def system_prompt(speech=False):
    n = NATIVE
    who = f"a non-native English speaker (native language: {n})" if n else "a non-native English speaker"
    transfer = (f"{n} transfer errors (false friends, literal calques, prepositions or verb patterns copied from {n}); " if n else "")
    transfer_field = f"true if the error copies a {n} pattern" if n else "always false"
    speech_rules = ("SPEECH MODE: this text was produced by speech recognition, the user dictated it. Ignore likely mis-transcriptions "
                    "(homophones and sound-alikes such as their/there/they're or weather/whether, misheard or split words), missing or odd "
                    "punctuation and capitalization, filler words, false starts and run-on sentences. Judge only the grammar and word choice "
                    "the speaker produced. Never set \"careless\".\n\n" if speech else "")
    return f"""You are an English writing coach inside a coding tool. The user is {who} who writes prompts to a coding agent in English and wants to improve the language. Feedback is shown as a small dim note, so a pedantic or wrong correction costs more than a missed one.
Calibrate like a careful native-speaking senior engineer reading a colleague's chat message: flag what they would actually correct, nothing else.

FLAG: wrong word or word form; grammar (agreement, verb forms and tenses, missing or wrong articles/determiners, plurals and countability, word order, wrong prepositions, an adverb where an adjective belongs such as "totally nonsense"); {transfer}phrasing a native would clearly rewrite because it sounds foreign.
DO NOT FLAG: typos and misspellings (including a slip that happens to form another real word, such as "now" for "not" or "form" for "from"), letter case, punctuation, informal tone, questions written in statement word order or with trailing ellipses (normal in chat), elliptical suggestions or questions built on a gerund or noun phrase ("Maybe running it nightly?"), slang, fragments, dropped subjects, terse imperatives, technical jargon, product/tool names, code, paths, URLs, anything in backticks or quotation marks, words from the user's own language used on purpose, valid stylistic choices, or the content/approach itself.
Chat conventions are NOT errors: a statement with a question mark ("The repo also had stats...?"), trailing ellipses, and sentence fragments are normal informal English; never "fix" them into formal questions.
When in doubt, do not flag. A missed tip is better than a pedantic one.
Before emitting a fix, drop it unless: a native editor would mark the original as an error (not taste), the fix keeps meaning and tone, and the original is ordinary prose.

CARELESS FLAG: set "careless" true only if the message was clearly dashed off without any care for language (many typos, missing capitalization and punctuation, broken fragments, swearing, frustration). A message that is capitalized and punctuated, or that contains deliberate or playful mistakes, is NOT careless. Always list the fixes you find regardless of this flag: the caller decides whether to show them.

{speech_rules}Output ONLY one raw JSON object, no fences:
{{"careless":false,"fixes":[{{"original":"","fix":"","category":"","transfer":false,"explanation":""}}],"did_well":[]}}
- original: exact substring copied verbatim from the message, only the few words with the error
- fix: the single corrected replacement: the smallest change that reads naturally. Keep the user's concise chat style: never add back dropped subjects or other elements, and prefer the short wording a native would use in chat over a formal, wordy construction
- category: exactly one of {', '.join(CATEGORIES)} (see definitions below)
- transfer: {transfer_field}
- explanation: max 25 words, neutral and encouraging; the reusable pattern with a tiny correct example; never restate the error; plain words, no grammar jargon
- at most 3 fixes, most valuable first; clean message: {{"fixes":[],"did_well":[...]}}
- did_well: categories (same list) in which the message used a non-trivial construction correctly; empty if unsure

{CATEGORY_GUIDE}"""


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=5)
    c.executescript(SCHEMA)
    for table in ("prompts", "findings"):   # databases from before dictation support
        if "mode" not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
            c.execute(f"ALTER TABLE {table} ADD COLUMN mode TEXT DEFAULT 'written'")
    return c


def get_state(c, key, default=0.0):
    r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return float(r[0]) if r else default


def set_state(c, key, value):
    c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))


def recurrence(c):
    return dict(c.execute("SELECT category, COUNT(*) FROM findings WHERE ts>? AND mode='written' GROUP BY category",
                          (time.time() - WINDOW,)).fetchall())


def rate(c, since, until, cat):
    """(findings per 100 words, words) in [since, until)."""
    words = c.execute("SELECT COALESCE(SUM(words),0) FROM prompts WHERE ts>=? AND ts<? AND mode='written'", (since, until)).fetchone()[0]
    n = c.execute("SELECT COUNT(*) FROM findings WHERE category=? AND ts>=? AND ts<? AND mode='written'", (cat, since, until)).fetchone()[0]
    return (100 * n / words if words else None), words


def transfer_rate(c, since, until):
    """(transfer errors per 100 words, words) in [since, until): slips that copy the user's native language."""
    words = c.execute("SELECT COALESCE(SUM(words),0) FROM prompts WHERE ts>=? AND ts<? AND mode='written'", (since, until)).fetchone()[0]
    n = c.execute("SELECT COUNT(*) FROM findings WHERE transfer=1 AND ts>=? AND ts<? AND mode='written'", (since, until)).fetchone()[0]
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
    """Lower-cased originals and fixes of corrections made in the last RECENT seconds."""
    out = set()
    for orig, fix in c.execute("SELECT original, fix FROM findings WHERE ts > ?", (time.time() - RECENT,)):
        out.update((orig.lower(), fix.lower()))
    return out


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


def validate(answer, prompt, recent=frozenset(), speech=False):
    """Keep only fixes whose original really occurs in the prompt and which change more than case/spacing."""
    low, spans, fixes = prompt.lower(), [], []
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
        if is_slip(orig, fix) or (speech and sounds_alike(orig, fix)):   # a typo, or a speech-recognition mishearing
            continue
        if orig.lower() in recent or fix.lower() in recent:     # the same error was just corrected: a paste, not new writing
            continue
        if any(qs <= a and b <= qe for qs, qe in quoted):      # quoted or code text is somebody else's words
            continue
        if fix.lower().startswith(orig.lower()) or fix.lower().endswith(orig.lower()):
            continue                                            # only adds words around terse phrasing: not an error
        spans.append((a, b))
        cat = f.get("category") if f.get("category") in CATEGORIES else "other"
        fixes.append((cat, int(bool(f.get("transfer"))), orig, fix, clean(f.get("explanation"), 300)))
    return fixes[:3]




def build_system(c, speech=False):
    top = [k for k, _ in sorted(recurrence(c).items(), key=lambda kv: -kv[1])[:3]]
    return system_prompt(speech) + (f"\nPay slightly more attention to this learner's recurring areas: {', '.join(top)}." if top else "")


def user_block(prompt):
    return f"<message>\n{prompt}\n</message>\nReview the message above. Reply with the JSON object only."


def answer_from(text):
    """The last JSON object in a model reply that carries a 'fixes' list, or None."""
    answers = [o for o in json_objects(text) if isinstance(o.get("fixes"), list)]
    return answers[-1] if answers else None


def sloppy_form(prompt):
    """Necessary condition for careless mode: the message *looks* dashed off (starts lowercase or has no closing
    punctuation). Keeps a single model judgement from silencing feedback on a carefully written message."""
    text = prompt.strip()
    first = next((ch for ch in text if ch.isalpha()), "")
    return first.islower() or not text.endswith((".", "!", "?", "…", ")", '"'))


def store_answer(c, prompt, answer, mode="written"):
    """Validate and store a review. Returns (careless, [(category, transfer, original, fix, explanation)])."""
    now = time.time()
    if mode == "written" and answer.get("careless") is True and sloppy_form(prompt):   # sloppy mode: no tips, excluded from all statistics
        set_state(c, "last_careless", now)
        c.commit()
        return True, []
    well = [x for x in answer.get("did_well", []) if x in CATEGORIES]
    pid = c.execute("INSERT INTO prompts(ts, words, did_well, mode) VALUES(?,?,?,?)",
                    (now, len(re.findall(r"\w+", prompt)), json.dumps(well), mode)).lastrowid
    fixes = validate(answer, prompt, recent_phrases(c), speech=(mode == "dictated"))
    for cat, tr, orig, fix, expl in fixes:
        c.execute("INSERT INTO findings(prompt_id, ts, category, transfer, original, fix, explanation, shown, mode) "
                  "VALUES(?,?,?,?,?,?,?,1,?)", (pid, now, cat, tr, orig, fix, expl, mode))
    c.commit()
    return False, fixes




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
        out.update(coach=True, system=build_system(db(), speech), user=user_block(text))
    print(json.dumps(out))


def ingest_cmd():
    """Mod path, step 2. stdin: {"prompt": text without marker, "mode", "reply"}. Stores the review;
    stdout: {"careless", "btw", "hint", "fixes": [...]}. Findings are stored as already shown, because the mod
    draws them inline."""
    try:
        data = json.load(sys.stdin)
        prompt, answer = (data.get("prompt") or "").strip(), answer_from(data.get("reply") or "")
        mode = "dictated" if data.get("mode") == "dictated" else "written"
    except Exception:
        prompt, answer, mode = "", None, "written"
    if not prompt or answer is None:
        print(json.dumps({"ok": False}))
        return
    c = db()
    careless, fixes = store_answer(c, prompt, answer, mode)
    rec = recurrence(c)
    fixes = sorted(fixes, key=lambda f: -rec.get(f[0], 0))[:MAX_TIPS]   # recurring areas first; no counts shown
    btw = None if careless or mode != "written" else maybe_btw(c)
    hint = setup_hint(c)
    c.commit()
    print(json.dumps({"ok": True, "careless": careless, "btw": btw, "hint": hint,
                      "fixes": [{"category": cat, "transfer": bool(tr), "original": o, "fix": f, "explanation": e}
                                for cat, tr, o, f, e in fixes]}))


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
            tips = c.execute("SELECT COUNT(*) FROM findings WHERE mode='dictated'").fetchone()[0]
            clean = c.execute("SELECT COUNT(*) FROM prompts p WHERE mode='dictated' AND NOT EXISTS "
                              "(SELECT 1 FROM findings f WHERE f.prompt_id=p.id)").fetchone()[0]
            top = c.execute("SELECT category, COUNT(*) FROM findings WHERE mode='dictated' GROUP BY category "
                            "ORDER BY COUNT(*) DESC LIMIT 3").fetchall()
            print(paint("2", "\nDictated messages (kept apart from the written statistics above)"))
            print(f"  {dictated[0]} messages, {tips} tips ({100 * tips / max(1, dictated[1]):.1f} per 100 words), "
                  f"{clean / dictated[0]:.0%} without a slip" + ("; most often " + ", ".join(f"{k} ({n})" for k, n in top) if top else ""))

    total_msgs, first = c.execute("SELECT COUNT(*), MIN(ts) FROM prompts WHERE mode='written'").fetchone()
    if not total_msgs:
        print("No written messages yet. Tips and stats appear after a few messages.")
        dictated_section()
        return
    total_tips = c.execute("SELECT COUNT(*) FROM findings WHERE mode='written'").fetchone()[0]
    since = time.strftime("%Y-%m-%d", time.localtime(first))
    print(paint("1", "English coach"), paint("2", f"· {total_msgs} messages checked since {since}, {total_tips} tips"))

    # headline: share of messages that needed no tip at all
    clean = []
    for w in range(WEEKS - 1, -1, -1):
        lo, hi = bucket(w)
        n = c.execute("SELECT COUNT(*) FROM prompts WHERE ts>=? AND ts<? AND mode='written'", (lo, hi)).fetchone()[0]
        k = c.execute("SELECT COUNT(*) FROM prompts p WHERE ts>=? AND ts<? AND mode='written' AND NOT EXISTS "
                      "(SELECT 1 FROM findings f WHERE f.prompt_id=p.id)", (lo, hi)).fetchone()[0]
        clean.append(k / n if n else None)
    last = next((v for v in reversed(clean) if v is not None), None)
    print(f"\nMessages without a slip  {spark(clean, 1.0)}  " +
          ("" if last is None else f"{last:.0%} this week"))

    # per category: weekly slips per 100 words on one shared scale
    cats = [r[0] for r in c.execute(
        "SELECT category FROM findings WHERE ts>? AND mode='written' GROUP BY category ORDER BY COUNT(*) DESC",
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

    rows = c.execute("SELECT original, fix, category, COUNT(*) n FROM findings "
                     "GROUP BY lower(original), lower(fix) HAVING n>1 ORDER BY n DESC LIMIT 6").fetchall()
    if rows:
        print(paint("2", "\nHabit phrases"))
        for o, f, cat, n in rows:
            print(f"  {n}×  {o} → {f}  " + paint("2", cat))

    dictated_section()


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
            elif key == "strip_marker":
                cfg[key] = value.lower() in ("1", "on", "true", "yes")
            else:
                cfg[key] = max(1, int(value))
        except ValueError:
            sys.exit(f"Invalid value for {key}: {value!r}")
        save_config(cfg)
        print(f"Saved {key} = {cfg[key]!r} in {CONFIG_FILE}")
        return
    for k in DEFAULTS:
        shown = cfg[k] if cfg[k] is not None else "(not set: no language-specific feedback)"
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
        elif cmd == "ingest":
            ingest_cmd()
        elif cmd == "report":
            report()
        elif cmd == "off":
            DATA.mkdir(parents=True, exist_ok=True)
            PAUSED.touch()
        elif cmd == "on":
            PAUSED.unlink(missing_ok=True)
        else:
            print(__doc__)
    except Exception:
        pass
