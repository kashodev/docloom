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

# ── Document slices ─────────────────────────────────────────────────────────
# Vocabularies are validated so a typo fails here rather than 40 minutes into a
# run. Sources: docloom.core.locale.enums.Locale, core.enums.DocumentCondition,
# and packs/invoice/templates/archetypes/.
LOCALES = {"en-US", "en-CA", "en-GB", "fr-CA", "fr-FR"}
CONDITIONS = {"clean", "light_scan", "heavy_scan", "handwritten"}
ARCHETYPES = {
    "banner-header-06", "boxed-form-01", "fullbleed-05", "handwritten-form-01",
    "meta-sidebar-01", "receipt-compact-01", "telecom-itemized-37",
}

slices = get("documents") or [{"pack": "invoice", "share": 100}]
total = int(get("run.total", 0) or 0)

sized_by_count = [s for s in slices if s.get("count") is not None]
sized_by_share = [s for s in slices if s.get("share") is not None]
if sized_by_count and sized_by_share:
    sys.exit("size slices with `count` or `share`, not a mix of both")
if not sized_by_count and not sized_by_share:
    sys.exit("every slice needs a `count` or a `share`")

if sized_by_count:
    counts = [int(s["count"]) for s in slices]
    if any(c <= 0 for c in counts):
        sys.exit("`count` must be positive")
    summed = sum(counts)
    if total and total != summed:
        sys.exit(f"slice counts total {summed} but run.total is {total}; drop one or align them")
    total = summed
else:
    shares = sum(float(s.get("share", 0)) for s in slices)
    if abs(shares - 100.0) > 1e-6:
        sys.exit(f"slice shares must total 100, got {shares:g}")
    if total <= 0:
        sys.exit("`run.total` is required when slices are sized by `share`")
    # Rounding remainder to the last slice, so the parts sum to exactly
    # run.total — a silently short run is worse than an uneven split.
    counts, assigned = [], 0
    for i, s in enumerate(slices):
        c = total - assigned if i == len(slices) - 1 else int(round(total * float(s["share"]) / 100.0))
        counts.append(c)
        assigned += c if i < len(slices) - 1 else 0

def as_list(value, field, allowed=None):
    """A slice constraint is a list, a scalar, or an int meaning 'use N of them'."""
    if value is None:
        return "", ""                      # unconstrained
    if isinstance(value, bool):
        sys.exit(f"`{field}` takes names or a count, not a boolean")
    if isinstance(value, int):
        return "", str(value)              # "use N"
    if isinstance(value, str):
        if value == "all":
            return "all", ""
        value = [value]
    names = [str(v) for v in value]
    if allowed:
        bad = sorted(set(names) - allowed)
        if bad:
            sys.exit(f"unknown {field}: {', '.join(bad)} (known: {', '.join(sorted(allowed))})")
    return ",".join(names), ""

emit("TOTAL", total)
rows = []
for i, (s, count) in enumerate(zip(slices, counts), start=1):
    name = str(s.get("name") or f"slice-{i}")
    if "/" in name or " " in name:
        sys.exit(f"slice name {name!r} must not contain spaces or slashes — it becomes a run id")
    pack = s.get("pack", "invoice")
    fmt = s.get("format", get("run.format", "pdf"))
    locales, _ = as_list(s.get("locales"), "locales", LOCALES)
    companies, company_n = as_list(s.get("companies"), "companies")
    archetypes, archetype_n = as_list(s.get("archetypes"), "archetypes", ARCHETYPES)
    condition = s.get("condition") or ""
    if condition and condition not in CONDITIONS:
        sys.exit(f"unknown condition {condition!r} (known: {', '.join(sorted(CONDITIONS))})")
    goods = "yes" if s.get("goods_receipt") else ""
    if goods and condition and condition != "handwritten":
        sys.exit(f"slice {name!r}: goods_receipt is a handwritten delivery note, "
                 f"so condition cannot be {condition!r}")
    # \x1f (ASCII Unit Separator), NOT tab: `read` collapses runs of *whitespace*
    # delimiters, so an omitted field would silently shift every later field left.
    rows.append("\x1f".join([name, pack, str(count), fmt, locales, companies,
                           company_n, archetypes, archetype_n, condition, goods]))
emit("SLICES", "\n".join(rows))
emit("SLICE_COUNT", len(rows))

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

# Each slice is its own run: one pack per run plan, and separate ids keep slices
# independently resumable, exportable and queryable (run_id is on every row).
run_id_for() {
  [[ "${SLICE_COUNT}" -eq 1 ]] && echo "${RUN_ID}" || echo "${RUN_ID}-$1"
}

# Feed the slice table to a `while read` loop. Fields are separated by \x1f
# rather than a tab: `read` treats whitespace delimiters as collapsible, so an
# empty composition field would shift every later field left.
each_slice() { printf '%s\n' "${SLICES}"; }

