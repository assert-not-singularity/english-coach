"""Unit tests for scripts/english-coach.py (no model calls). Run: python3 -m unittest discover -s tests -p "test_*.py" -v"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "english-coach.py"


def load_module(data_dir, config_dir):
    """Import the script with its data and config directories pointed at temporary folders."""
    os.environ["ENGLISH_COACH_DIR"], os.environ["ENGLISH_COACH_CONFIG_DIR"] = str(data_dir), str(config_dir)
    spec = importlib.util.spec_from_file_location("english_coach", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Workspace(unittest.TestCase):
    """A throwaway data and config directory per test; `cli` runs the script as a subprocess against them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data, self.cfg = Path(self._tmp.name) / "data", Path(self._tmp.name) / "cfg"
        self.cfg.mkdir()
        self.config({"native_language": "German", "dictation_marker": "[dictated]"})
        self.ec = load_module(self.data, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, values):
        (self.cfg / "config.json").write_text(json.dumps(values))

    def cli(self, *args, stdin=""):
        env = dict(os.environ, ENGLISH_COACH_DIR=str(self.data), ENGLISH_COACH_CONFIG_DIR=str(self.cfg))
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], input=stdin, text=True, env=env,
                              capture_output=True, timeout=60).stdout

    def ingest(self, prompt, fixes, mode="written", context="", careless=False):
        reply = json.dumps({"careless": careless, "fixes": fixes, "did_well": []})
        out = self.cli("ingest", stdin=json.dumps({"prompt": prompt, "mode": mode, "reply": reply, "context": context}))
        return json.loads(out)

    @staticmethod
    def fix(original, fix, severity="error", category="phrasing", transfer=False):
        return {"original": original, "fix": fix, "category": category, "transfer": transfer, "severity": severity,
                "explanation": "x"}

    def findings(self, where="1=1"):
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            return c.execute(f"SELECT id, original, fix, severity, dismissed, mode FROM findings WHERE {where} ORDER BY id").fetchall()


class Marker(Workspace):
    def test_marker_anywhere_in_any_case(self):
        for text in ("Hello there. [dictated]", "[Dictated] Hello there.", "Hello [dictated] there. [DICTATED]"):
            cleaned, speech = self.ec.split_marker(text)
            self.assertTrue(speech, text)
            self.assertNotIn("dictated", cleaned.lower())

    def test_typed_text_is_untouched_including_line_breaks(self):
        text = "Look at this:\n> quoted line\nWhy?"
        self.assertEqual(self.ec.split_marker(text), (text, False))

    def test_line_breaks_survive_when_a_marker_is_removed(self):
        cleaned, speech = self.ec.split_marker("Line one. [dictated]\nLine two.")
        self.assertTrue(speech)
        self.assertIn("\n", cleaned)


