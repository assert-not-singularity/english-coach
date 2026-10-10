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

    def test_text_after_a_quote_block_is_still_the_users_own(self):
        prompt = "Look:\n> The build have failed.\nBut I am since two days trying to fix it."
        kept = self.validate(prompt, "I am since two days trying", "I have been trying for two days")
        self.assertEqual(len(kept), 1)
        self.assertEqual(self.validate(prompt, "build have failed", "build has failed"), [])

    def test_sentences_are_counted_without_quotes_and_code(self):
        text = "Look at this. It fails again! Why?\nSecond line without a stop\n> a quoted line.\n```\ncode. more.\n```\nLast one."
        spans = self.ec.sentence_spans(text)
        self.assertEqual([text[a:b] for a, b in spans], ["Look at this.", "It fails again!", "Why?", "Second line without a stop", "Last one."])
        self.assertEqual(self.ec.sentence_index(spans, text, "fails again"), 1)
        self.assertIsNone(self.ec.sentence_index(spans, text, "not in the text"))

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
        self.assertNotIn('"defer" instead of', prepared["system"])   # (a phrase meaning in the offered list may say "instead of")

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


class Slips(Workspace):
    def test_which_small_word_changes_are_slips(self):
        slip = self.ec.is_small_word_slip
        self.assertTrue(slip("If have", "If I have", speech=False))                      # a forgotten subject pronoun
        self.assertFalse(slip("explain me", "explain to me", speech=False))              # a missing preposition is a gap when typed
        self.assertFalse(slip("does the models", "do the models", speech=False))         # a swap is not an omission when typed
        self.assertTrue(slip("explain me", "explain to me", speech=True))                # in dictation any small word may be misheard
        self.assertFalse(slip("does the models", "do the models", speech=True))         # a swap that does not sound alike is the speaker's
        self.assertTrue(slip("0.5 is phrase suggestions", "0.5 is for phrase suggestions", speech=True))   # a word missing from the transcript
        self.assertFalse(slip("should have been looked", "should have looked", speech=True))   # an extra word is the speaker's own
        self.assertFalse(slip("wondering that", "wondering if", speech=True))
        self.assertTrue(slip("I have a", "I have an", speech=True) is False)             # not a missing word
        self.assertFalse(slip("informations", "information", speech=True))              # not a small word
        self.assertFalse(slip("defer this", "derive this", speech=False))

    def age_findings(self, seconds=3600):
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            c.execute("UPDATE findings SET ts = ts - ?", (seconds,))
            c.commit()

    def test_a_first_omission_is_a_hidden_slip_not_an_error(self):
        out = self.ingest("If have time later, I will check the logs again.", [self.fix("If have", "If I have", category="pronoun")])
        self.assertEqual(out["fixes"], [])
        self.assertEqual(self.findings()[0][3], "slip")
        report = self.cli("report")
        self.assertIn("0 tips", report.splitlines()[0])
        self.assertIn("Slips: 1", report)
        self.assertIn("slip", self.cli("recent"))

    def test_the_same_omission_again_is_promoted_to_an_error(self):
        self.ingest("If have time later, I will check the logs again.", [self.fix("If have", "If I have", category="pronoun")])
        self.age_findings()
        out = self.ingest("If have an idea tomorrow, I will write it down.", [self.fix("If have", "If I have", category="pronoun")])
        self.assertEqual([f["severity"] for f in out["fixes"]], ["error"])
        self.assertEqual([f[3] for f in self.findings()], ["slip", "error"])

    def test_a_missing_preposition_in_typed_text_stays_an_error(self):
        out = self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me", category="preposition")])
        self.assertEqual([f["severity"] for f in out["fixes"]], ["error"])

    def test_a_small_word_missing_from_dictated_text_is_a_slip(self):
        out = self.ingest("Then the point zero five is phrase suggestions, honestly.", [self.fix("is phrase suggestions", "is for phrase suggestions")], mode="dictated")
        self.assertEqual(out["fixes"], [])
        self.assertEqual(self.findings()[0][3], "slip")

    def test_an_extra_or_unlike_small_word_in_dictated_text_stays_an_error(self):
        out = self.ingest("Then how it should have been looked is not clear to me.", [self.fix("should have been looked", "should have looked")], mode="dictated")
        self.assertEqual([f["severity"] for f in out["fixes"]], ["error"])
        out = self.ingest("I am just wondering that this is possible at all.", [self.fix("wondering that", "wondering if")], mode="dictated")
        self.assertEqual([f["severity"] for f in out["fixes"]], ["error"])


