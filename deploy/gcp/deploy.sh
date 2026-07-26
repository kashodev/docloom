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
#   ./deploy.sh -c run.yaml catalogue   build the LLM content catalogue (one-off)
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
import datetime as _dt
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
# The content catalogue generation draws from. Empty ⇒ the built-in seed
# catalogue (local-first, no artifact). A published artifact URI (the output of a
# `catalogue` build) makes generation use those descriptions; it is read-only
# here, so every task and every slice can share one.
emit("CATALOGUE_URI", get("run.catalogue", ""))

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
# packs/invoice/enums.py BusinessType. Telecom is the one type whose invoices
# carry hundreds of line items, so it is how a slice asks for long documents.
BUSINESS_TYPES = {
    "retail", "ecommerce", "grocery", "wholesale", "auto_repair", "construction",
    "manufacturing", "b2b_saas", "ai_platform", "telecom", "accounting", "legal",
    "consulting", "healthcare", "logistics", "utilities",
}
NAMED_WEAR = {"crisp", "varied", "worn"}
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

def iso_date(value, field):
    """A YAML date (parsed to a date) or a YYYY-MM-DD string, as an ISO string."""
    if isinstance(value, _dt.date):
        return value.isoformat()
    try:
        _dt.date.fromisoformat(str(value))
    except ValueError:
        sys.exit(f"{field}: {value!r} is not a date (use YYYY-MM-DD)")
    return str(value)

def date_range_flags(s, name):
    """Slice-level `date_range: [from, to]`, else run-level `run.date_range`."""
    dr = s.get("date_range", get("run.date_range"))
    if dr is None:
        return []
    if not isinstance(dr, (list, tuple)) or len(dr) != 2:
        sys.exit(f"slice {name!r}: date_range must be [from, to]")
    lo = iso_date(dr[0], f"slice {name!r} date_range from")
    hi = iso_date(dr[1], f"slice {name!r} date_range to")
    if lo > hi:                                    # ISO strings sort chronologically
        sys.exit(f"slice {name!r}: date_range is [from, to]; got [{lo}, {hi}]")
    return [f"--issue-date-from={lo}", f"--issue-date-to={hi}"]

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
    conditions, _ = as_list(s.get("conditions", s.get("condition")), "condition", CONDITIONS)
    business, _ = as_list(s.get("business_types"), "business_types", BUSINESS_TYPES)
    goods = "yes" if s.get("goods_receipt") else ""
    if goods and conditions and conditions != "handwritten":
        sys.exit(f"slice {name!r}: goods_receipt is a handwritten delivery note, "
                 f"so condition cannot be {conditions!r}")
    wear = s.get("wear")
    if isinstance(wear, list):
        wear = ":".join(str(w) for w in wear)
    elif wear is not None:
        wear = str(wear)
        if wear not in NAMED_WEAR and not wear.replace(".", "", 1).isdigit():
            sys.exit(f"unknown wear {wear!r} (known: {', '.join(sorted(NAMED_WEAR))}, "
                     "a number, or a [low, high] range)")

    # The composition is emitted as ready-made `docloom generate` flags rather
    # than as more positional fields. One source of truth for the vocabulary
    # (here), and bash never has to thread a dozen optional columns through a
    # `read` loop where one omission shifts everything left.
    flags = []
    flags += [f"--locale={v}" for v in locales.split(",") if v]
    flags += [f"--company={v}" for v in companies.split(",") if v]
    if company_n:
        flags.append(f"--company-count={company_n}")
    flags += [f"--archetype={v}" for v in archetypes.split(",") if v and v != "all"]
    if archetype_n:
        flags.append(f"--archetype-count={archetype_n}")
    flags += [f"--business-type={v}" for v in business.split(",") if v]
    flags += [f"--condition={v}" for v in conditions.split(",") if v]
    if wear:
        flags.append(f"--wear={wear}")
    if goods:
        flags.append("--goods-receipt")
    flags += date_range_flags(s, name)
    if not s.get("enforce_date_era", get("run.enforce_date_era", True)):
        flags.append("--no-date-era-floor")     # allow deliberately anachronistic dates

    description = " ".join(flags).replace("--", "") or "unconstrained"
    # \x1f (ASCII Unit Separator), NOT tab: `read` collapses runs of *whitespace*
    # delimiters, so an omitted field would silently shift every later field left.
    rows.append("\x1f".join([name, pack, str(count), fmt, "|".join(flags), description]))