class Guards(Workspace):
    def validate(self, prompt, original, fix, **kwargs):
        return self.ec.validate({"fixes": [self.fix(original, fix)]}, prompt, **kwargs)

    def test_typo_is_not_a_finding(self):
        self.assertFalse(self.ec.is_slip("explain me", "explain to me"))
        self.assertTrue(self.ec.is_slip("but now always", "but not always"))
        self.assertEqual(self.validate("I read it but now always.", "now always", "not always"), [])

    def test_sound_alikes_only_in_speech(self):
        self.assertTrue(self.ec.sounds_alike("there going", "they're going"))
        self.assertTrue(self.ec.sounds_alike("write the code", "right the code"))
        self.assertFalse(self.ec.sounds_alike("informations", "information"))
        prompt = "I think there going to merge it."
        self.assertEqual(self.validate(prompt, "there going", "they're going", speech=True), [])
        self.assertEqual(len(self.validate(prompt, "there going", "they're going", speech=False)), 1)

    def test_quoted_code_and_blockquote_text_is_ignored(self):
        for prompt, original in (('The log says "we was here" now.', "we was here"), ("Run `we was here` now.", "we was here"),
                                 ("Look:\n> The build have failed.\nWhy?", "build have failed")):
            self.assertEqual(self.validate(prompt, original, "xx yy"), [], prompt)

    def test_only_added_words_around_terse_phrasing_is_not_a_fix(self):
        self.assertEqual(self.validate("Ok. Makes sense.", "Makes sense.", "That makes sense."), [])
        self.assertEqual(self.validate("Ok. Makes sense.", "Makes sense", "Makes sense to me"), [])
        self.assertEqual(len(self.validate("Makes absolutely sense.", "absolutely sense", "absolute sense")), 1)

    def test_phrase_quoted_from_the_assistant_is_dropped(self):
        context = "The case retry-budget-is-fixed checks that a dictated sentence gets no tip."
        self.assertEqual(self.validate("What is retry budget is fixed?", "What is retry budget is fixed", "Is it fixed", context=context), [])
        self.assertEqual(len(self.validate("Can you explain me why?", "explain me", "explain to me", context=context)), 1)

    def test_recently_corrected_phrase_is_not_flagged_again(self):
        recent = ({"explain me"}, {"explain to me"})
        self.assertEqual(self.validate("It said explain me.", "explain me", "explain to me", recent=recent), [])

    def test_a_paraphrase_of_a_recent_correction_is_also_a_repeat(self):
        recent = ({"does the llm models also score well"}, {"do the llm models also score well"})
        self.assertEqual(self.validate("Why does the LLM models fail?", "does the LLM models", "do the LLM models", recent=recent), [])
        self.assertEqual(len(self.validate("Why does the cache fail?", "does the cache", "do the cache", recent=recent)), 1)

    def test_at_most_one_style_item_and_errors_come_first(self):
        answer = {"fixes": [self.fix("alpha beta", "gamma delta", "style"), self.fix("explain me", "explain to me"),
                            self.fix("rather makes", "makes more", "style")]}
        result = self.ec.validate(answer, "Can you explain me why alpha beta rather makes sense?")
        self.assertEqual([r[5] for r in result], ["error", "style"])


class Storage(Workspace):
    def test_old_database_is_migrated(self):
        old = self.data
        old.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(old / "log.db")
        c.executescript("CREATE TABLE prompts(id INTEGER PRIMARY KEY, ts REAL, words INTEGER, did_well TEXT);"
                        "CREATE TABLE findings(id INTEGER PRIMARY KEY, prompt_id INTEGER, ts REAL, category TEXT, transfer INTEGER,"
                        " original TEXT, fix TEXT, explanation TEXT, shown INTEGER DEFAULT 0);"
                        "CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT);"
                        "INSERT INTO prompts(ts,words,did_well) VALUES(1,5,'[]');"
                        "INSERT INTO findings(prompt_id,ts,category,transfer,original,fix,explanation) VALUES(1,1,'x',0,'a','b','c');")
        c.commit()
        c.close()
        self.cli("report")
        with closing(sqlite3.connect(old / "log.db")) as check:
            columns = {r[1] for r in check.execute("PRAGMA table_info(findings)")}
        self.assertTrue({"mode", "severity", "dismissed", "dismiss_reason"} <= columns)
        self.assertEqual(self.findings()[0][3:], ("error", 0, "written"))

    def test_careless_flag_needs_a_sloppy_form_and_written_mode(self):
        careful = "Can you explain me why this fails, please?"
        self.assertEqual(self.ingest(careful, [self.fix("explain me", "explain to me")], careless=True)["careless"], False)
        sloppy = "ugh just fix the damn thing it fails again whatever"
        self.assertEqual(self.ingest(sloppy, [], careless=True)["careless"], True)
        self.assertEqual(self.ingest(sloppy + " now", [], mode="dictated", careless=True)["careless"], False)

    def test_style_items_are_stored_but_shown_only_on_request(self):
        fixes = [self.fix("explain me", "explain to me"), self.fix("rather makes sense", "makes more sense", "style")]
        shown = self.ingest("Can you explain me why it rather makes sense?", fixes)["fixes"]
        self.assertEqual([f["severity"] for f in shown], ["error"])
        self.assertEqual(len(self.findings()), 2)
        self.config({"native_language": "German", "show_style": True})
        shown = self.ingest("Please explain me again why it rather makes sense, thanks!", [self.fix("rather makes sense", "makes more sense", "style")])["fixes"]
        self.assertEqual([f["severity"] for f in shown], [])    # corrected a moment ago: not repeated

    def test_dictated_statistics_are_kept_apart(self):
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me", category="preposition")])
        self.ingest("Could you check weather it works for me please", [self.fix("check weather", "check whether")], mode="dictated")
        report = self.cli("report")
        self.assertIn("1 tips", report.splitlines()[0])
        self.assertIn("Dictated messages", report)

    def test_style_items_get_their_own_report_section_and_do_not_count_as_errors(self):
        self.ingest("Can you explain me why it rather makes sense?", [self.fix("rather makes sense", "makes more sense", "style")])
        report = self.cli("report")
        self.assertIn("0 tips", report.splitlines()[0])
        self.assertIn("Native polish: 1 style suggestions", report)


