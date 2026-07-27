# Quickstart

Install docsynth and generate your first synthetic documents — with a computed
golden dataset — using the [studio](studio/overview.md). This walk-through uses
the **local** target: no cloud account, no API keys.

## Prerequisites

- **Python 3.12+** and **git**.
- **Chromium for PDF rendering.** docsynth renders PDFs through headless Chromium
  (via Playwright); one command installs it below. *Skip it only if you will
  generate `--format html` exclusively.*
- *(Optional, cloud)* the **`gcloud` CLI** and a GCP project — only if you later
  use the `gcp` target. Not needed here.

## Install docsynth

=== "From PyPI (recommended)"

    ```bash
    # 1. Create and activate a virtual environment
    python -m venv .venv
    source .venv/bin/activate          # Windows: .venv\Scripts\activate

    # 2. Install docsynth with the studio wizard extra
    pip install 'docsynth[studio]'

    # 3. Install the headless Chromium the PDF renderer uses
    playwright install chromium
    ```

=== "From source"

    ```bash
    git clone https://github.com/kashodev/docsynth.git
    cd docsynth
    python -m venv .venv
    source .venv/bin/activate          # Windows: .venv\Scripts\activate
    pip install -e '.[studio]'
    playwright install chromium
    ```

Either way you get the **`docsynth`** command:

```bash
docsynth --help
docsynth studio --help
```

!!! tip "Extras"
    - `'.[studio]'` adds arrow-key prompts (`questionary`). Plain
      `pip install -e .` still works — the wizard falls back to numbered menus.
    - `'.[gcp]'` / `'.[aws]'` add the cloud backends (`gs://` / `firestore://` /
      `bigquery://` / `s3://`); `'.[anthropic]'` adds the Claude provider;
      `'.[all]'` is everything. None are needed for this local quickstart.

## Run the studio

`docsynth studio` is an interactive wizard. Launch it and it walks you through
the flow — **target → project → pack → step → confirm** — prompting only for what
you haven't already passed as a flag:

```bash
docsynth studio
```

Choose:

1. **Target → `local`.** Runs on this machine, synchronously; no keys.
2. **Project →** type a new name. The studio creates a local workspace
   (a `blobs/` dir for documents + shards and a `runs.db` for run state) and
   saves it to `~/.docsynth/projects.yaml`.
3. **Pack →** `invoice` (the sole installed pack, so it's auto-selected).
4. **Steps** — the studio runs the three in order:
     - **catalog** — build a product catalogue. Keep it small to start
       (e.g. **5** companies × **10** products); the default mix is `procedural`,
       so no LLM and no keys.
     - **pdfs** — how many documents to render (e.g. **20**), and `pdf` or
       `html`.
     - **export** — write the **golden dataset** (every field value, exact to
       the cent) to DuckDB / Parquet.
5. **Confirm** — it runs locally and prints where everything landed.

Prefer one command? Every prompt has a flag, so the same run is scriptable — see
[Studio → Steps](studio/steps.md) for the fully-flagged (CI) form.

## What you get

- **Rendered documents** in the workspace `blobs/` (the PDFs/HTML).
- **A golden dataset** from the export step — the ground truth for every
  document, which is the point: score an OCR/extraction model against it. See
  [Data model & the golden set](architecture/data-model.md).

## Next steps

- [Studio — Overview](studio/overview.md) — the full wizard, cloud runs, tuning.
- [Architecture — Overview](architecture/overview.md) — kernel ↔ pack, flows.
- [Invoice pack](invoice/overview.md) — what the first pack models.
- [Deploying on GCP](operations/gcp.md) — scale a run to Cloud Run Jobs.
