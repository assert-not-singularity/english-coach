#!/usr/bin/env python3
"""English coach: UserPromptSubmit hook.

Hook mode: show pending tips from earlier analysis (dim systemMessage), then
spawn a detached worker for the current prompt. Never blocks the prompt; every
failure is silent.
Worker: ask Haiku for structured findings, validate, store in SQLite.

Usage:
  english-coach.py            hook mode (hook JSON on stdin)
  english-coach.py --worker   internal; prompt text on stdin
  english-coach.py stop       Stop hook: show the tip for this turn
  english-coach.py report     print per-category trend
  english-coach.py off|on     pause / resume
  english-coach.py setup      interactive setup (native language, thresholds)
  english-coach.py config [set KEY VALUE]   show or change settings
"""
import json, os, re, sqlite3, subprocess, sys, time
from pathlib import Path

DATA = Path(os.environ.get("ENGLISH_COACH_DIR", Path.home() / ".local/share/english-coach"))
DB = DATA / "log.db"
PAUSED = DATA / "paused"
GUARD = "ENGLISH_COACH_ACTIVE"


CONFIG_FILE = Path(os.environ.get("ENGLISH_COACH_CONFIG_DIR",
                                   Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "english-coach")) / "config.json"
DEFAULTS = {"native_language": None, "min_words": 6, "max_tips": 2, "color": False}


def load_config():
    """Settings from CONFIG_FILE merged over DEFAULTS; a broken file falls back to defaults."""
    cfg = dict(DEFAULTS)
    try:
        for k, v in json.loads(CONFIG_FILE.read_text()).items():
            if k in DEFAULTS and type(v) in (type(DEFAULTS[k]), type(None)) or (k == "native_language" and isinstance(v, str)):
                cfg[k] = v
    except Exception:
        pass
    if isinstance(cfg["native_language"], str) and not cfg["native_language"].strip():
        cfg["native_language"] = None
    return cfg


CFG = load_config()
NATIVE = CFG["native_language"]          # None = unknown: no assumption is made about the user's language
MIN_WORDS, MAX_CHARS = max(1, int(CFG["min_words"])), 2000
MAX_TIPS = max(1, int(CFG["max_tips"]))
STOP_WAIT = 25   # seconds the Stop hook may wait for the analysis
# ANSI highlighting of tips; off by default because not every UI renders escape codes
_COLOR = os.environ.get("ENGLISH_COACH_COLOR", "1" if CFG["color"] else "0") != "0"
RED, GREEN, DIM, OFF = ("\x1b[31m", "\x1b[32m", "\x1b[2m", "\x1b[0m") if _COLOR else ("", "", "", "")
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
CREATE TABLE IF NOT EXISTS prompts(id INTEGER PRIMARY KEY, ts REAL, words INTEGER, did_well TEXT);
CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY, prompt_id INTEGER, ts REAL, category TEXT,
  transfer INTEGER, original TEXT, fix TEXT, explanation TEXT, shown INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
"""

def system_prompt():
    n = NATIVE
    who = f"a non-native English speaker (native language: {n})" if n else "a non-native English speaker"
    transfer = (f"{n} transfer errors (false friends, literal calques, prepositions or verb patterns copied from {n}); " if n else "")
    transfer_field = f"true if the error copies a {n} pattern" if n else "always false"
    return f"""You are an English writing coach inside a coding tool. The user is {who} who writes prompts to a coding agent in English and wants to improve the language. Feedback is shown as a small dim note, so a pedantic or wrong correction costs more than a missed one.
Calibrate like a careful native-speaking senior engineer reading a colleague's chat message: flag what they would actually correct, nothing else.

FLAG: wrong word or word form; grammar (agreement, verb forms and tenses, missing or wrong articles/determiners, plurals and countability, word order, wrong prepositions); {transfer}phrasing a native would clearly rewrite because it sounds foreign.
DO NOT FLAG: typos and misspellings, letter case, punctuation, informal tone, questions written in statement word order or with trailing ellipses (normal in chat), slang, fragments, dropped subjects, terse imperatives, technical jargon, product/tool names, code, paths, URLs, anything in backticks or quotation marks, words from the user's own language used on purpose, valid stylistic choices, or the content/approach itself.
Chat conventions are NOT errors: a statement with a question mark ("The repo also had stats...?"), trailing ellipses, and sentence fragments are normal informal English; never "fix" them into formal questions.
When in doubt, do not flag. A missed tip is better than a pedantic one.
Before emitting a fix, drop it unless: a native editor would mark the original as an error (not taste), the fix keeps meaning and tone, and the original is ordinary prose.

CARELESS MODE: if the message was clearly dashed off without any care for language (many typos, no capitalization or punctuation, broken fragments, swearing, frustration), the user does not want feedback: set "careless" true and return no fixes. Judge the overall impression, not single errors; a message with a few genuine mistakes in otherwise careful writing is NOT careless.

