#!/usr/bin/env bash
#
# docloom on GCP — provision, build, deploy and run, driven by a config file.
#
# The same script and the same config shape run a 1,000-document smoke test and a
# multi-million-document production run; only the numbers differ. Nothing about
# "test" is baked in.
#
#   ./deploy.sh -c run.yaml plan        show the plan; touch nothing
#   ./deploy.sh -c run.yaml provision   APIs, bucket, Firestore, service account, IAM
#   ./deploy.sh -c run.yaml build       build + push the image (Cloud Build)
#   ./deploy.sh -c run.yaml deploy      create/update the Cloud Run job
#   ./deploy.sh -c run.yaml run         execute — one execution per document type
#   ./deploy.sh -c run.yaml resume      re-queue failed units, reclaim crashed ones
#   ./deploy.sh -c run.yaml status      run progress, from Firestore
#   ./deploy.sh -c run.yaml logs        recent job logs
#   ./deploy.sh -c run.yaml export      export golden shards to the configured sink
#   ./deploy.sh -c run.yaml all         provision + build + deploy + run
#   ./deploy.sh -c run.yaml teardown    delete the job and this run's output
#
# Override any value without editing the file (repeatable):
#
#   ./deploy.sh -c run.yaml --set run.total=1000 --set job.tasks=2 run
#
# Every step is idempotent: re-running converges rather than failing on
# "already exists". A deploy is usually re-run after something midway went
# wrong, and a script that only works against a clean project is one you cannot
# trust the second time.
#
set -euo pipefail

# Any python3 with PyYAML. Override if your default lacks it.
PYTHON="${PYTHON:-python3}"

CONFIG=""
declare -a OVERRIDES=()
COMMAND=""

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2 ;;
    --set)       OVERRIDES+=("$2"); shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    -*)          echo "unknown flag: $1" >&2; usage; exit 1 ;;
    *)           COMMAND="$1"; shift ;;
  esac
done

[[ -n "${COMMAND}" ]] || { usage; exit 1; }
if [[ -z "${CONFIG}" ]]; then
  CONFIG="$(dirname "$0")/run.example.yaml"
  echo "no --config given; using ${CONFIG}" >&2
fi
[[ -f "${CONFIG}" ]] || { echo "config not found: ${CONFIG}" >&2; exit 1; }

