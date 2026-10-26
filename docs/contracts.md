# EventLens Shared Contracts

Package `backend.shared.contracts` (`src/backend/shared/contracts`). It is the only
code the bounded contexts share; `ArticleBundle` (`bundle.py`) lives here because
both RAG ingest and the event pipeline consume it.

Shared frozen boundary for Data Collection and downstream consumers.

`EventLens Data Contract & Processing Playbook v3.0` is source of truth for
`event.v1`, including event taxonomy, enum registry, and living-event schema.
`Event.entities` embeds an EventEntity transport projection joining canonical
Entity identity (`entity_type`, `canonical_name`) with EventEntity relation fields.
Canonical Entity and EventEntity remain separate database tables.

## Contracts and Generated Schemas

- Frozen Pydantic models are source of truth. JSON Schemas are generated on demand for
  `article.v1`, `article_media.v1`, `article_rejected.v1`, JSON-only
  `article_batch_manifest.v1`, `event.v1`, and `article_enrichment.v1`.
  RAG-owned models generate `article_document.v1`, `briefing_query.v1`, and
  `briefing_answer.v1` schemas. No generated schema files are checked in.
- Arrow descriptors are generated in memory for `article.v1`, `article_media.v1`,
  and `article_rejected.v1`. Each descriptor reconstructs an exact PyArrow schema,
  including field order, nested structs, nullability, integer width,
  `list<string>`, and `timestamp[us, UTC]`. No descriptor snapshots are checked in.
- Semantic vectors: `src/backend/shared/contracts/semantic_vectors.v1.json` describes valid and
  invalid cross-field examples for bundle, manifest, and event relation rules.

Manifest and event have no Arrow schemas because their frozen formats are JSON.
JSON Schema validates structural constraints such as required fields, types,
ranges, and string lengths. Semantic vectors define rules JSON Schema cannot
enforce, including UTC normalization, media count, media foreign keys, unique and
contiguous media positions, manifest quality totals, EventEntity foreign keys,
and unique event entity-role pairs. Event timestamps and time precision remain
independently optional and omittable; consumers must not infer precision from
timestamp equality. Severity, confidence, and object-valued attributes are also
omittable.
UI does not expose numeric confidence, but `event.v1` carries materialized Event and
EventEntity confidence for downstream consumers. Timeline, delta, fact, claim, and
evidence contracts remain deferred. EventRelation also remains deferred; merged
redirect targets and resolution belong to EventRelation, outside canonical Event
DDL and `event.v1`.
