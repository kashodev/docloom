#!/usr/bin/env python3
"""Eyeball a built catalogue's coherence.

For each company, prints its business type, its assigned merchant sub-category,
and a sample of its product descriptions. A COHERENT catalogue shows one product
family per company — no company mixing apparel with computer parts. Run it after
`deploy.sh -c run.coherence.yaml catalogue`:

    /path/to/.venv/bin/python verify_coherence.py \
        gs://crawler-rag-data-2026-docloom/catalogues/invoice/v2-test

Reads the parquet via `gcloud storage cp` + pyarrow, so it needs no GCP extra —
just gcloud auth and pyarrow (both already in the project venv).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pyarrow.parquet as pq

URI = (sys.argv[1] if len(sys.argv) > 1
       else "gs://crawler-rag-data-2026-docloom/catalogues/invoice/v2-test").rstrip("/")
PROJECT = "crawler-rag-data-2026"
SAMPLE = 12


def _pull(tmp: str, key: str) -> list[dict]:
    dst = os.path.join(tmp, os.path.basename(key))
    subprocess.run(["gcloud", "storage", "cp", f"{URI}/{key}", dst, "--project", PROJECT],
                   check=True, capture_output=True)
    return pq.read_table(dst).to_pylist()


def main() -> None:
    tmp = tempfile.mkdtemp()
    # A single-task (tasks=1) build writes companies.parquet / products.parquet at
    # the root. If those are missing it is a sharded build — pull shard 0 instead.
    try:
        companies = _pull(tmp, "companies.parquet")
        products = _pull(tmp, "products.parquet")
    except subprocess.CalledProcessError:
        companies = _pull(tmp, "shards/companies-000000.parquet")
        products = _pull(tmp, "shards/products-000000.parquet")

    by_company: dict[str, list[str]] = {}
    for p in products:
        by_company.setdefault(p["company_id"], []).append(p["description"])

    for c in sorted(companies, key=lambda c: c["company_id"]):
        descs = by_company.get(c["company_id"], [])
        cat = c.get("product_category") or "(none)"
        print(f"\n{c['company_id']}  {c['business_type']:<12} family={cat!r}  "
              f"({len(descs)} products)")
        for d in descs[:SAMPLE]:
            print(f"    - {d}")
    print(f"\n{len(companies)} companies. Each block above should read as one "
          "coherent line — no company mixing unrelated categories.")


if __name__ == "__main__":
    main()