emit("SLICES", "\n".join(rows))
emit("SLICE_COUNT", len(rows))

# ── Catalogue provider mix ──────────────────────────────────────────────────
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

# ── Catalogue fallback pool ─────────────────────────────────────────────────
# How a quarantined provider's share is redistributed: entries name a provider
# in the mix or the literal `procedural` (the free sink), and their shares total
# 100. Omitted ⇒ procedural. Validated here so a typo fails at plan, not 20
# minutes into a paid build.
fallback = get("catalogue.fallback", []) or []
if fallback:
    provider_names = {p["name"] for p in providers}
    for f in fallback:
        if "name" not in f or "share" not in f:
            sys.exit(f"each `catalogue.fallback` entry needs a name and a share, got {f}")
        if f["name"] != "procedural" and f["name"] not in provider_names:
            sys.exit(f"fallback name {f['name']!r} is neither 'procedural' nor a "
                     f"catalogue.providers name ({', '.join(sorted(provider_names))})")
    fshare = sum(float(f.get("share", 0)) for f in fallback)
    if abs(fshare - 100.0) > 1e-6:
        sys.exit(f"`catalogue.fallback` shares must total 100, got {fshare:g}")
    emit("FALLBACK_POOL", " ".join(f"{f['name']}:{f['share']}" for f in fallback))
else:
    emit("FALLBACK_POOL", "")

# Catalogue build parameters + the full provider mix as YAML. The mix (with each
# provider's extra_body, e.g. enable_thinking) travels to the container in the
# DOCLOOM_PROVIDERS env var — it is configuration, not secret; the API keys go
# through Secret Manager separately.
emit("CAT_OUT", get("catalogue.out", ""))
emit("CAT_VERSION", get("catalogue.version", "v1"))
emit("CAT_COMPANIES", int(get("catalogue.companies", 1000)))
emit("CAT_PRODUCTS", int(get("catalogue.products_per_company", 300)))
emit("CAT_CONCURRENCY", int(get("catalogue.concurrency", 8)))
# A build over more than one task is a *sharded* build: it is a run over company
# ranges, coordinated through the same StateStore and work-unit claim as
# generation (so it reuses the composite index provision already creates). One
# task keeps the single-process in-memory build — the safe, unchanged default.
emit("CAT_TASKS", int(get("catalogue.tasks", 1)))
emit("CAT_PARALLELISM", int(get("catalogue.parallelism", get("catalogue.tasks", 1))))
emit("CAT_UNIT_SIZE", int(get("catalogue.unit_size", 200)))
emit("CAT_BUILD_ID", get("catalogue.build_id", ""))
# Compact single-line JSON (valid YAML, so the CLI parses it unchanged) rather
# than multi-line YAML: an env var value must survive gcloud's flag parsing, and
# a newline or a stray comma in a YAML block does not travel cleanly.
import json as _json
_mix_json = {"providers": providers}
if fallback:
    _mix_json["fallback"] = fallback
emit("CAT_PROVIDERS_JSON", _json.dumps(_mix_json) if providers else "")
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

# A freshly created service account is not immediately visible to the IAM
# binding APIs — `add-iam-policy-binding` fails with "Service account … does not
# exist" for a few seconds after `create` returns success. Eventual consistency
# across Google's IAM replicas, not a bug in the create.
#
# It only bites on a first-ever provision, which is exactly when nobody is
# watching for it and the script looks broken. Re-running would work, so this
# waits rather than failing and telling the operator to try again.
await_service_account() {
  local attempt
  for attempt in $(seq 1 30); do
    if have iam service-accounts describe "${SA}" --project="${PROJECT}"; then
      # Visible to `describe` is necessary but not sufficient — give the binding
      # replicas a moment behind it.
      sleep 5
      return 0
    fi
    sleep 2
  done
  echo "  service account ${SA} did not become visible after ~60s; re-run provision" >&2
  return 1
}

# Each slice is its own run: one pack per run plan, and separate ids keep slices
# independently resumable, exportable and queryable (run_id is on every row).
run_id_for() {
  [[ "${SLICE_COUNT}" -eq 1 ]] && echo "${RUN_ID}" || echo "${RUN_ID}-$1"
}

