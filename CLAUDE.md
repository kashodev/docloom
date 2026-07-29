# docsynth — repo guide for Claude / contributors

docsynth generates synthetic documents with a cent-exact **computed golden
dataset**, for testing AI/OCR extraction pipelines. The
[documentation site](https://kashodev.github.io/docsynth/) has the full picture;
this file is the short orientation for working in the repo.

## Working here

- **`main` is protected.** Never commit to it directly — branch, then open a
  pull request. CI must pass before it can merge.
- **Track work as GitHub issues**, not an in-repo file. There is no `TODO.md`;
  open an issue for bugs, deferrals, and follow-ups.
- **Lint is a repo-wide gate.** `ruff check .` must be clean — rule scoping
  lives in `pyproject.toml`, and the CI `ruff` job pins the version. Keep new
  code clean rather than widening ignores.
- **Tests:** run `python -m pytest` (the repo root must be on `sys.path` —
  some tests import via `from tests.… import …`). The four "needs gcp extra"
  tests fail locally when the `gcp` extra is installed and pass on a clean
  runner; that's expected.

## Architecture

The kernel is document-agnostic; a **pack** supplies one document type (invoices
today). The kernel ↔ pack contract is the load-bearing boundary — read it before
adding or changing a pack:

@site-docs/docs/architecture/overview.md
@site-docs/docs/architecture/contract.md

The site also covers the concurrency/coordination model, the golden set, the
studio, and per-platform deployment.

## Publishing

Releases go to PyPI via **Trusted Publishing** (OIDC, no stored token): bump
`[project].version`, tag `vX.Y.Z`, and publish a GitHub Release — the `release`
workflow builds and uploads. Full steps: `site-docs/docs/operations/publishing.md`.

## Docs site

Built from `site-docs/` (mkdocs-material); deploys to GitHub Pages on merge to
`main`. Build locally with `cd site-docs && mkdocs build --strict`.
