# english-coach: notes for Claude

A public Claude Code plugin (mod) that reviews the user's English prompts. Backend: `scripts/english-coach.py`;
mod: `hooks/register.tsx` and `hooks/overview.tsx`. See README.md for the design.

## Privacy: test data must be invented

Everything in this repository is public. Fixtures and examples (`tests/eval-cases.json`, unit tests, README and
docs, commit messages) must be **invented sentences**. Never copy the maintainer's real messages, names of their
projects, colleagues or employers, or anything about how this project was written. A short snippet is fine only
if nobody could tell it from a synthetic example. German (or any language) inside a fixture is fine.

To try a real sentence against the prompt, run it ad hoc with a throwaway script outside the repo (for example
through `run_once` in `tests/eval.py`) and do not add it to any file that is checked in.

Do not read the maintainer's session logs or database into fixtures, and ask before sending their real messages
to a model for anything beyond the review itself.

## Working rules

- Ask before committing or pushing. Small tweaks get no version bump; a version bump goes with a release.
- Back up `~/.local/share/english-coach/log.db` before editing or deleting anything in it.
- A sentence in the review prompt stays only if the eval fails without it. Size- or category-based downgrade
  guards cost recall on real errors; do not add them without eval evidence.
- Nothing may assume the user's native language; use only what the settings say.
- Statistics must stay clean: slips, style items, dismissed tips and phrase suggestions are never counted as errors.

## Checks

```
claude plugin validate .
claude plugin test .
python3 -m unittest discover -s tests -p "test_*.py"
python3 tests/eval.py            # real Haiku calls; run before changing the prompt or the validation
```