class Overview(Workspace):
    def overview(self, mode="both"):
        return json.loads(self.cli("overview", mode))

    def seed(self):
        self.ingest("Can you explain me why this fails, please? It happens every time.", [self.fix("explain me", "explain to me", category="preposition", transfer=True)])
        self.ingest("Can we defer this score from the text, please, now", [self.fix("defer this score", "derive this score", category="word-choice")], mode="dictated")
        self.ingest("If have time later, I will check the logs again and tell you.", [self.fix("If have", "If I have", category="pronoun")])
        self.ingest("Another clean message that needs no correction at all, thanks.", [])

    def test_modes_are_kept_apart(self):
        self.seed()
        written, spoken, both = self.overview("written"), self.overview("dictated"), self.overview("both")
        self.assertEqual((written["messages"], spoken["messages"], both["messages"]), (3, 1, 4))
        self.assertEqual([c["category"] for c in written["categories"]], ["preposition"])
        self.assertEqual([c["category"] for c in spoken["categories"]], ["word-choice"])
        self.assertEqual(sorted(c["category"] for c in both["categories"]), ["preposition", "word-choice"])

    def test_slips_and_errors_are_counted_apart(self):
        self.seed()
        data = self.overview("written")
        self.assertEqual((data["errors"], data["slips"]), (1, 1))
        self.assertEqual(data["slip_examples"][0]["original"], "If have")

    def test_a_category_carries_examples_and_eight_weekly_values(self):
        self.seed()
        category = self.overview("written")["categories"][0]
        self.assertEqual(len(category["weekly"]), 8)
        self.assertEqual(category["examples"][0]["original"], "explain me")
        self.assertTrue(category["examples"][0]["transfer"])

    def test_examples_carry_the_changed_words_and_the_transfer_share(self):
        self.ingest("We defer this score from the text, please check it now", [self.fix("defer this score", "derive this score", category="word-choice", transfer=True)])
        data = self.overview("written")
        self.assertEqual(data["categories"][0]["examples"][0]["core"], {"wrong": "defer", "right": "derive"})
        self.assertEqual(data["transfer"]["total"], 1)
        self.assertAlmostEqual(data["transfer"]["share"], 1.0)
        self.assertEqual(data["weeks_with_data"], 1)

    def test_accuracy_does_not_depend_on_message_length(self):
        # the same one error in a short and in a long message: per 100 words it is lower for the long one, the clean share lower too
        self.ingest("Can you explain me why this fails, please?", [self.fix("explain me", "explain to me", category="preposition")])
        long_text = "This is a longer message. " * 12 + "Can you explain me why the second job fails, please?"
        self.ingest(long_text, [self.fix("explain me", "explain to me", category="preposition")])
        data = self.overview("written")
        self.assertEqual(data["errors"], 1)                     # the second occurrence is a repeat of a recent correction
        self.assertAlmostEqual(data["errors_per_100_words"], 100 * 1 / data["words"])
        buckets = {b["label"]: b for b in data["length_buckets"]}
        self.assertEqual(buckets["1-10 words"]["clean_share"], 0.0)
        self.assertEqual(buckets["61+ words"]["clean_share"], 1.0)

    def test_error_free_sentences(self):
        text = "First sentence is fine. Can you explain me why this fails? Third one ends here. And a fourth."
        self.ingest(text, [self.fix("explain me", "explain to me", category="preposition")])
        data = self.overview("written")
        self.assertEqual(data["sentences"], 4)
        self.assertAlmostEqual(data["error_free_sentences"], 0.75)

    def test_clean_share(self):
        self.seed()
        self.assertAlmostEqual(self.overview("written")["clean_now"], 2 / 3)

    def test_an_empty_database_gives_empty_data(self):
        data = self.overview("both")
        self.assertEqual((data["messages"], data["errors"], data["categories"], data["since"]), (0, 0, [], None))
        self.assertIsNone(data["clean_now"])

    def test_dismissed_findings_leave_the_overview(self):
        self.seed()
        self.cli("dismiss", "1", "false")
        self.assertEqual(self.overview("written")["categories"], [])


