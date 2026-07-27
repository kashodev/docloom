# Projects & provisioning

A **project** is a provisioned environment on a target — a local workspace, or a
GCP project with its bucket and Firestore. The studio remembers them in a registry
so you name a project once and reuse it.

## The registry

One human-readable file, `~/.docsynth/projects.yaml` (or `$DOCSYNTH_HOME`). It is a
**cache** over what each target already knows, so a lost or hand-edited file is
never fatal — provisioning is idempotent and re-discovers. Two hard rules:

- It stores secret **names only, never values** — keys live in Secret Manager
  (**① no secret leak**).
- Adding a project that already exists **adopts** it rather than erroring,
  mirroring idempotent provisioning.

Each entry records the target, id, region/bucket (or root), what provisioning
created, the secret names present, and the last run.

## Getting a project

- **Provision a new one.** First use of a new `--project` on a target stands it up:
  `local` makes a workspace dir; `gcp` runs `deploy.sh provision + build` (APIs,
  bucket, Firestore + claim index, service account/IAM, image — a few minutes).
- **Onboard an existing one** (`--onboard`). Registers an already-set-up cloud
  project with its real region/bucket **without creating anything** — for a project
  you (or a prior `deploy.sh provision`) already stood up. Cloud only; a local
  workspace has nothing remote to adopt.
- **Pick a saved one.** Interactively, the project screen lists saved projects (the
  default starred) plus "provision new" / "onboard existing".

## Subcommands

`studio` is a command group; alongside [`status`](dispatch.md):

- **`docsynth studio projects`** — list saved projects.
  ```
  gcp    crawler-rag-data-2026   us-central1 · gs://…-docsynth  provisioned 2026-07-20 · last run adhoc-5k *
  local  corpus-v2               ./corpus-v2  created 2026-07-24
  ```
- **`docsynth studio provision --project <ref>`** — (re)provision the selected
  project; idempotent, safe to re-run.
- **`docsynth studio teardown --project <ref> [--delete-data] [--yes]`** — delete a
  project's resources and forget it. **Keeps data unless `--delete-data`** (then the
  bucket / local workspace goes too). Destructive → requires confirmation
  (interactive prompt or `--yes`); cloud teardown deletes the Cloud Run job(s),
  leaving Firestore and the service account in place. It drives `deploy.sh teardown`
  non-interactively (`ASSUME_YES` / `TEARDOWN_BUCKET`).

Once a project is saved, later commands (`pdfs`, `export`, `status`) find it by
`--project <ref>` (a `<target>:<id>` ref, a bare id, or the default).