class Dismiss(Workspace):
    def test_dismissed_finding_leaves_the_statistics_and_is_never_flagged_again(self):
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me", category="preposition")])
        self.assertIn("1 tips", self.cli("report").splitlines()[0])
        self.assertIn("Dismissed 1", self.cli("dismiss", "1", "asr"))
        self.assertIn("0 tips", self.cli("report").splitlines()[0])
        self.assertIn("[dismissed: asr]", self.cli("recent"))
        again = self.ingest("Please explain me this once more with an example", [self.fix("explain me", "explain to me again")])
        self.assertEqual(again["fixes"], [])

    def test_undismiss_restores(self):
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me")])
        self.cli("dismiss", "1")
        self.assertIn("Restored 1", self.cli("undismiss", "1"))
        self.assertEqual(self.findings()[0][4], 0)

    def test_dismiss_last_takes_every_finding_of_the_latest_message(self):
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me")])
        self.ingest("I am since two days trying to fix it, really.", [self.fix("I am since two days trying", "I have been trying for two days"),
                                                                      self.fix("fix it", "solve it")])
        output = self.cli("dismiss", "last", "false")
        self.assertEqual(output.count("Dismissed"), 2)
        self.assertEqual([f[4] for f in self.findings()], [0, 1, 1])


class Watchlist(Workspace):
    def say(self, text, original, fix, **kwargs):
        return self.ingest(text, [self.fix(original, fix, **kwargs)])

    def test_keys_are_per_change_and_ignore_the_sentence(self):
        keys = self.ec.habit_keys("defer this score out of the used language", "derive this score from the language used")
        self.assertIn(("defer", "derive"), keys)
        self.assertEqual(self.ec.habit_keys("explain me why", "explain to me why"), [("explain me", "explain to me")])
        self.assertEqual(self.ec.habit_keys("I am since two days trying", "I have been trying for two days")[0][0], "i am since two days trying")

    def test_reminder_only_after_the_mistake_came_back(self):
        first = self.say("Can we defer this score from the text, please?", "defer this score", "derive this score", category="word-choice")
        self.assertIsNone(first["fixes"][0]["reminder"])
        second = self.say("Then we defer the level from the data, right?", "defer the level", "derive the level", category="word-choice")
        self.assertIn("derive", second["fixes"][0]["reminder"])

    def test_one_count_per_message(self):
        self.ingest("We defer this and defer that again, really.", [self.fix("defer this", "derive this"), self.fix("defer that", "derive that")])
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            hits = c.execute("SELECT hits FROM habits WHERE wrong='defer'").fetchone()[0]
        self.assertEqual(hits, 1)

    def test_correct_uses_resolve_it_and_a_relapse_reactivates_it(self):
        self.say("Can we defer this score from the text, please?", "defer this score", "derive this score")
        self.say("Then we defer the level from the data, right?", "defer the level", "derive the level")
        milestone = None
        for i in range(self.ec.RESOLVE_AFTER):
            out = self.ingest(f"Message number {i}: we derive the level from the data here.", [])
            milestone = out["btw"] or milestone
        self.assertIn("leaves your watchlist", milestone)
        self.assertIn("resolved", self.cli("watch"))
        self.say("But today we defer the score once more, honestly.", "defer the score", "derive the score")
        self.assertNotIn("resolved", self.cli("watch").split("defer")[1].split("\n")[0])

    def test_dismissing_reduces_the_count(self):
        self.say("Can we defer this score from the text, please?", "defer this score", "derive this score")
        self.say("Then we defer the level from the data, right?", "defer the level", "derive the level")
        self.cli("dismiss", "2", "asr")
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            hits = c.execute("SELECT hits FROM habits WHERE wrong='defer'").fetchone()[0]
        self.assertEqual(hits, 1)

    def test_manual_entry_reaches_the_reviewer_prompt_and_can_be_removed(self):
        self.assertIn("Watching", self.cli("watch", "add", "defer", "derive", "derive = get from"))
        prepared = json.loads(self.cli("prepare", stdin=json.dumps({"prompt": "Can you explain how this score is calculated, please?"})))
        self.assertIn('"defer" instead of "derive"', prepared["system"])
        self.assertIn("Removed 1 entry", self.cli("watch", "remove", "defer"))
        prepared = json.loads(self.cli("prepare", stdin=json.dumps({"prompt": "Can you explain how this score is calculated, please?"})))
        self.assertNotIn("instead of", prepared["system"])

    def test_report_lists_the_watchlist(self):
        self.say("Can we defer this score from the text, please?", "defer this score", "derive this score")
        self.say("Then we defer the level from the data, right?", "defer the level", "derive the level")
        self.assertIn("Watchlist", self.cli("report"))


