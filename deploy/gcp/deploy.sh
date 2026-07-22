#!/usr/bin/env bash
#
# docloom on GCP — provision, build, deploy and run, via the gcloud CLI.
#
# Every step is idempotent: re-running it converges rather than failing on
# "already exists". That matters because a deploy is usually re-run after
# something in the middle went wrong, and a script that only works on a clean
# project is a script you cannot trust the second time.
#
#   ./deploy.sh provision      enable APIs, bucket, Firestore, service account, IAM
#   ./deploy.sh build          build + push the image (Cloud Build; no local Docker)
#   ./deploy.sh deploy         create/update the Cloud Run job
#   ./deploy.sh run            execute the job and wait
#   ./deploy.sh status         run progress, from Firestore
#   ./deploy.sh logs           tail the execution's logs
#   ./deploy.sh export         export golden shards (one-off job execution)
#   ./deploy.sh all            provision + build + deploy + run
#   ./deploy.sh teardown       delete the job, bucket contents and image
#
# Override anything by exporting it first, e.g.  TOTAL=1000 ./deploy.sh run
#
set -euo pipefail

# ── Configuration ───────────────────────────────────────────────────────────
PROJECT="${PROJECT:-crawler-rag-data-2026}"
REGION="${REGION:-us-central1}"
# Firestore's free tier applies to the (default) database only. nam5 is the US
# multi-region that the free quota has historically been tied to.
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-nam5}"
FIRESTORE_DB="${FIRESTORE_DB:-(default)}"

BUCKET="${BUCKET:-${PROJECT}-docloom}"
AR_REPO="${AR_REPO:-docloom}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/docloom:${IMAGE_TAG}"

JOB="${JOB:-docloom-generate}"
SA_NAME="${SA_NAME:-docloom-run}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# ── The run itself ──────────────────────────────────────────────────────────
RUN_ID="${RUN_ID:-test-25k}"
TOTAL="${TOTAL:-25000}"
# 25000 / 500 = 50 units over 4 tasks ≈ 12 each. Several units per task means a
# slow one cannot leave a task idle at the tail; see docs/concurrency.md.
UNIT_SIZE="${UNIT_SIZE:-500}"
PACK="${PACK:-invoice}"
FORMAT="${FORMAT:-pdf}"

TASKS="${TASKS:-4}"
PARALLELISM="${PARALLELISM:-4}"
# Chromium is the bottleneck and wants real CPU and headroom.
CPU="${CPU:-2}"
MEMORY="${MEMORY:-4Gi}"
# Generous: a task that hits the wall is killed mid-unit, and while the lease
# makes that recoverable, it is wasted work.
TASK_TIMEOUT="${TASK_TIMEOUT:-3600s}"
# Retries are safe: generation is deterministic per (run_id, index) and blob
# writes overwrite in place, so a retried unit reproduces identical output.
MAX_RETRIES="${MAX_RETRIES:-3}"

STORAGE_URI="gs://${BUCKET}/runs"
STATE_URI="firestore://${PROJECT}/${FIRESTORE_DB}"
SINK_URI="${SINK_URI:-}"   # e.g. bigquery://PROJECT/dataset?staging=gs://BUCKET/staging

say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
have() { gcloud "$@" >/dev/null 2>&1; }

# ── Steps ───────────────────────────────────────────────────────────────────
provision() {
  say "Enabling APIs (safe to re-run)"
  gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    firestore.googleapis.com \
    storage.googleapis.com \
    secretmanager.googleapis.com \
    --project="${PROJECT}"

  say "Artifact Registry repo: ${AR_REPO}"
  if have artifacts repositories describe "${AR_REPO}" --location="${REGION}" --project="${PROJECT}"; then
    echo "  exists"
  else
    gcloud artifacts repositories create "${AR_REPO}" \
      --repository-format=docker --location="${REGION}" \
      --description="docloom images" --project="${PROJECT}"
  fi

  say "Bucket: gs://${BUCKET}"
  if have storage buckets describe "gs://${BUCKET}" --project="${PROJECT}"; then
    echo "  exists"
  else
    # Uniform access: IAM only, no per-object ACLs. Simpler to reason about and
    # the only thing the service account needs.
    gcloud storage buckets create "gs://${BUCKET}" \
      --project="${PROJECT}" --location="${REGION}" \
      --uniform-bucket-level-access --default-storage-class=STANDARD
  fi

  say "Firestore database: ${FIRESTORE_DB} (${FIRESTORE_LOCATION}, native mode)"
  if have firestore databases describe --database="${FIRESTORE_DB}" --project="${PROJECT}"; then
    echo "  exists"
  else
    gcloud firestore databases create \
      --database="${FIRESTORE_DB}" \
      --location="${FIRESTORE_LOCATION}" \
      --type=firestore-native \
      --project="${PROJECT}"
  fi

  say "Service account: ${SA}"
  if have iam service-accounts describe "${SA}" --project="${PROJECT}"; then
    echo "  exists"
  else
    gcloud iam service-accounts create "${SA_NAME}" \
      --display-name="docloom Cloud Run job" --project="${PROJECT}"
  fi

  say "IAM — least privilege"
  # Bucket-scoped, not project-wide: the job writes documents and shards to this
  # bucket and has no business touching any other.
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${SA}" --role="roles/storage.objectAdmin" \
    --project="${PROJECT}" --quiet
  # Firestore has no per-database IAM, so this one is project-scoped.
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${SA}" --role="roles/datastore.user" \
    --condition=None --quiet >/dev/null

  echo
  echo "  storage : gs://${BUCKET} (objectAdmin, bucket-scoped)"
  echo "  state   : ${STATE_URI} (datastore.user, project-scoped)"
}

