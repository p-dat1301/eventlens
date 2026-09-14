# EventLens Contracts

Shared frozen boundary for Data Collection and downstream consumers.

## Artifacts

- JSON Schemas: `article.v1`, `article_media.v1`, `article_rejected.v1`, and
  JSON-only `article_batch_manifest.v1`.
- Arrow schema descriptors: `article.v1`, `article_media.v1`, and
  `article_rejected.v1`. Each descriptor reconstructs an exact PyArrow schema,
  including field order, nested structs, nullability, integer width,
  `list<string>`, and `timestamp[us, UTC]`.
- Semantic vectors: `contracts/semantic_vectors.v1.json` describes valid and
  invalid cross-field examples for bundle and manifest rules.

Manifest has no Arrow artifact because its frozen handoff format is JSON.
JSON Schema validates shape; semantic vectors separately define rules JSON
Schema cannot enforce, including media count, media foreign keys, unique and
contiguous media positions, and manifest quality totals.
