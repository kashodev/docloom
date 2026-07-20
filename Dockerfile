# docloom container.
#
# NOT required to try docloom. The local-first path — filesystem storage,
# SQLite state, Parquet + DuckDB golden — runs from a plain `pip install docloom`
# with no container and no cloud account.
#
# This image exists for the two things that genuinely benefit from one:
#
#   1. Reproducible rendering. PDF generation drives headless Chromium via
#      Playwright, whose output is sensitive to browser and font versions.
#      Pinning them in an image makes 250k documents byte-reproducible across
#      machines. The Playwright base image ships a matched Chromium.
#
#   2. The Cloud Run deploy artifact (GCP path). The same image runs as the
#      generation Service and the export Job — one build, two entry commands.
#
# Build:  docker build -t docloom .
# Local:  docker run --rm -v "$PWD/out:/data" docloom generate --config ...
# Cloud:  pushed to Artifact Registry, deployed to Cloud Run (see deploy.sh).

FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY pyproject.toml README.md ./
COPY src ./src

# `[gcp]` pulls in the GCS / Firestore / BigQuery SDKs for the cloud path.
# Drop it for a purely local image; add `[aws]` for S3.
RUN pip install --no-cache-dir ".[gcp,anthropic]"

# Ship the invoice pack's templates (they live inside the package).
COPY templates/manifest.yaml ./templates/manifest.yaml

# Chromium is already present in the base image; this just verifies the wiring.
RUN python -c "import docloom.packs; print('packs:', docloom.available_packs())" \
    2>/dev/null || python -c "import docloom.packs, docloom; print(docloom.available_packs())"

ENTRYPOINT ["docloom"]
CMD ["--help"]