build() {
  say "Building ${IMAGE} with Cloud Build"
  # Cloud Build rather than local docker: no local daemon needed, and the image
  # is built on the same architecture Cloud Run runs (linux/amd64), which a
  # laptop on Apple silicon would otherwise get wrong.
  gcloud builds submit --tag "${IMAGE}" --project="${PROJECT}" .
}

deploy_job() {
  say "Deploying job ${JOB} (${TASKS} tasks, parallelism ${PARALLELISM})"
  # ^|^ is a custom delimiter so argument values may contain commas safely.
  gcloud run jobs deploy "${JOB}" \
    --image="${IMAGE}" \
    --region="${REGION}" \
    --project="${PROJECT}" \
    --service-account="${SA}" \
    --tasks="${TASKS}" \
    --parallelism="${PARALLELISM}" \
    --task-timeout="${TASK_TIMEOUT}" \
    --max-retries="${MAX_RETRIES}" \
    --cpu="${CPU}" \
    --memory="${MEMORY}" \
    --args="^|^generate|--run-id=${RUN_ID}|--total=${TOTAL}|--unit-size=${UNIT_SIZE}|--pack=${PACK}|--format=${FORMAT}|--storage=${STORAGE_URI}|--state=${STATE_URI}"
}

run_job() {
  say "Executing ${JOB} — run '${RUN_ID}', ${TOTAL} documents over ${TASKS} tasks"
  gcloud run jobs execute "${JOB}" \
    --region="${REGION}" --project="${PROJECT}" --wait
}

resume() {
  say "Resuming run '${RUN_ID}' (re-queues failed units, reclaims crashed ones)"
  gcloud run jobs execute "${JOB}" \
    --region="${REGION}" --project="${PROJECT}" --wait \
    --args="^|^generate|--run-id=${RUN_ID}|--total=${TOTAL}|--unit-size=${UNIT_SIZE}|--pack=${PACK}|--format=${FORMAT}|--storage=${STORAGE_URI}|--state=${STATE_URI}|--resume"
}

status() {
  say "Status of run '${RUN_ID}'"
  gcloud run jobs execute "${JOB}" \
    --region="${REGION}" --project="${PROJECT}" --wait \
    --tasks=1 --parallelism=1 \
    --args="^|^status|--run-id=${RUN_ID}|--state=${STATE_URI}"
}

logs() {
  say "Recent logs for ${JOB}"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${JOB}" \
    --project="${PROJECT}" --limit=100 --freshness=1h \
    --format='value(timestamp, textPayload)'
}

export_golden() {
  if [[ -z "${SINK_URI}" ]]; then
    echo "Set SINK_URI first, e.g."
    echo "  SINK_URI='bigquery://${PROJECT}/docloom_golden?staging=gs://${BUCKET}/staging' ./deploy.sh export"
    exit 1
  fi
  say "Exporting run '${RUN_ID}' to ${SINK_URI}"
  # One task: export is a single sequential read of the shards.
  gcloud run jobs execute "${JOB}" \
    --region="${REGION}" --project="${PROJECT}" --wait \
    --tasks=1 --parallelism=1 \
    --args="^|^export|--run-id=${RUN_ID}|--storage=${STORAGE_URI}|--sink=${SINK_URI}"
}

teardown() {
  say "Tearing down (the bucket's contents are deleted — this is not reversible)"
  read -r -p "  delete job '${JOB}' and everything under gs://${BUCKET}/runs? [y/N] " reply
  [[ "${reply}" == "y" ]] || { echo "  aborted"; exit 0; }
  gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT}" --quiet || true
  gcloud storage rm -r "gs://${BUCKET}/runs" --project="${PROJECT}" || true
  echo "  bucket, Firestore database and service account left in place — delete by hand if wanted"
}

case "${1:-}" in
  provision) provision ;;
  build)     build ;;
  deploy)    deploy_job ;;
  run)       run_job ;;
  resume)    resume ;;
  status)    status ;;
  logs)      logs ;;
  export)    export_golden ;;
  teardown)  teardown ;;
  all)       provision; build; deploy_job; run_job ;;
  *)
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