class Backfill(Workspace):
    def transcripts(self, records):
        folder = Path(self._tmp.name) / "projects" / "-some-project"
        folder.mkdir(parents=True)
        (folder / "session.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return Path(self._tmp.name) / "projects"

    def run_backfill(self, root, *args):
        env = dict(os.environ, ENGLISH_COACH_DIR=str(self.data), ENGLISH_COACH_CONFIG_DIR=str(self.cfg), ENGLISH_COACH_TRANSCRIPTS=str(root))
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "backfill", *args], text=True, env=env, capture_output=True, timeout=60).stdout

    def test_old_rows_get_sentence_counts_and_positions_from_the_session_logs(self):
        text = "First sentence is fine. Can you explain me why this fails? Third one ends here."
        self.ingest(text, [self.fix("explain me", "explain to me", category="preposition")])
        with closing(sqlite3.connect(self.data / "log.db")) as c:                       # make it look like an old row
            c.execute("UPDATE prompts SET sentences=NULL"); c.execute("UPDATE findings SET sent_idx=NULL")
            c.commit(); ts = c.execute("SELECT ts FROM prompts").fetchone()[0]
        import datetime
        stamp = datetime.datetime.fromtimestamp(ts - 9, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        root = self.transcripts([{"type": "user", "timestamp": stamp, "message": {"content": text}}])
        self.assertIn("Would update 1 of 1", self.run_backfill(root, "--dry-run"))
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            self.assertIsNone(c.execute("SELECT sentences FROM prompts").fetchone()[0])
        self.assertIn("Updated 1 of 1", self.run_backfill(root))
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            self.assertEqual(c.execute("SELECT sentences FROM prompts").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT sent_idx FROM findings").fetchone()[0], 1)

    def test_rows_without_a_matching_message_stay_empty_and_nothing_is_printed_from_the_logs(self):
        self.ingest("Can you explain me why this fails, please? It happens every time.", [])
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            c.execute("UPDATE prompts SET sentences=NULL"); c.commit()
        root = self.transcripts([{"type": "user", "timestamp": "2020-01-01T00:00:00Z", "message": {"content": "A completely different old message."}}])
        out = self.run_backfill(root)
        self.assertIn("0 of 1", out)
        self.assertNotIn("different", out)


class Summaries(Workspace):
    def overview(self, mode="both"):
        return json.loads(self.cli("overview", mode))

    def seed(self):
        self.ingest("Can you explain me why this fails, please? It happens every time.", [self.fix("explain me", "explain to me", category="preposition", transfer=True)])
        self.ingest("We navigate back into the overview page now and then, honestly.", [self.fix("back into the overview", "back to the overview", category="preposition", transfer=True)])
        self.ingest("I just try reopening it once more, then we will see what happens.", [self.fix("I just try reopening", "I'll just try reopening", category="verb-form")])

    def test_core_change_keeps_enough_context_to_mean_something(self):
        core = self.ec.core_change
        self.assertEqual(core("I just try reopening", "I'll just try reopening"), {"wrong": "i just try", "right": "i'll just try"})
        self.assertEqual(core("or mouse pointer not changing", "or the mouse pointer doesn't change"), {"wrong": "not changing", "right": "doesn't change"})
        self.assertEqual(core("defer this score", "derive this score"), {"wrong": "defer", "right": "derive"})

    def test_summary_input_groups_the_corrections_by_category(self):
        self.seed()
        out = json.loads(self.cli("summary-input", "written"))
        self.assertEqual(out["categories"], ["preposition", "verb-form"])
        self.assertEqual(out["model"], "sonnet")
        groups = json.loads(out["prompt"])
        self.assertEqual(len(groups["preposition"]), 2)
        self.assertIn("German", out["system"])
        self.assertNotIn("dictated", out["prompt"])

    def test_a_stored_summary_appears_in_the_overview_and_goes_stale_with_new_findings(self):
        self.seed()
        reply = json.dumps({"categories": {"preposition": {"headline": "You pick the preposition by its German counterpart.", "detail": "Learn the set phrases."},
                                           "unknown": {"headline": "ignored", "detail": ""}},
                            "focus": ["Watch prepositions after verbs of movement.", "Second.", "Third.", "Fourth is dropped."]})
        self.assertEqual(json.loads(self.cli("summary-store", "written", stdin=json.dumps({"reply": "Here you go: " + reply})))["stored"], 1)
        data = self.overview("written")
        by_cat = {c["category"]: c for c in data["categories"]}
        self.assertIn("German counterpart", by_cat["preposition"]["summary"]["headline"])
        self.assertEqual(by_cat["preposition"]["summary"]["detail"], "Learn the set phrases.")
        self.assertFalse(by_cat["preposition"]["summary_stale"])
        self.assertIsNone(by_cat["verb-form"]["summary"])
        self.assertEqual(len(data["focus"]), 3)
        self.assertFalse(data["focus_stale"])
        self.ingest("Another sentence that needs fixing, so please look at it closely now.", [self.fix("needs fixing", "needs a fix", category="verb-form")])
        self.assertTrue(self.overview("written")["focus_stale"])

    def due(self):
        return json.loads(self.cli("summary-due"))["modes"]

    def many(self, n, start=0):
        for i in range(n):
            self.ingest(f"Message number {start + i} says explain me the thing {start + i} please.", [self.fix(f"explain me the thing {start + i}", f"explain to me the thing {start + i}", category="preposition")])
            self.age_findings()

    def age_findings(self, seconds=3600):
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            c.execute("UPDATE findings SET ts = ts - ?", (seconds,)); c.commit()

    def test_nothing_is_due_before_there_is_enough_to_summarize(self):
        self.many(5)
        self.assertEqual(self.due(), [])

    def test_opening_the_pane_prepares_a_first_report_without_the_six_hour_wait(self):
        self.many(10)
        self.cli("summary-input", "written")                                           # an attempt a moment ago
        self.assertNotIn("written", self.due())
        self.assertNotIn("written", json.loads(self.cli("summary-due", "--open"))["modes"])      # still inside the ten minutes
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            c.execute("UPDATE state SET value = value - 700 WHERE key='summary_attempt_written'"); c.commit()
        self.assertNotIn("written", self.due())                                           # six hours have not passed
        self.assertIn("written", json.loads(self.cli("summary-due", "--open"))["modes"])        # ten minutes have

    def test_the_overview_says_where_the_report_stands(self):
        self.many(4)
        data = self.overview("written")
        self.assertEqual((data["summary_state"], data["errors_needed"]), ("not_enough", 6))
        self.many(6, start=4)
        self.assertEqual(self.overview("written")["summary_state"], "none")
        self.cli("summary-store", "written", stdin=json.dumps({"reply": json.dumps({"categories": {"preposition": "x"}, "focus": ["a"]})}))
        self.assertEqual(self.overview("written")["summary_state"], "ready")
        self.many(1, start=10)
        self.assertEqual(self.overview("written")["summary_state"], "stale")

    def test_a_first_summary_becomes_due_at_ten_errors_and_is_not_repeated_within_six_hours(self):
        self.many(10)
        self.assertEqual(sorted(self.due()), ["both", "written"])
        self.cli("summary-input", "written")
        self.assertEqual(self.due(), ["both"])                       # written was just attempted
        self.config({"native_language": "German", "auto_summary": False})
        self.assertEqual(self.due(), [])

    def test_a_new_summary_needs_ten_new_errors_after_the_last_one(self):
        self.many(10)
        self.cli("summary-store", "written", stdin=json.dumps({"reply": json.dumps({"categories": {"preposition": "x"}, "focus": ["a"]})}))
        self.assertNotIn("written", self.due())
        self.many(9, start=10)
        self.assertNotIn("written", self.due())
        self.many(1, start=19)
        with closing(sqlite3.connect(self.data / "log.db")) as c:                 # the earlier attempt is long ago
            c.execute("DELETE FROM state WHERE key LIKE 'summary_attempt_%'"); c.commit()
        self.assertIn("written", self.due())

    def test_unusable_replies_are_refused(self):
        self.seed()
        self.assertFalse(json.loads(self.cli("summary-store", "written", stdin=json.dumps({"reply": "no json here"})))["ok"])


if __name__ == "__main__":
    unittest.main()


class Phrases(Workspace):
    """Native-phrase suggestions: the reviewer says what fits, the schedule decides what is shown."""

    def say(self, prompt, phrases, fixes=(), mode="written"):
        reply = json.dumps({"careless": False, "fixes": list(fixes), "did_well": [], "phrases": phrases})
        return json.loads(self.cli("ingest", stdin=json.dumps({"prompt": prompt, "mode": mode, "reply": reply, "context": ""})))

    def filler(self, n=4):
        for i in range(n):
            self.say(f"Please look at the file number {i} again.", [])

    def row(self, pid):
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            return c.execute("SELECT shown, skipped, uses, status FROM phrases WHERE id=?", (pid,)).fetchone()

    CHASE = [{"id": "cut-to-the-chase", "original": "get to the point quickly"}]

    def test_first_fitting_occasion_is_shown(self):
        out = self.say("Let us get to the point quickly and fix the build.", self.CHASE)
        self.assertEqual(out["phrase"]["phrase"], "cut to the chase")
        self.assertEqual(out["phrase"]["original"], "get to the point quickly")
        self.assertIn("example", out["phrase"])
        self.assertEqual(self.row("cut-to-the-chase")[:2], (1, 0))

    def test_a_phrase_is_never_a_finding(self):
        self.say("Let us get to the point quickly and fix the build.", self.CHASE)
        self.assertEqual(self.findings(), [])

    def test_unknown_missing_or_unmatched_suggestions_are_dropped(self):
        for bad in ([{"id": "no-such-phrase", "original": "the build"}],
                    [{"id": "cut-to-the-chase", "original": "words that are not there"}],
                    [{"id": "cut-to-the-chase"}], ["cut-to-the-chase"], "cut-to-the-chase"):
            self.assertIsNone(self.say("Let us get to the point quickly and fix the build.", bad)["phrase"])

    def test_quoted_text_gets_no_suggestion(self):
        out = self.say('He wrote "get to the point quickly" in the ticket and I agree.', self.CHASE)
        self.assertIsNone(out["phrase"])

    def test_a_message_that_already_uses_the_phrase_gets_none_and_counts_a_use(self):
        out = self.say("Let me cut to the chase and fix the build now.", self.CHASE)
        self.assertIsNone(out["phrase"])
        self.assertEqual(self.row("cut-to-the-chase")[2], 1)

    def test_two_uses_retire_it_for_good(self):
        self.say("Let me cut to the chase and fix the build now.", [])
        self.say("Okay, to cut to the chase again: the cache is the problem.", [])
        self.assertEqual(self.row("cut-to-the-chase")[3], "learned")
        self.filler()
        self.assertIsNone(self.say("Let us get to the point quickly and fix the build.", self.CHASE)["phrase"])

    def test_inflected_use_is_recognized(self):
        self.say("He is cutting to the chase, finally, so the plan is clear.", [])
        self.assertEqual(self.row("cut-to-the-chase")[2], 1)

    def test_not_again_right_after_a_suggestion(self):
        self.say("Let us get to the point quickly and fix the build.", self.CHASE)
        again = self.say("We should rule out the network, then fix the build.", [{"id": "rule-out", "original": "rule out"}])
        self.assertIsNone(again["phrase"])
        self.assertEqual(self.row("rule-out")[:2], (0, 0))   # too soon: not shown and not counted as an occasion

    def test_schedule_lets_occasions_pass_before_the_next_showing(self):
        self.say("Let us get to the point quickly and fix the build.", self.CHASE)          # shown 1x
        self.filler()
        self.assertIsNone(self.say("I want to get to the point quickly with this one.", self.CHASE)["phrase"])   # occasion 1 let pass
        self.assertEqual(self.row("cut-to-the-chase")[:2], (1, 1))
        self.filler()
        self.assertIsNotNone(self.say("Again, get to the point quickly please, the plan is fine.", self.CHASE)["phrase"])   # shown 2x
        self.assertEqual(self.row("cut-to-the-chase")[:2], (2, 0))
        for _ in range(3):                                                                    # now three occasions pass first
            self.filler()
            self.assertIsNone(self.say("Please get to the point quickly in the next review.", self.CHASE)["phrase"])
        self.filler()
        self.assertIsNotNone(self.say("Once more, get to the point quickly in this section.", self.CHASE)["phrase"])
        self.assertEqual(self.row("cut-to-the-chase")[0], 3)

    def test_it_retires_after_five_showings(self):
        self.say("Let us get to the point quickly and fix the build.", [])      # creates the schema
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            c.execute("INSERT INTO phrases(id, shown, skipped, uses, status) VALUES('cut-to-the-chase', 4, 7, 0, 'active')")
            c.commit()
        self.filler()
        self.assertIsNotNone(self.say("Let us get to the point quickly and fix the build.", self.CHASE)["phrase"])
        self.assertEqual(self.row("cut-to-the-chase")[3], "retired")
        self.filler()
        self.assertIsNone(self.say("Let us get to the point quickly and fix the build.", self.CHASE)["phrase"])

    def test_no_suggestion_on_words_that_carry_a_correction(self):
        out = self.say("Let us get to the point quickly and fix the build.", self.CHASE,
                       fixes=[self.fix("get to the point quickly", "get quickly to the point")])
        self.assertIsNone(out["phrase"])

    def test_careless_messages_get_none(self):
        reply = json.dumps({"careless": True, "fixes": [], "did_well": [], "phrases": self.CHASE})
        out = json.loads(self.cli("ingest", stdin=json.dumps({"prompt": "ok lets get to the point quickly and fix it", "mode": "written", "reply": reply})))
        self.assertTrue(out["careless"])
        self.assertIsNone(out.get("phrase"))

    def test_spoken_messages_can_get_one(self):
        self.assertIsNotNone(self.say("Let us get to the point quickly and fix the build.", self.CHASE, mode="dictated")["phrase"])

    def test_ignore_last_and_unignore(self):
        self.say("Let us get to the point quickly and fix the build.", self.CHASE)
        self.assertIn("not be suggested again", self.cli("phrases", "ignore", "last"))
        self.filler()
        self.assertIsNone(self.say("Let us get to the point quickly and fix the build.", self.CHASE)["phrase"])
        self.assertIn("be suggested again", self.cli("phrases", "unignore", "cut-to-the-chase"))
        self.assertEqual(self.row("cut-to-the-chase")[3], "active")

    def test_ignore_by_words_and_unknown(self):
        self.assertIn("will not be suggested again", self.cli("phrases", "ignore", "rule out"))
        self.assertEqual(self.row("rule-out")[3], "ignored")
        self.assertIn("No such phrase", self.cli("phrases", "ignore", "no such thing"))

    def test_list(self):
        self.assertIn("No phrase has been suggested", self.cli("phrases"))
        self.say("Let us get to the point quickly and fix the build.", self.CHASE)
        self.assertIn("cut to the chase", self.cli("phrases"))

    def test_prompt_offers_a_few_phrases_and_leaves_out_ones_already_used(self):
        out = json.loads(self.cli("prepare", stdin=json.dumps({"prompt": "Let me cut to the chase and fix the build now, please."})))
        self.assertIn("PHRASE SUGGESTION", out["system"])
        offered = [line for line in out["system"].split("PHRASE SUGGESTION")[1].splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(offered), 8)
        self.assertNotIn("cut-to-the-chase", out["system"])

    def test_the_phrase_file_is_well_formed(self):
        entries = json.loads((SCRIPT.parent.parent / "data" / "phrases.json").read_text())
        self.assertGreaterEqual(len(entries), 30)
        self.assertEqual(len({e["id"] for e in entries}), len(entries))
        import re
        for e in entries:
            self.assertTrue({"id", "phrase", "meaning", "example"} <= set(e))
            self.assertTrue(re.search(r"\b(?:" + e["pattern"] + r")\b", e["example"], re.I) or e["phrase"].split()[0] in e["example"].lower(), e["id"])


class PastedChat(Workspace):
    """A copied chat thread (names and timestamps on their own lines) is not the user's writing: no review at all."""

    THREAD = ("maria\n12 minutes ago\nwe pick the colors, i draw the first sketch\n\n12m ago\n"
              "i sent the file to your shared folder so you can check it later\n\nprint test result\n\nmaria\nnow\n"
              "bought more paper since i will print now 300 pages approx")

    def prepare(self, prompt):
        return json.loads(self.cli("prepare", stdin=json.dumps({"prompt": prompt})))

    def test_a_thread_with_timestamp_lines_is_not_reviewed(self):
        self.assertFalse(self.prepare(self.THREAD)["coach"])

    def test_other_timestamp_shapes(self):
        for stamps in ("Yesterday at 3 PM\nsome text here\n14:32", "2 hours ago\nwords words\n3 days ago", "10:05\nokay then\n10:07"):
            self.assertFalse(self.prepare("name\n" + stamps + "\nmore words to reach the minimum")["coach"], stamps)

    def test_ago_inside_a_sentence_is_ordinary_text(self):
        for text in ("I fixed that bug two days ago but the build still fails on the second step.",
                     "It was merged 20 minutes ago, and then the pipeline ran at 14:32 again without any issue.",
                     "Started 3 hours ago\nand it is still running on the old data, which is strange."):
            self.assertTrue(self.prepare(text)["coach"], text)

    def test_a_single_timestamp_line_does_not_count(self):
        self.assertTrue(self.prepare("Please check the log from\n14:32\nbecause the deploy failed there, I think.")["coach"])


class DiffParts(Workspace):
    """The tip shows the changed words in colour and trims long unchanged stretches."""

    def parts(self, a, b):
        return self.ec.diff_parts(a, b)

    def test_a_one_word_change_in_a_long_fragment_shows_only_neighbours(self):
        p = self.parts("do not cut off by the ground truth chain length", "do not cut off at the ground truth chain length")
        self.assertEqual([t for t, c in p["original"] if c], ["by"])
        self.assertEqual([t for t, c in p["fix"] if c], ["at"])
        self.assertEqual(p["original"][0], ["…", False])
        self.assertIn(["cut off", False], p["original"])

    def test_a_moved_word_is_marked_on_both_sides(self):
        p = self.parts("this is what I requested actually from you", "this is what I actually requested from you")
        self.assertEqual([t for t, c in p["original"] if c], ["actually"])
        self.assertEqual([t for t, c in p["fix"] if c], ["actually"])

    def test_an_inserted_word_is_marked_in_the_fix(self):
        p = self.parts("explain me", "explain to me")
        self.assertEqual([t for t, c in p["fix"] if c], ["to"])

    def test_text_is_rebuilt_without_loss(self):
        a, b = "I am since two hours trying", "I have been trying for two hours"
        p = self.parts(a, b)
        self.assertEqual(" ".join(t for t, _ in p["original"] if t != "…"), a)
        self.assertEqual(" ".join(t for t, _ in p["fix"] if t != "…"), b)

    def test_case_and_punctuation_alone_do_not_count_as_changes(self):
        p = self.parts("Okay, so this fails", "okay so this fails again")
        self.assertEqual([t for t, c in p["original"] if c], [])

    def test_ingest_carries_the_parts(self):
        out = self.ingest("Can you explain me why the build is failing, please?", [self.fix("explain me", "explain to me")])
        self.assertEqual([t for t, c in out["fixes"][0]["parts"]["fix"] if c], ["to"])


class ContextChange(Workspace):
    def test_a_lone_word_swap_keeps_two_words_of_context(self):
        c = self.ec.context_change("we should take the latest version here", "we should use the latest version here")
        self.assertEqual(c, {"wrong": "we should take the latest", "right": "we should use the latest"})

    def test_short_fragments_stay_whole_and_edges_are_respected(self):
        self.assertEqual(self.ec.context_change("take it", "use it"), {"wrong": "take it", "right": "use it"})

    def test_polish_and_slip_lists_carry_the_context(self):
        self.ingest("Honestly we should take the latest version here, I think.", [self.fix("take the latest version", "use the latest version", severity="style")])
        data = json.loads(self.cli("overview", "written"))
        self.assertIn("the latest", data["style_examples"][0]["core"]["wrong"])


class WordOrderLabel(Workspace):
    def test_a_fix_that_changes_the_words_is_not_word_order(self):
        self.ingest("I am not sure if especially the second example fits here.", [self.fix("if especially the", "if the", category="word-order")])
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            self.assertEqual(c.execute("SELECT category FROM findings").fetchone()[0], "phrasing")

    def test_a_pure_reordering_keeps_its_label(self):
        self.ingest("We needed the intervals only already to test the smoothing.", [self.fix("needed the intervals only", "only needed the intervals", category="word-order")])
        with closing(sqlite3.connect(self.data / "log.db")) as c:
            self.assertEqual(c.execute("SELECT category FROM findings").fetchone()[0], "word-order")
