# TODO

Tracked follow-ups that are deliberately deferred, not forgotten.

## Before / soon after first public push
- [ ] **Add a `LICENSE` file.** MIT is the conventional pick for a library like
      this. `pyproject.toml` should then set `license = "MIT"` and a
      `classifiers` entry. No licence file is committed yet, so the repo is
      currently "all rights reserved" by default — add one before promoting it.
- [ ] Review the README framing given the repo generates realistic synthetic
      financial documents: make the testing/eval intent unmistakable up front.

## Backlog
- [ ] Cloud adapters end-to-end verification against emulators
      (fake-gcs-server, Firestore emulator) and a real BigQuery project.
- [ ] Remaining invoice archetypes (~13) from the source corpus.
- [ ] Playwright PDF renderer (running headers, `(cont'd)` markers).
- [ ] Generator pipeline tying storage + state + sink into a run.
- [ ] LLM provider abstraction + weighted mix (phase 5).
- [ ] Catalogue + logo generation.
