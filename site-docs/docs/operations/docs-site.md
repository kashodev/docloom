# Deploying this docs site

This site is a standalone [mkdocs-material](https://squidfunk.github.io/mkdocs-material/)
project under `site-docs/`, with its **own** toolchain (`site-docs/requirements.txt`)
— independent of the docsynth package, buildable and shippable on its own. It is
deployed to **GitHub Pages**.

## Run it locally

```bash
pip install -r site-docs/requirements.txt
mkdocs serve --config-file site-docs/mkdocs.yml    # live reload at 127.0.0.1:8000
mkdocs build --strict --config-file site-docs/mkdocs.yml   # → site-docs/site/ (gitignored)
```

`--strict` fails the build on a broken internal link or a bad Mermaid fence, so CI
catches drift.

## Deploy (GitHub Pages via Actions)

`.github/workflows/docs.yml` runs on push to `main` when `site-docs/**` changes
(or via *Run workflow*), builds the site, and runs `mkdocs gh-deploy --force`,
which pushes the static output to the **`gh-pages`** branch that Pages serves.

**One-time setup:** *Settings → Pages → Deploy from a branch → `gh-pages` / root.*
After that, docs changes ship automatically. The site is at
`https://kashodev.github.io/docsynth/`.

One-shot manual deploy: `mkdocs gh-deploy --config-file site-docs/mkdocs.yml`.

## Writing pages

The full audit and page plan lives in
`feature_explorations/documentation-site.md`. The governing rule: **the code is
the source of truth** — every page is re-derived from the source, and the stale
`DESIGN.md` / `docs/*` are retired rather than copied. Diagrams are first-class
(Mermaid via `pymdownx.superfences`); every flow and structural relationship gets
one.