gen_args() {  # name, pack, count, format, [extra flag]
  local name="$1" pack="$2" count="$3" fmt="$4" extra="${5:-}"
  local args="^|^generate|--run-id=$(run_id_for "${name}")|--total=${count}"
  args+="|--pack=${pack}|--unit-size=${UNIT_SIZE}|--format=${fmt}"
  # config_id records which slice produced a run. It is on the Run, not on the
  # golden rows — filter documents by run_id, not by this.
  args+="|--config-id=${name}|--max-line-items=${MAX_LINE_ITEMS}"
  args+="|--storage=${STORAGE_URI}|--state=${STATE_URI}|--llm-usage=${LLM_USAGE}"
  [[ -n "${extra}" ]] && args+="|${extra}"
  echo "${args}"
}

# One line describing a slice's composition, or a note that it has none.
constraints_of() {  # locales, companies, company_n, archetypes, archetype_n, condition, goods
  local out=""
  [[ -n "$1" ]] && out+="locales=$1 "
  [[ -n "$2" ]] && out+="companies=[$2] "
  [[ -n "$3" ]] && out+="companies=${3}× "
  [[ "$4" == "all" ]] && out+="templates=all "
  [[ -n "$4" && "$4" != "all" ]] && out+="templates=[$4] "
  [[ -n "$5" ]] && out+="templates=${5}× "
  [[ -n "$6" ]] && out+="condition=$6 "
  [[ -n "$7" ]] && out+="goods-receipt "
  [[ -z "${out}" ]] && out="unconstrained"
  echo "${out}"
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

  documents       ${TOTAL} total, unit size ${UNIT_SIZE}
EOF
  local units_total=0
  while IFS=$'\x1f' read -r name pack count fmt loc comp comp_n arch arch_n cond goods; do
    [[ -n "${name}" ]] || continue
    local units=$(( (count + UNIT_SIZE - 1) / UNIT_SIZE ))
    units_total=$(( units_total + units ))
    printf '    %-16s %8s %-8s %4s units  run id: %s\n' \
      "${name}" "${count}" "${pack}/${fmt}" "${units}" "$(run_id_for "${name}")"
    printf '                     %s\n' "$(constraints_of "${loc}" "${comp}" "${comp_n}" "${arch}" "${arch_n}" "${cond}" "${goods}")"
  done < <(each_slice)
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
  IFS=$'\x1f' read -r f_name f_pack f_count f_fmt _ < <(each_slice)
  gcloud run jobs deploy "${JOB}" \
    --image="${IMAGE}" --region="${REGION}" --project="${PROJECT}" \
    --service-account="${SA}" \
    --tasks="${TASKS}" --parallelism="${PARALLELISM}" \
    --task-timeout="${TASK_TIMEOUT}" --max-retries="${MAX_RETRIES}" \
    --cpu="${CPU}" --memory="${MEMORY}" \
    --args="$(gen_args "${f_name}" "${f_pack}" "${f_count}" "${f_fmt}")"
}

run_job() {
  while IFS=$'\x1f' read -r name pack count fmt _ _ _ _ _ _ _; do
    [[ -n "${name}" ]] || continue
    say "Executing ${JOB} — slice '${name}': ${count} ${pack} document(s)"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --args="$(gen_args "${name}" "${pack}" "${count}" "${fmt}")"
  done < <(each_slice)
}

resume() {
  while IFS=$'\x1f' read -r name pack count fmt _ _ _ _ _ _ _; do
    [[ -n "${name}" ]] || continue
    say "Resuming '$(run_id_for "${name}")' — re-queue failed, reclaim crashed"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --args="$(gen_args "${name}" "${pack}" "${count}" "${fmt}" "--resume")"
  done < <(each_slice)
}

status() {
  while IFS=$'\x1f' read -r name _ _ _ _ _ _ _ _ _ _; do
    [[ -n "${name}" ]] || continue
    say "Status — $(run_id_for "${name}")"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --tasks=1 --parallelism=1 \
      --args="^|^status|--run-id=$(run_id_for "${name}")|--state=${STATE_URI}"
  done < <(each_slice)
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
  while IFS=$'\x1f' read -r name _ _ _ _ _ _ _ _ _ _; do
    [[ -n "${name}" ]] || continue
    say "Exporting $(run_id_for "${name}") → ${SINK_URI}"
    # One task: export is a single sequential read of the shards.
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --tasks=1 --parallelism=1 \
      --args="^|^export|--run-id=$(run_id_for "${name}")|--storage=${STORAGE_URI}|--sink=${SINK_URI}"
  done < <(each_slice)
}

teardown() {
  say "Teardown — deletes this run's output. Not reversible."
  echo "  job    : ${JOB}"
  while IFS=$'\x1f' read -r name _ _ _ _ _ _ _ _ _ _; do
    [[ -n "${name}" ]] && echo "  output : gs://${BUCKET}/runs/$(run_id_for "${name}")"
  done < <(each_slice)
  read -r -p "  proceed? [y/N] " reply
  [[ "${reply}" == "y" ]] || { echo "  aborted"; exit 0; }
  gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT}" --quiet || true
  while IFS=$'\x1f' read -r name _ _ _ _ _ _ _ _ _ _; do
    [[ -n "${name}" ]] && gcloud storage rm -r "gs://${BUCKET}/runs/$(run_id_for "${name}")" \
      --project="${PROJECT}" || true
  done < <(each_slice)
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
