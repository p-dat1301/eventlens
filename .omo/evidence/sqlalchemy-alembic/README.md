# SQLAlchemy and Alembic migration evidence

This directory preserves the local evidence for the RAG database refactor
merged in PR #4. It contains synthetic fixture and test data only; no
production database was adopted.

- `baseline/`: checks, DB suite, browser, and evaluation reports before the
  refactor.
- `parity/`: old, fresh Alembic, and adopted database outputs plus query plans.
- `vector-routes/`: exact and HNSW diagnostic plans on the old and new code.
- `adopt-smoke/` and `image-smoke/`: legacy adoption and built-image runs.
- `review-fix/`: focused reruns after review findings.
- `final/`: final local gates and evaluation reports.

The 61 raw `.log` and `.out` files are preserved byte for byte in
`raw-logs.tar.gz`, with paths relative to this directory. They are archived
because tool-generated logs contain trailing whitespace. The JSON, text plans,
and TSV summaries remain directly viewable in Git.

The full suite now also runs in GitHub Actions. The DB job uploads its full
pytest logs and JUnit files as a `database-evidence-*` artifact on success or
failure. Those CI artifacts are separate from this historical refactor record.
