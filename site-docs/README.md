# docloom docs site

The standalone documentation site for docloom, built with
[mkdocs-material](https://squidfunk.github.io/mkdocs-material/) and deployed to
**GitHub Pages**. It is independent of the docloom package — its own toolchain
(`requirements.txt`), buildable and shippable on its own.

## Run it locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r site-docs/requirements.txt
mkdocs serve --config-file site-docs/mkdocs.yml   # live-reload at http://127.0.0.1:8000
```

Build the static site (into `site-docs/site/`, gitignored):

```bash
mkdocs build --strict --config-file site-docs/mkdocs.yml
```

## Deploy

Pushes to `main` that touch `site-docs/**` trigger
[`.github/workflows/docs.yml`](../.github/workflows/docs.yml), which runs
`mkdocs gh-deploy` to build and push the static site to the **`gh-pages`** branch
that GitHub Pages serves.

**One-time setup:** Settings → Pages → *Deploy from a branch* → `gh-pages` / `/`
(root). After that every docs change ships automatically.

One-shot manual deploy: `mkdocs gh-deploy --config-file site-docs/mkdocs.yml`.

## Status

This is the **scaffold** (build-order item 1): the nav skeleton, theme, and an
empty-but-live site. Most pages are stubs; content is written section by section
per the build plan in `feature_explorations/documentation-site.md`.