Output ONLY one raw JSON object, no fences:
{{"careless":false,"fixes":[{{"original":"","fix":"","category":"","transfer":false,"explanation":""}}],"did_well":[]}}
- original: exact substring copied verbatim from the message, only the few words with the error
- fix: the single corrected replacement
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
    return c


def get_state(c, key, default=0.0):
    r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return float(r[0]) if r else default


def set_state(c, key, value):
    c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))


def recurrence(c):
    return dict(c.execute("SELECT category, COUNT(*) FROM findings WHERE ts>? GROUP BY category",
                          (time.time() - WINDOW,)).fetchall())


def rate(c, since, until, cat):
    """(findings per 100 words, words) in [since, until)."""
    words = c.execute("SELECT COALESCE(SUM(words),0) FROM prompts WHERE ts>=? AND ts<?", (since, until)).fetchone()[0]
    n = c.execute("SELECT COUNT(*) FROM findings WHERE category=? AND ts>=? AND ts<?", (cat, since, until)).fetchone()[0]
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


def collect_lines(c):
    """Pending tips (+ a sparse Btw) as display lines; marks them shown."""
    lines = []
    if NATIVE is None and not get_state(c, "hint_shown"):
        lines.append("Tip: run /english-coach:setup to tell the coach your native language, so it can spot transfer errors (shown once).")
        set_state(c, "hint_shown", 1)
    rows = c.execute("SELECT id, category, original, fix, explanation FROM findings "
                     "WHERE shown=0 ORDER BY id").fetchall()
    if rows:
        rec = recurrence(c)
        rows.sort(key=lambda r: -rec.get(r[1], 0))   # recurring areas first; no counts shown
        for _id, cat, orig, fix, expl in rows[:MAX_TIPS]:
            lines.append(f"  {RED}{orig}{OFF} → {GREEN}{fix}{OFF}  {DIM}{expl}{OFF}")
        c.execute("UPDATE findings SET shown=1 WHERE shown=0")
    # sparse "Btw": improved category AND the latest analyzed message handled it well
    if time.time() - get_state(c, "last_btw") > BTW_COOLDOWN:
        r = c.execute("SELECT did_well, ts FROM prompts ORDER BY id DESC LIMIT 1").fetchone()
        well = set(json.loads(r[0])) if r and r[0] and r[1] > get_state(c, "last_careless") else set()
        for cat, (old, new) in improved(c).items():
            if cat in well:
                lines.append(f"  Btw: your {cat} is getting better ({old:.1f} → {new:.1f} slips per 100 words).")
                set_state(c, "last_btw", time.time())
                break
    return lines


def emit(lines):
    if lines:
        # Claude Code prefixes every line with "<Hook> says:", so keep one short line per tip
        print(json.dumps({"systemMessage": "\n".join("✎ " + ln.strip() for ln in lines)}))