# Storage sub-path for a slice. A single-slice run stays flat (empty → the CLI
# defaults it to the run id). A multi-slice run nests every slice under one
# parent folder, `<run.id>/<slice>/…`, instead of sibling `<run.id>-<slice>/`
# folders — so one logical run is one directory. The run id (above) stays flat
# and unique, so state, resume, export and the golden run_id column are unchanged.
prefix_for() {
  [[ "${SLICE_COUNT}" -eq 1 ]] && echo "" || echo "${RUN_ID}/$1"
}

# The storage path a slice's blobs actually live under: nested for multi-slice,
# the flat run id otherwise. Used for the teardown/output paths.
path_for() {
  local p; p="$(prefix_for "$1")"; [[ -n "${p}" ]] && echo "${p}" || run_id_for "$1"
}

# Feed the slice table to a `while read` loop. Fields are separated by \x1f
# rather than a tab: `read` treats whitespace delimiters as collapsible, so an
# empty composition field would shift every later field left.
each_slice() { printf '%s\n' "${SLICES}"; }

gen_args() {  # name, pack, count, format, composition flags, [extra flag]
  local name="$1" pack="$2" count="$3" fmt="$4" comp="${5:-}" extra="${6:-}"
  local args="^|^generate|--run-id=$(run_id_for "${name}")|--total=${count}"
  args+="|--pack=${pack}|--unit-size=${UNIT_SIZE}|--format=${fmt}"
  # config_id records which slice produced a run. It is on the Run, not on the
  # golden rows — filter documents by run_id, not by this.
  args+="|--config-id=${name}|--max-line-items=${MAX_LINE_ITEMS}"
  args+="|--storage=${STORAGE_URI}|--state=${STATE_URI}|--llm-usage=${LLM_USAGE}"
  local prefix; prefix="$(prefix_for "${name}")"
  [[ -n "${prefix}" ]] && args+="|--storage-prefix=${prefix}"
  # Draw descriptions from a published catalogue when one is configured; omitted,
  # the CLI falls back to the seed catalogue exactly as before.
  [[ -n "${CATALOGUE_URI}" ]] && args+="|--catalogue=${CATALOGUE_URI}"
  # The slice's composition, already formatted by the config parser. An
  # unconstrained slice contributes nothing, so it runs exactly as before.
  [[ -n "${comp}" ]] && args+="|${comp}"
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

  documents       ${TOTAL} total, unit size ${UNIT_SIZE}
  catalogue       ${CATALOGUE_URI:-<seed (built-in)>}
EOF
  local units_total=0
  while IFS=$'\x1f' read -r name pack count fmt _ desc; do
    [[ -n "${name}" ]] || continue
    local units=$(( (count + UNIT_SIZE - 1) / UNIT_SIZE ))
    units_total=$(( units_total + units ))
    printf '    %-16s %8s %-8s %4s units  run id: %s\n' \
      "${name}" "${count}" "${pack}/${fmt}" "${units}" "$(run_id_for "${name}")"
    printf '                     %s\n' "${desc}"
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
    if (( CAT_TASKS > 1 )); then
      local cat_units=$(( (CAT_COMPANIES + CAT_UNIT_SIZE - 1) / CAT_UNIT_SIZE ))
      echo "  catalogue (budget \$${BUDGET_USD}): ${CAT_COMPANIES} companies, sharded —"
      echo "    ${cat_units} unit(s) of ${CAT_UNIT_SIZE} across ${CAT_TASKS} task(s), resumable"
    else
      echo "  catalogue (budget \$${BUDGET_USD}): ${CAT_COMPANIES} companies, single-task in-memory build"
    fi
    echo "  provider mix:"
    for p in ${PROVIDER_MIX}; do
      IFS=: read -r name model weight <<<"${p}"
      printf '    %-12s %-22s %s%%\n' "${name}" "${model}" "${weight}"
    done
    if [[ -n "${FALLBACK_POOL}" ]]; then
      echo "  fallback (a quarantined provider's share →):"
      for f in ${FALLBACK_POOL}; do
        IFS=: read -r name share <<<"${f}"
        printf '    %-12s %s%%\n' "${name}" "${share}"
      done
    else
      echo "  fallback: none → a quarantined provider's share goes to procedural"
    fi
    echo "    build once with:  docloom catalogue --providers <this catalogue block>"
    echo "      --out gs://…/catalogues/invoice/v1 --version v1"
    echo "    then generation draws from it: docloom generate --catalogue gs://…/v1"
    echo "    Document generation itself still makes no LLM calls."
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
    await_service_account
  fi

  say "Firestore composite index for the unit claim"
  # The claim is `where(state == 'pending').order_by('unit_index').limit(1)`.
  # An equality filter *plus* an order-by on a different field needs a composite
  # index; Firestore rejects the query outright without one:
  #
  #   FailedPrecondition: 400 The query requires an index.
  #
  # This is the single most important query in the system — no claim, no run —
  # and it is invisible in testing because the Firestore emulator creates
  # indexes on demand instead of demanding them. Only a real database fails.
  #
  # Index builds are asynchronous and take a few minutes on an empty collection,
  # so this runs at provision time rather than being discovered by the first
  # worker. `create` fails when an equivalent index already exists, which is the
  # idempotent case, so that failure is swallowed rather than treated as an error.
  if gcloud firestore indexes composite create \
      --collection-group=work_units --query-scope=COLLECTION \
      --field-config=field-path=state,order=ascending \
      --field-config=field-path=unit_index,order=ascending \
      --database="${FIRESTORE_DB}" --project="${PROJECT}" 2>/dev/null; then
    echo "  created (state ASC, unit_index ASC on work_units)"
  else
    echo "  already present"
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
  IFS=$'\x1f' read -r f_name f_pack f_count f_fmt f_comp _ < <(each_slice)
  gcloud run jobs deploy "${JOB}" \
    --image="${IMAGE}" --region="${REGION}" --project="${PROJECT}" \
    --service-account="${SA}" \
    --tasks="${TASKS}" --parallelism="${PARALLELISM}" \
    --task-timeout="${TASK_TIMEOUT}" --max-retries="${MAX_RETRIES}" \
    --cpu="${CPU}" --memory="${MEMORY}" \
    --args="$(gen_args "${f_name}" "${f_pack}" "${f_count}" "${f_fmt}" "${f_comp}")"
}