# ── Load the config ─────────────────────────────────────────────────────────
# One python pass emits shell assignments, so the file is parsed once and every
# value is shell-quoted rather than re-split by the shell.
load_config() {
  local overrides_joined=""
  [[ ${#OVERRIDES[@]} -gt 0 ]] && overrides_joined="$(printf '%s\n' "${OVERRIDES[@]}")"
  "${PYTHON}" - "${CONFIG}" "${overrides_joined}" <<'PYEOF'
import shlex, sys

try:
    import yaml
except ImportError:
    sys.exit("this script parses YAML config; install it with: pip install pyyaml")

path, raw_overrides = sys.argv[1], sys.argv[2]
with open(path) as fh:
    cfg = yaml.safe_load(fh) or {}

# --set a.b.c=value, applied before anything is read.
for line in filter(None, raw_overrides.splitlines()):
    if "=" not in line:
        sys.exit(f"--set needs key=value, got {line!r}")
    dotted, value = line.split("=", 1)
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    current = node.get(parts[-1])
    # Keep the config's type so `--set run.total=1000` stays an int.
    if isinstance(current, bool):
        value = value.lower() in ("1", "true", "yes")
    elif isinstance(current, int):
        value = int(value)
    elif isinstance(current, float):
        value = float(value)
    node[parts[-1]] = value


def get(dotted, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def emit(name, value):
    print(f"{name}={shlex.quote(str(value))}")


project = get("project")
if not project:
    sys.exit("config is missing `project`")

emit("PROJECT", project)
emit("REGION", get("region", "us-central1"))
emit("BUCKET", get("bucket", f"{project}-docloom"))
emit("FIRESTORE_DB", get("firestore.database", "(default)"))
emit("FIRESTORE_LOCATION", get("firestore.location", "nam5"))
emit("AR_REPO", get("artifact_registry.repo", "docloom"))
emit("IMAGE_TAG", get("artifact_registry.image_tag", "v1"))

emit("RUN_ID", get("run.id", "run"))
emit("TOTAL", int(get("run.total", 0)))
emit("UNIT_SIZE", int(get("run.unit_size", 1000)))
emit("FORMAT", get("run.format", "pdf"))
emit("CONFIG_ID", get("run.config_id", "default"))
emit("MAX_LINE_ITEMS", int(get("run.max_line_items", 8000)))
emit("LLM_USAGE", get("run.llm_usage", "shard://"))

emit("JOB", get("job.name", "docloom-generate"))
emit("SA_NAME", get("job.service_account", "docloom-run"))
emit("TASKS", int(get("job.tasks", 1)))
emit("PARALLELISM", int(get("job.parallelism", get("job.tasks", 1))))
emit("CPU", get("job.cpu", 2))
emit("MEMORY", get("job.memory", "4Gi"))
emit("TASK_TIMEOUT", get("job.task_timeout", "3600s"))
emit("MAX_RETRIES", int(get("job.max_retries", 3)))
emit("SINK_URI", get("export.sink", ""))

# ── Document mix ────────────────────────────────────────────────────────────
docs = get("documents", []) or []
if not docs:
    sys.exit("config is missing `documents` — at least one {pack, share} entry")
shares = sum(float(d.get("share", 0)) for d in docs)
if abs(shares - 100.0) > 1e-6:
    sys.exit(f"`documents` shares must total 100, got {shares:g}")

total = int(get("run.total", 0))
if total <= 0:
    sys.exit("`run.total` must be a positive number of documents")

# Apportion by share, giving the rounding remainder to the last entry so the
# parts always sum to exactly run.total — a silently short run is worse than an
# uneven split.
parts, assigned = [], 0
for i, d in enumerate(docs):
    pack = d.get("pack")
    if not pack:
        sys.exit("every `documents` entry needs a `pack`")
    if i == len(docs) - 1:
        count = total - assigned
    else:
        count = int(round(total * float(d.get("share", 0)) / 100.0))
        assigned += count
    parts.append(f"{pack}:{count}:{d.get('share', 0)}")
emit("DOC_MIX", " ".join(parts))
emit("DOC_COUNT", len(parts))

# ── Catalogue provider mix (validated, not yet executable) ──────────────────
providers = get("catalogue.providers", []) or []
if providers:
    weights = sum(float(p.get("weight", 0)) for p in providers)
    if abs(weights - 100.0) > 1e-6:
        sys.exit(f"`catalogue.providers` weights must total 100, got {weights:g}")
    emit("PROVIDER_MIX", " ".join(
        f"{p['name']}:{p.get('model', '?')}:{p.get('weight', 0)}" for p in providers
    ))
else:
    emit("PROVIDER_MIX", "")
emit("BUDGET_USD", get("catalogue.budget_usd", ""))
emit("SECRET_MAP", " ".join(
    f"{k}={v}" for k, v in (get("catalogue.secrets", {}) or {}).items()
))
PYEOF
}

# Assign first so a parse failure aborts here (set -e), with the parser's own
# message. `eval "$(load_config)"` would discard the exit status and carry on
# into a confusing "unbound variable" further down.
CONFIG_ENV="$(load_config)"
eval "${CONFIG_ENV}"

SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/docloom:${IMAGE_TAG}"
STORAGE_URI="gs://${BUCKET}/runs"
STATE_URI="firestore://${PROJECT}/${FIRESTORE_DB}"

say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
have() { gcloud "$@" >/dev/null 2>&1; }

# A run holds exactly one pack in its plan, so each document type gets its own
# run id when there is more than one.
run_id_for() {
  [[ "${DOC_COUNT}" -eq 1 ]] && echo "${RUN_ID}" || echo "${RUN_ID}-$1"
}

gen_args() {  # pack, count, [extra flag]
  local pack="$1" count="$2" extra="${3:-}"
  local args="^|^generate|--run-id=$(run_id_for "${pack}")|--total=${count}"
  args+="|--pack=${pack}|--unit-size=${UNIT_SIZE}|--format=${FORMAT}"
  args+="|--config-id=${CONFIG_ID}|--max-line-items=${MAX_LINE_ITEMS}"
  args+="|--storage=${STORAGE_URI}|--state=${STATE_URI}|--llm-usage=${LLM_USAGE}"
  [[ -n "${extra}" ]] && args+="|${extra}"
  echo "${args}"
}

# ── Commands ────────────────────────────────────────────────────────────────
plan() {
  say "Plan — nothing is created or changed"
  cat <<EOF
  config          ${CONFIG}
  project         ${PROJECT}   region ${REGION}
  image           ${IMAGE}
  bucket          gs://${BUCKET}
  state           ${STATE_URI}  (${FIRESTORE_LOCATION})
  service account ${SA}

  documents       ${TOTAL} total, unit size ${UNIT_SIZE}, format ${FORMAT}
EOF
  local units_total=0
  for entry in ${DOC_MIX}; do
    IFS=: read -r pack count share <<<"${entry}"
    local units=$(( (count + UNIT_SIZE - 1) / UNIT_SIZE ))
    units_total=$(( units_total + units ))
    printf '    %-12s %8s docs (%s%%)  → %4s units  run id: %s\n' \
      "${pack}" "${count}" "${share}" "${units}" "$(run_id_for "${pack}")"
  done
  cat <<EOF

  job             ${JOB}: ${TASKS} task(s), parallelism ${PARALLELISM}
                  ${CPU} vCPU / ${MEMORY}, timeout ${TASK_TIMEOUT}, retries ${MAX_RETRIES}
  units per task  ~$(( units_total / (TASKS > 0 ? TASKS : 1) )) of ${units_total}
  telemetry       ${LLM_USAGE}
  export sink     ${SINK_URI:-<disabled>}
EOF
  if (( units_total < TASKS )); then
    echo
    echo "  ⚠ ${units_total} unit(s) for ${TASKS} task(s): some tasks will do nothing."
    echo "    Lower run.unit_size or job.tasks."
  fi
  if [[ -n "${PROVIDER_MIX}" ]]; then
    echo
    echo "  catalogue mix (budget \$${BUDGET_USD}) — validated, NOT executed:"
    for p in ${PROVIDER_MIX}; do
      IFS=: read -r name model weight <<<"${p}"
      printf '    %-12s %-22s %s%%\n' "${name}" "${model}" "${weight}"
    done
    echo "    ⚠ no 'docloom catalogue' command exists yet, so this cannot run."
    echo "      Generation makes no LLM calls; see docs/deploy-gcp.md."
  fi
}

provision() {
  say "Enabling APIs (safe to re-run)"
  gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
    cloudbuild.googleapis.com firestore.googleapis.com storage.googleapis.com \
    secretmanager.googleapis.com --project="${PROJECT}"

  say "Artifact Registry: ${AR_REPO}"
  if have artifacts repositories describe "${AR_REPO}" --location="${REGION}" --project="${PROJECT}"; then
    echo "  exists"
  else
    gcloud artifacts repositories create "${AR_REPO}" --repository-format=docker \
      --location="${REGION}" --description="docloom images" --project="${PROJECT}"
  fi

  say "Bucket: gs://${BUCKET}"
  if have storage buckets describe "gs://${BUCKET}" --project="${PROJECT}"; then
    echo "  exists"
  else
    # Uniform access: IAM only, no per-object ACLs — simpler, and all the job needs.
    gcloud storage buckets create "gs://${BUCKET}" --project="${PROJECT}" \
      --location="${REGION}" --uniform-bucket-level-access \
      --default-storage-class=STANDARD
  fi

  say "Firestore: ${FIRESTORE_DB} (${FIRESTORE_LOCATION}, native mode)"
  if have firestore databases describe --database="${FIRESTORE_DB}" --project="${PROJECT}"; then
    echo "  exists"
  else
    gcloud firestore databases create --database="${FIRESTORE_DB}" \
      --location="${FIRESTORE_LOCATION}" --type=firestore-native --project="${PROJECT}"
  fi

  say "Service account: ${SA}"
  if have iam service-accounts describe "${SA}" --project="${PROJECT}"; then
    echo "  exists"
  else
    gcloud iam service-accounts create "${SA_NAME}" \
      --display-name="docloom Cloud Run job" --project="${PROJECT}"
  fi

  say "IAM — least privilege"
  # Bucket-scoped: the job writes this run's output and has no business elsewhere.
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${SA}" --role="roles/storage.objectAdmin" \
    --project="${PROJECT}" --quiet >/dev/null
  # Firestore has no per-database IAM, so this one has to be project-scoped.
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${SA}" --role="roles/datastore.user" \
    --condition=None --quiet >/dev/null
  echo "  storage.objectAdmin on gs://${BUCKET}; datastore.user on ${PROJECT}"

  if [[ -n "${SECRET_MAP}" ]]; then
    say "Catalogue secrets — granting accessor on each (secrets themselves are not created)"
    for pair in ${SECRET_MAP}; do
      local secret="${pair#*=}"
      if have secrets describe "${secret}" --project="${PROJECT}"; then
        gcloud secrets add-iam-policy-binding "${secret}" \
          --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor \
          --project="${PROJECT}" --quiet >/dev/null
        echo "  ${secret}: accessor granted"
      else
        echo "  ${secret}: MISSING — create it with:"
        echo "      gcloud secrets create ${secret} --replication-policy=automatic --project=${PROJECT}"
        echo "      gcloud secrets versions add ${secret} --data-file=- --project=${PROJECT}"
      fi
    done
  fi
}

build() {
  say "Building ${IMAGE} with Cloud Build"
  # Cloud Build, not local docker: no daemon needed, and it produces linux/amd64
  # — which an Apple-silicon laptop would get wrong, yielding an image Cloud Run
  # cannot start.
  ( cd "$(dirname "$0")/../.." && gcloud builds submit --tag "${IMAGE}" --project="${PROJECT}" . )
}

deploy_job() {
  say "Deploying job ${JOB} (${TASKS} tasks, parallelism ${PARALLELISM})"
  # Args are set per execution, so the job definition carries the first type's
  # only as a sane default.
  IFS=: read -r first_pack first_count _ <<<"${DOC_MIX%% *}"
  gcloud run jobs deploy "${JOB}" \
    --image="${IMAGE}" --region="${REGION}" --project="${PROJECT}" \
    --service-account="${SA}" \
    --tasks="${TASKS}" --parallelism="${PARALLELISM}" \
    --task-timeout="${TASK_TIMEOUT}" --max-retries="${MAX_RETRIES}" \
    --cpu="${CPU}" --memory="${MEMORY}" \
    --args="$(gen_args "${first_pack}" "${first_count}")"
}

run_job() {
  for entry in ${DOC_MIX}; do
    IFS=: read -r pack count _ <<<"${entry}"
    say "Executing ${JOB} — ${count} ${pack} document(s), run '$(run_id_for "${pack}")'"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --args="$(gen_args "${pack}" "${count}")"
  done
}

resume() {
  for entry in ${DOC_MIX}; do
    IFS=: read -r pack count _ <<<"${entry}"
    say "Resuming '$(run_id_for "${pack}")' — re-queue failed, reclaim crashed"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --args="$(gen_args "${pack}" "${count}" "--resume")"
  done
}

status() {
  for entry in ${DOC_MIX}; do
    IFS=: read -r pack _ _ <<<"${entry}"
    say "Status — $(run_id_for "${pack}")"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --tasks=1 --parallelism=1 \
      --args="^|^status|--run-id=$(run_id_for "${pack}")|--state=${STATE_URI}"
  done
}

logs() {
  say "Recent logs for ${JOB}"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${JOB}" \
    --project="${PROJECT}" --limit=100 --freshness=1h \
    --format='value(timestamp, textPayload)'
}

export_golden() {
  [[ -n "${SINK_URI}" ]] || { echo "set export.sink in ${CONFIG} first"; exit 1; }
  for entry in ${DOC_MIX}; do
    IFS=: read -r pack _ _ <<<"${entry}"
    say "Exporting $(run_id_for "${pack}") → ${SINK_URI}"
    # One task: export is a single sequential read of the shards.
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --tasks=1 --parallelism=1 \
      --args="^|^export|--run-id=$(run_id_for "${pack}")|--storage=${STORAGE_URI}|--sink=${SINK_URI}"
  done
}

teardown() {
  say "Teardown — deletes this run's output. Not reversible."
  echo "  job    : ${JOB}"
  for entry in ${DOC_MIX}; do
    IFS=: read -r pack _ _ <<<"${entry}"
    echo "  output : gs://${BUCKET}/runs/$(run_id_for "${pack}")"
  done
  read -r -p "  proceed? [y/N] " reply
  [[ "${reply}" == "y" ]] || { echo "  aborted"; exit 0; }
  gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT}" --quiet || true
  for entry in ${DOC_MIX}; do
    IFS=: read -r pack _ _ <<<"${entry}"
    gcloud storage rm -r "gs://${BUCKET}/runs/$(run_id_for "${pack}")" --project="${PROJECT}" || true
  done
  echo "  bucket, Firestore database and service account left in place"
}

case "${COMMAND}" in
  plan)      plan ;;
  provision) provision ;;
  build)     build ;;
  deploy)    deploy_job ;;
  run)       run_job ;;
  resume)    resume ;;
  status)    status ;;
  logs)      logs ;;
  export)    export_golden ;;
  teardown)  teardown ;;
  all)       plan; provision; build; deploy_job; run_job ;;
  *)         echo "unknown command: ${COMMAND}" >&2; usage; exit 1 ;;
esac
