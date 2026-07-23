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

# The base image bundles a Chromium build matched to ONE Playwright version, and
# the pip package MUST be that exact version or launch fails with
# "Executable doesn't exist". They are declared together here, from a single arg,
# so they can never drift — an unpinned `playwright>=…` floating to a newer
# release than the base image is what silently broke every rendered document.
# Bump both by bumping this one number (and picking the matching `vX.Y.Z-noble`).
ARG PLAYWRIGHT_VERSION=1.61.0
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble
ARG PLAYWRIGHT_VERSION

# Stream stdout/stderr immediately, so a long job's progress reaches Cloud
# Logging as it happens rather than only when the process exits.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY pyproject.toml README.md ./
COPY src ./src

# Pin Playwright to the base image's version *before* installing the package, so
# `.[gcp,anthropic]` (whose `playwright>=…` is already satisfied) cannot pull a
# newer, browser-mismatched release. `[gcp]` adds the GCS / Firestore / BigQuery
# SDKs for the cloud path; drop it for a purely local image, add `[aws]` for S3.
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
 && pip install --no-cache-dir ".[gcp,anthropic]" \
 && test "$(python -c 'import importlib.metadata as m; print(m.version("playwright"))')" = "${PLAYWRIGHT_VERSION}"

# Ship the invoice pack's templates (they live inside the package).
COPY templates/manifest.yaml ./templates/manifest.yaml

# Smoke-test the wiring that actually breaks in production. `import` alone passed
# happily while rendering was completely broken — the pack registered, but every
# document failed to launch a browser. So this renders ONE real PDF end to end
# (record → HTML → Chromium → PDF bytes) and fails the build if it cannot. It is
# the one assertion standing between a broken image and a green run over zero
# documents.
#
# Written as `python -c` one-liners rather than a heredoc on purpose: Cloud
# Build's daemon has no BuildKit heredoc support, and a `RUN … <<'PY'` block gets
# parsed as Dockerfile instructions (a Python `from x import y` reads as a second
# `FROM`).
RUN python -c "import docloom.packs; print('packs:', docloom.available_packs())"
RUN python -c "import docloom.packs; from docloom.core import get_pack; from docloom.core.pipeline.pdf import PdfRenderer; pack=get_pack('invoice'); rec=pack.default_source().generate('docker-build-check', 0); pdf=PdfRenderer(pack).render(rec).data; assert pdf[:5]==b'%PDF-', 'renderer did not return a PDF'; assert len(pdf) > 1000, f'suspiciously small PDF ({len(pdf)} bytes)'; print(f'render check: {len(pdf)} byte PDF OK')"

ENTRYPOINT ["docloom"]
CMD ["--help"]