run_job() {
  while IFS=$'\x1f' read -r name pack count fmt comp _; do
    [[ -n "${name}" ]] || continue
    say "Executing ${JOB} — slice '${name}': ${count} ${pack} document(s)"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --args="$(gen_args "${name}" "${pack}" "${count}" "${fmt}" "${comp}")"
  done < <(each_slice)
}

resume() {
  while IFS=$'\x1f' read -r name pack count fmt comp _; do
    [[ -n "${name}" ]] || continue
    say "Resuming '$(run_id_for "${name}")' — re-queue failed, reclaim crashed"
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --args="$(gen_args "${name}" "${pack}" "${count}" "${fmt}" "${comp}" "--resume")"
  done < <(each_slice)
}

status() {
  while IFS=$'\x1f' read -r name _; do
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
  while IFS=$'\x1f' read -r name _; do
    [[ -n "${name}" ]] || continue
    say "Exporting $(run_id_for "${name}") → ${SINK_URI}"
    local prefix_arg=""; p="$(prefix_for "${name}")"; [[ -n "${p}" ]] && prefix_arg="|--storage-prefix=${p}"
    # One task: export is a single sequential read of the shards.
    gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" \
      --wait --tasks=1 --parallelism=1 \
      --args="^|^export|--run-id=$(run_id_for "${name}")|--storage=${STORAGE_URI}|--sink=${SINK_URI}${prefix_arg}"
  done < <(each_slice)
}

teardown() {
  say "Teardown — deletes this run's output. Not reversible."
  echo "  job    : ${JOB}"
  while IFS=$'\x1f' read -r name _; do
    [[ -n "${name}" ]] && echo "  output : gs://${BUCKET}/runs/$(path_for "${name}")"
  done < <(each_slice)
  read -r -p "  proceed? [y/N] " reply
  [[ "${reply}" == "y" ]] || { echo "  aborted"; exit 0; }
  gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT}" --quiet || true
  while IFS=$'\x1f' read -r name _; do
    [[ -n "${name}" ]] && gcloud storage rm -r "gs://${BUCKET}/runs/$(path_for "${name}")" \
      --project="${PROJECT}" || true
  done < <(each_slice)
  echo "  bucket, Firestore database and service account left in place"
}