class Forget(Workspace):
    def test_forget_last_deletes_the_latest_finding_without_a_trace(self):
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me")])
        self.ingest("I am since two days trying to fix it, really.", [self.fix("I am since two days trying", "I have been trying for two days"),
                                                                      self.fix("fix it", "solve it")])
        self.assertIn("Removed 3", self.cli("forget"))
        self.assertEqual([f[0] for f in self.findings()], [1, 2])
        self.assertEqual(self.findings("dismissed=1"), [])

    def test_forget_message_takes_every_finding_of_the_latest_message(self):
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me")])
        self.ingest("I am since two days trying to fix it, really.", [self.fix("I am since two days trying", "I have been trying for two days"),
                                                                      self.fix("fix it", "solve it")])
        self.assertEqual(self.cli("forget", "message").count("Removed"), 2)
        self.assertEqual([f[0] for f in self.findings()], [1])

    def test_a_forgotten_phrase_can_be_flagged_again_unlike_a_dismissed_one(self):
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me")])
        self.cli("forget", "1")
        # nothing is left behind, so the same phrase may be flagged again
        again = self.ingest("Please explain me this once more, with an example", [self.fix("explain me", "explain to me again")])
        self.assertEqual(len(again["fixes"]), 1)

    def test_forget_updates_the_watchlist_counts(self):
        self.ingest("Can we defer this score from the text, please?", [self.fix("defer this score", "derive this score")])
        self.ingest("Then we defer the level from the data, right?", [self.fix("defer the level", "derive the level")])
        self.cli("forget")
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            self.assertEqual(c.execute("SELECT hits FROM habits WHERE wrong='defer'").fetchone()[0], 1)
        self.cli("forget")
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            self.assertIsNone(c.execute("SELECT hits FROM habits WHERE wrong='defer'").fetchone())


class FalseFriends(Workspace):
    def system(self, text):
        return json.loads(self.cli("prepare", stdin=json.dumps({"prompt": text})))["system"]

    def test_the_hint_appears_only_for_words_in_the_message(self):
        self.assertIn('"become"', self.system("I think I will become a new laptop next week, because mine is slow."))
        self.assertNotIn("Known false friends", self.system("Can you explain me why the build is failing, because I am since two hours trying?"))

    def test_a_phrase_entry_matches_as_a_phrase(self):
        self.assertIn('"in that case"', self.system("I mean in that case where this test failed, the log was empty."))
        self.assertNotIn("in that case", self.system("The case where this test failed was in that module, honestly."))

    def test_no_hint_without_a_known_language(self):
        self.config({"native_language": "Klingon"})
        self.assertNotIn("Known false friends", self.system("I will become a new laptop next week, because mine is slow."))


if __name__ == "__main__":
    unittest.main()
