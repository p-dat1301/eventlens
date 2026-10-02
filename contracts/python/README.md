# EventLens Contracts

Shared frozen boundary for Data Collection and downstream consumers.

`EventLens Data Contract & Processing Playbook v3.0` is source of truth for
`event.v1`, including event taxonomy, enum registry, and living-event schema.
`Event.entities` embeds an EventEntity transport projection joining canonical
Entity identity (`entity_type`, `canonical_name`) with EventEntity relation fields.
Canonical Entity and EventEntity remain separate database tables.

## Artifacts

- JSON Schemas: `article.v1`, `article_media.v1`, `article_rejected.v1`,
  JSON-only `article_batch_manifest.v1`, and canonical current-state `event.v1`.
- Arrow schema descriptors: `article.v1`, `article_media.v1`, and
  `article_rejected.v1`. Each descriptor reconstructs an exact PyArrow schema,
  including field order, nested structs, nullability, integer width,
  `list<string>`, and `timestamp[us, UTC]`.
- Semantic vectors: `contracts/semantic_vectors.v1.json` describes valid and
  invalid cross-field examples for bundle, manifest, and event relation rules.

Manifest and event have no Arrow artifacts because their frozen formats are JSON.
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