build_catalogue() {
  [[ -n "${CAT_PROVIDERS_JSON}" ]] || {
    echo "no catalogue.providers in ${CONFIG} — nothing to build" >&2; exit 1; }
  [[ -n "${CAT_OUT}" ]] || { echo "set catalogue.out in ${CONFIG}" >&2; exit 1; }

  # Secrets must exist and be readable, or the build fails one call in, having
  # spent nothing but the operator's patience.
  local secret_args=() missing=0
  for pair in ${SECRET_MAP}; do
    local env_name="${pair%%=*}" secret="${pair#*=}"
    if have secrets describe "${secret}" --project="${PROJECT}"; then
      # Grant the accessor here too, so `catalogue` is self-contained after the
      # operator creates the secrets — no need to re-run provision.
      gcloud secrets add-iam-policy-binding "${secret}" \
        --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor \
        --project="${PROJECT}" --quiet >/dev/null
      secret_args+=("--set-secrets=${env_name}=${secret}:latest")
    else
      echo "  secret ${secret} MISSING — create it with your key value:" >&2
      echo "      printf %s \"\$YOUR_KEY\" | gcloud secrets create ${secret} --data-file=- --project=${PROJECT}" >&2
      missing=1
    fi
  done
  [[ "${missing}" -eq 0 ]] || { echo "create the secret(s) above, then re-run" >&2; exit 1; }

  say "Catalogue build → ${CAT_OUT} (${CAT_VERSION})"
  echo "  ${CAT_COMPANIES} companies × ${CAT_PRODUCTS} SKUs, concurrency ${CAT_CONCURRENCY}, budget \$${BUDGET_USD}"

  local cat_job="${JOB}-catalogue"
  local -a cat_args=(
    "catalogue"
    "--out=${CAT_OUT}" "--version=${CAT_VERSION}"
    "--companies=${CAT_COMPANIES}" "--products-per-company=${CAT_PRODUCTS}"
    "--concurrency=${CAT_CONCURRENCY}" "--budget-usd=${BUDGET_USD}"
  )
  local tasks parallelism retries
  if (( CAT_TASKS > 1 )); then
    # A sharded build: a run over company ranges, worked by N tasks against the
    # shared StateStore. The atomic claim splits the units, each task writes its
    # own shards, and whoever finishes last writes the root manifest — the same
    # coordination as generation, so it resumes and cannot double-spend the
    # budget. `--state` is what switches the CLI into this mode.
    echo "  sharded: ${CAT_TASKS} task(s), ${CAT_UNIT_SIZE} companies/shard, resumable via state ${STATE_URI}"
    cat_args+=("--state=${STATE_URI}" "--unit-size=${CAT_UNIT_SIZE}")
    [[ -n "${CAT_BUILD_ID}" ]] && cat_args+=("--build-id=${CAT_BUILD_ID}")
    tasks="${CAT_TASKS}"; parallelism="${CAT_PARALLELISM}"; retries="${MAX_RETRIES}"
  else
    # A single-task job: one process builds the whole catalogue in memory and
    # uploads once. Simplest, and fine up to a few hundred thousand companies.
    cat_args+=("--no-batch")
    tasks=1; parallelism=1; retries=0
  fi

  # The mix rides in DOCLOOM_PROVIDERS (config); the keys are injected from
  # Secret Manager. Args are joined with `|` (no catalogue value contains one),
  # matching the `^|^` delimiter used for the generate job.
  local joined; joined="$(IFS='|'; echo "${cat_args[*]}")"
  gcloud run jobs deploy "${cat_job}" \
    --image="${IMAGE}" --region="${REGION}" --project="${PROJECT}" \
    --service-account="${SA}" \
    --tasks="${tasks}" --parallelism="${parallelism}" --max-retries="${retries}" \
    --task-timeout="${TASK_TIMEOUT}" --cpu="${CPU}" --memory="${MEMORY}" \
    --set-env-vars="^@^DOCLOOM_PROVIDERS=${CAT_PROVIDERS_JSON}" \
    "${secret_args[@]}" \
    --args="^|^${joined}"

  gcloud run jobs execute "${cat_job}" --region="${REGION}" --project="${PROJECT}" --wait
  echo
  echo "  built. Inspect it:  gcloud storage cat ${CAT_OUT}/manifest.json | \${PYTHON} -m json.tool"
  echo "  generate from it:   ./deploy.sh -c ${CONFIG} --set … run   (add --catalogue ${CAT_OUT} to the generate args)"
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
  catalogue) build_catalogue ;;
  teardown)  teardown ;;
  all)       plan; provision; build; deploy_job; run_job ;;
  *)         echo "unknown command: ${COMMAND}" >&2; usage; exit 1 ;;
esac
