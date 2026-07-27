# LLM catalogue & secrets

The `catalog` step can be **procedural** (key-free, the default) or **LLM-backed**.
The studio makes the LLM path a matter of picking a preset — it never hand-authors
the provider block and never touches a key value.

## Provider-mix presets (`studio/mixes.py`)

A **mix** names which providers write descriptions, in what proportion, which
Secret Manager keys they need, and how a quarantined provider's share is
redistributed. It expands to the exact `catalogue.providers` / `fallback` /
`secrets` block [`deploy.sh`](../operations/deploy-tool.md) runs.

| Mix | Providers | Keys |
|---|---|---|
| `procedural` | — (combinatorial, no LLM) | none |
| `cheap-mix` | deepseek 50 / dashscope 50 | deepseek + dashscope |
| `balanced` | deepseek 40 / dashscope 40 / anthropic 20 | all three |
| `anthropic` | anthropic 100 | anthropic |

Both reasoning models have thinking disabled in-mix (deepseek
`extra_body.thinking.type=disabled`, dashscope `enable_thinking=false`) — otherwise
they burn the budget reasoning and return empty completions. See
[Providers](../core/providers.md) for the mix / fallback / budget mechanics.

The wizard prompts for the **mix** and, for an LLM mix, a **budget** (a hard USD
cap); `--mix` / `--budget` bypass. Procedural never asks for a budget.

## Secret Manager name capture

For an LLM mix on a cloud target, the studio records the mix's secret **names** on
the project (never a value) and prints the `gcloud secrets create` line for any
that are missing:

```
this mix needs 2 Secret Manager secret(s) (names saved; values stay in Secret Manager):
  DEEPSEEK_API_KEY    → deepseek-api-key
  DASHSCOPE_API_KEY   → dashscope-api-key
create any that are missing (once), then the build reads them:
  printf %s "$YOUR_KEY" | gcloud secrets create deepseek-api-key --data-file=- --project=…
```

`deploy.sh catalogue` then verifies each secret exists, grants the service account
access, and injects it at task start. Keys never touch the studio, the config, or
the registry — only their names (**① no secret leak**).

## Re-use { #re-use }

A published catalogue is copied and generated from for months, so a **rebuild costs
money** — the studio never rebuilds silently. Before the catalog step it reads the
version's `manifest.json` (`catalogue_info`, via a local file or `gcloud storage
cat`); if one exists it is **reused by default**:

- interactive: *"catalogue 'v2' already exists (built 2026-07-20 · \$3.41) — rebuild
  (costs money)?"*
- scripted: reuse unless `--rebuild-catalogue` forces a rebuild.

See [Tuning & scaffolding](tuning.md) for the concurrency/tasks knobs on the build.
