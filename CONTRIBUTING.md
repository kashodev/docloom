# Contributing

## Branching

`main` is protected — it does not accept direct pushes. **All work goes on a
branch and lands through a pull request**, however small.

```bash
git switch -c <type>/<short-description>    # branch from an up-to-date main
# ...work, commit...
git push -u origin <branch>
```

Branch names are `<type>/<short-description>`, where type is one of:

| Type | For |
|---|---|
| `feat/` | new capability |
| `fix/` | a defect |
| `refactor/` | restructuring with no behaviour change |
| `docs/` | documentation only |
| `chore/` | tooling, packaging, housekeeping |
| `test/` | tests only |

One PR per logical change. Two unrelated changes on one branch make review
harder and a revert riskier — branch twice instead.

## Pull requests

[`.github/pull_request_template.md`](.github/pull_request_template.md) is applied
automatically. Fill in every section that applies and **delete the ones that do
not** — a section left as an unedited prompt reads as answered when it isn't.

The template asks for the *What*, *Why*, *How* (including trade-offs and
alternatives), *Testing*, *Visuals*, *Reviewer focus*, and *Risk & rollout*. The
two that most often get skipped and matter most:

- **How** — the reasoning, not a restatement of the diff. A reviewer can read the
  code; they cannot read why you rejected the other approach.
- **Reviewer focus** — say what to scrutinise and what to skim. A 900-line diff
  that is 850 lines of moved code deserves that note.

Be honest in the PR body about anything untested, partially done, or knowingly
traded off. It is much cheaper to flag it than to have a reviewer find it.

## Before opening a PR

```bash
PYTHONPATH=src pytest -q          # full suite
```

- New behaviour needs a test that fails without the change.
- Some tests are gated and skip without their dependency (Chromium, the
  Firestore/GCS emulators, boto3/moto, a real BigQuery project). If your change
  touches those paths, run them locally and say so in *Testing*.
- Rendering changes: regenerate the affected files in `samples/` and reference
  them in *Visuals*.

## Things that will fail review

- Committing anything with real personal data. PDFs are gitignored by default;
  the `samples/` exception exists only because that content is wholly synthetic.
- Committing third-party reference material (see `templates/stamps/`).
- Changing a golden value. The record is the ground truth an extraction pipeline
  is scored against — rendering may change freely, the computed figures may not.
- Changing how the RNG is consumed **without saying so in the PR**. Drawing an
  extra value, drawing in a different order, or drawing a different number of
  values shifts every subsequent draw, so every document changes for a given
  `(run_id, index)`. That is sometimes the right thing to do — making line items
  distinct was — but it means a run cannot be resumed across the change, and an
  old run id no longer reproduces its old corpus. Call it out explicitly under
  *Risk & rollout* so a reviewer can weigh it. See
  [docs/concurrency.md](docs/concurrency.md#reproducibility-is-per-code-version-not-absolute).
