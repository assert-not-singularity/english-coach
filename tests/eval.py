#!/usr/bin/env python3
"""Prompt regression eval for the English coach.

Runs the review pipeline the mod uses (scripts/english-coach.py prepare -> a Haiku call through the claude CLI ->
ingest) on the fixture messages in eval-cases.json, several times each, against throwaway data, and reports a
pass rate per case. Model output is probabilistic, so a case passes at >= --min (default 0.75).

  python3 tests/eval.py                                   # the working-tree script
  python3 tests/eval.py -n 5 --lang Portuguese            # more runs, another native language
  python3 tests/eval.py --script new=scripts/english-coach.py --script old=/path/to/old.py
                                                          # compare a candidate against what is installed
Exit code 1 when any case of any script falls below --min.
"""
import argparse, json, os, re, sqlite3, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SCRIPT = HERE.parent / "scripts" / "english-coach.py"


def run_once(script, text, lang, seed=()):
    with tempfile.TemporaryDirectory() as tmp:
        cfg, data = Path(tmp) / "cfg", Path(tmp) / "data"
        cfg.mkdir()
        (cfg / "config.json").write_text(json.dumps({"native_language": lang or None, "min_words": 1,
                                                     "max_tips": 3, "dictation_marker": "[dictated]"}))
        env = dict(os.environ, ENGLISH_COACH_DIR=str(data), ENGLISH_COACH_CONFIG_DIR=str(cfg))
        if seed:   # corrections made "just before": create the schema, then insert them as shown tips
            subprocess.run([sys.executable, "-I", str(script), "report"], env=env, capture_output=True, timeout=60)
            c = sqlite3.connect(data / "log.db")
            for i, (orig, fix) in enumerate(seed):
                c.execute("INSERT INTO findings(prompt_id,ts,category,transfer,original,fix,explanation,shown) "
                          "VALUES(0,strftime('%s','now'),'phrasing',0,?,?,'',1)", (orig, fix))
            c.commit()
            c.close()
        def step(*args, stdin=""):
            return subprocess.run([sys.executable, "-I", str(script), *args], input=stdin, text=True, env=env,
                                  capture_output=True, timeout=60).stdout

        prep = json.loads(step("prepare", stdin=json.dumps({"prompt": text})) or "{}")
        if prep.get("coach"):
            reply = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence", "--tools", "",
                                    "--disable-slash-commands", "--setting-sources", "project",
                                    "--system-prompt", prep["system"], prep["user"]],
                                   capture_output=True, text=True, timeout=180, env=env).stdout
            step("ingest", stdin=json.dumps({"prompt": prep["text"], "mode": prep["mode"], "reply": reply}))
        db = data / "log.db"
        if not db.exists():
            return {"careless": False, "findings": [], "error": "no database (review failed)"}
        c = sqlite3.connect(db)
        careless = c.execute("SELECT 1 FROM state WHERE key='last_careless'").fetchone() is not None
        findings = c.execute("SELECT original, fix, category FROM findings WHERE prompt_id != 0").fetchall()   # 0 = seeded history
        return {"careless": careless, "findings": findings, "error": None}


def check(case, out):
    """List of failed expectations (empty = pass)."""
    if out["error"]:
        return [out["error"]]
    problems, found = [], out["findings"]
    if out["careless"] != case.get("careless", False):
        problems.append(f"careless={out['careless']} (expected {case.get('careless', False)})")
    if case.get("no_findings") and found:
        problems.append("unexpected tip: " + "; ".join(f"{o} -> {f}" for o, f, _ in found))
    for want in case.get("flag", []):
        if not any(want.lower() in o.lower() for o, _, _ in found):
            problems.append(f"missed '{want}'")
    for banned in case.get("fix_lacks", []):
        if any(banned.lower() in f.lower() for _, f, _ in found):
            problems.append(f"fix contains '{banned}'")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--runs", type=int, default=3)
    ap.add_argument("--script", action="append", default=[], help="[label=]path, repeatable")
    ap.add_argument("--lang", default="German", help="native language for the run ('' for none)")
    ap.add_argument("--min", type=float, default=0.75)
    ap.add_argument("--case", default="", help="only cases whose id contains this")
    ap.add_argument("-j", "--jobs", type=int, default=6)
    args = ap.parse_args()

    scripts = []
    for spec in args.script or [str(DEFAULT_SCRIPT)]:
        label, _, path = spec.partition("=") if "=" in spec else ("", "", spec)
        scripts.append((label or Path(path).parent.parent.name, Path(path).expanduser().resolve()))
    cases = [c for c in json.loads((HERE / "eval-cases.json").read_text()) if args.case in c["id"]]

    jobs = [(s, c, i) for s in scripts for c in cases for i in range(args.runs)]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        outs = list(pool.map(lambda j: run_once(j[0][1], j[1]["text"], args.lang, j[1].get("seed", ())), jobs))

    results = {}   # (label, case id) -> list of problem lists
    for (s, c, _), out in zip(jobs, outs):
        results.setdefault((s[0], c["id"]), []).append(check(c, out))

    width = max(len(c["id"]) for c in cases)
    print(f"{'case':{width}}  " + "  ".join(f"{label:>8}" for label, _ in scripts))
    failed = False
    for c in cases:
        cells = []
        for label, _ in scripts:
            runs = results[(label, c["id"])]
            rate = sum(1 for r in runs if not r) / len(runs)
            failed |= rate < args.min and not c.get("informational")
            cells.append(f"{rate:>7.0%}{('~' if c.get('informational') else '*') if rate < args.min else ' '}")
        print(f"{c['id']:{width}}  " + "  ".join(cells))
    print(f"\n(* below {args.min:.0%}, fails the run; ~ informational, not gating; {args.runs} runs per case)")
    for label, _ in scripts:
        for c in cases:
            bad = sorted({p for r in results[(label, c['id'])] for p in r})
            if bad and sum(1 for r in results[(label, c['id'])] if not r) / args.runs < args.min:
                print(f"  [{label}] {c['id']}: " + " | ".join(bad)[:220])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