def hook():
    """UserPromptSubmit: show anything pending, start analysis of this prompt in the background."""
    if os.environ.get(GUARD) or PAUSED.exists():
        return
    try:
        prompt = (json.load(sys.stdin).get("prompt") or "").strip()
    except Exception:
        return
    lines = []
    try:
        c = db()
        lines = collect_lines(c)
        c.commit()
        if coachable(prompt):
            token = str(time.time())
            set_state(c, "inflight", token)
            c.commit()
            p = subprocess.Popen([sys.executable, __file__, "--worker"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=dict(os.environ, **{GUARD: "1", "ENGLISH_COACH_TOKEN": token}),
                                 start_new_session=True)
            p.stdin.write(prompt.encode())
            p.stdin.close()
        c.close()
    except Exception:
        pass
    emit(lines)


def stop():
    """Stop hook: wait (bounded) for the running analysis, then show its tip in the same turn."""
    if os.environ.get(GUARD) or PAUSED.exists():
        return
    try:
        c = db()
        deadline = time.time() + STOP_WAIT
        while time.time() < deadline:
            tok = get_state(c, "inflight")
            if not tok or time.time() - tok > 60:
                break
            time.sleep(0.5)
        lines = collect_lines(c)
        c.commit()
        c.close()
        emit(lines)
    except Exception:
        pass


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


def validate(answer, prompt):
    """Keep only fixes whose original really occurs in the prompt and which change more than case/spacing."""
    low, spans, fixes = prompt.lower(), [], []
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
        spans.append((a, b))
        cat = f.get("category") if f.get("category") in CATEGORIES else "other"
        fixes.append((cat, int(bool(f.get("transfer"))), orig, fix, clean(f.get("explanation"), 300)))
    return fixes[:3]


def worker():
    prompt = sys.stdin.read().strip()
    c = db()
    try:
        analyze(c, prompt)
    finally:
        token = os.environ.get("ENGLISH_COACH_TOKEN")
        if token and get_state(c, "inflight") == float(token):
            set_state(c, "inflight", "0")
            c.commit()


def analyze(c, prompt):
    if not prompt:
        return
    top = [k for k, _ in sorted(recurrence(c).items(), key=lambda kv: -kv[1])[:3]]
    system = system_prompt() + (f"\nPay slightly more attention to this learner's recurring areas: {', '.join(top)}." if top else "")
    r = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence", "--disable-slash-commands",
                        "--tools", "", "--system-prompt", system,
                        f"<message>\n{prompt}\n</message>\nReview the message above. Reply with the JSON object only."],
                       capture_output=True, text=True, timeout=90)
    answers = [o for o in json_objects(r.stdout) if isinstance(o.get("fixes"), list)]
    if not answers:
        return
    answer = answers[-1]
    now = time.time()
    if answer.get("careless") is True:      # sloppy mode: no tips, excluded from all statistics
        set_state(c, "last_careless", now)
        c.commit()
        return
    well = [x for x in answer.get("did_well", []) if x in CATEGORIES]
    pid = c.execute("INSERT INTO prompts(ts, words, did_well) VALUES(?,?,?)",
                    (now, len(re.findall(r"\w+", prompt)), json.dumps(well))).lastrowid
    for cat, tr, orig, fix, expl in validate(answer, prompt):
        c.execute("INSERT INTO findings(prompt_id, ts, category, transfer, original, fix, explanation) "
                  "VALUES(?,?,?,?,?,?,?)", (pid, now, cat, tr, orig, fix, expl))
    c.commit()


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

    total_msgs, first = c.execute("SELECT COUNT(*), MIN(ts) FROM prompts").fetchone()
    if not total_msgs:
        print("No data yet. Tips and stats appear after a few messages.")
        return
    total_tips = c.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    since = time.strftime("%Y-%m-%d", time.localtime(first))
    print(paint("1", "English coach"), paint("2", f"· {total_msgs} messages checked since {since}, {total_tips} tips"))

    # headline: share of messages that needed no tip at all
    clean = []
    for w in range(WEEKS - 1, -1, -1):
        lo, hi = bucket(w)
        n = c.execute("SELECT COUNT(*) FROM prompts WHERE ts>=? AND ts<?", (lo, hi)).fetchone()[0]
        k = c.execute("SELECT COUNT(*) FROM prompts p WHERE ts>=? AND ts<? AND NOT EXISTS "
                      "(SELECT 1 FROM findings f WHERE f.prompt_id=p.id)", (lo, hi)).fetchone()[0]
        clean.append(k / n if n else None)
    last = next((v for v in reversed(clean) if v is not None), None)
    print(f"\nMessages without a slip  {spark(clean, 1.0)}  " +
          ("" if last is None else f"{last:.0%} this week"))

    # per category: weekly slips per 100 words on one shared scale
    cats = [r[0] for r in c.execute(
        "SELECT category FROM findings WHERE ts>? GROUP BY category ORDER BY COUNT(*) DESC",
        (now - WEEKS * W,)).fetchall()]
    series = {cat: [rate(c, *bucket(w), cat)[0] for w in range(WEEKS - 1, -1, -1)] for cat in cats}
    scale = max([v for vs in series.values() for v in vs if v] or [1.0])
    print(paint("2", "\nSlips per 100 words, 8 weeks (shared scale)"))
    for cat in cats:
        old, w_old = rate(c, now - 8 * W, now - 4 * W, cat)
        new, w_new = rate(c, now - 4 * W, now + 1, cat)
        if w_old < 200 or w_new < 200 or new is None:
            arrow = paint("2", "·")
        elif new < 0.7 * (old or 0):
            arrow = paint("32", "↓")
        elif new > 1.3 * (old or 0):
            arrow = paint("31", "↑")
        else:
            arrow = "→"
        cur = new if new is not None else 0.0
        print(f"  {cat:20} {spark(series[cat], scale)}  {cur:4.1f} {arrow}")

    rows = c.execute("SELECT original, fix, category, COUNT(*) n FROM findings "
                     "GROUP BY lower(original), lower(fix) HAVING n>1 ORDER BY n DESC LIMIT 6").fetchall()
    if rows:
        print(paint("2", "\nHabit phrases"))
        for o, f, cat, n in rows:
            print(f"  {n}×  {o} → {f}  " + paint("2", cat))

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
            if key == "native_language":
                cfg[key] = None if value.lower() in ("", "none", "unset") else value
            elif key == "color":
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
    for key, label in (("min_words", "Minimum words per message to coach"), ("max_tips", "Maximum tips shown at once")):
        v = input(f"{label} [{cfg[key]}]: ").strip()
        if v.isdigit() and int(v) > 0:
            cfg[key] = int(v)
    save_config(cfg)
    print(f"\nSaved to {CONFIG_FILE}. Takes effect from the next message.")


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["config"]:
            config_cmd(sys.argv[2:])
        elif sys.argv[1:2] == ["setup"]:
            setup()
        elif "--worker" in sys.argv:
            worker()
        elif "stop" in sys.argv:
            stop()
        elif "report" in sys.argv:
            report()
        elif "off" in sys.argv:
            DATA.mkdir(parents=True, exist_ok=True); PAUSED.touch()
        elif "on" in sys.argv:
            PAUSED.unlink(missing_ok=True)
        else:
            hook()
    except Exception:
        pass
