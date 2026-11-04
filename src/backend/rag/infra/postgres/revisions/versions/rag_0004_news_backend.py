"""News backend: catalog taxonomy, article labels, summaries, jobs, chat.

Revision ID: rag_0004_news_backend
Revises: rag_0003_search_platform
Create Date: 2026-10-29

Additive only; no existing table, view, trigger or row changes. Article
documents, source references and source versions are untouched.

* ``rag_topics``: display names of the frozen ``TopicTag`` codes
  (``topic-taxonomy.v1``). Unclassified is a state, never a row.
* ``rag_locations`` / ``rag_location_aliases``: Vietnamese administrative
  units after the 2025-07-01 reorganisation (34 province-level units) plus the
  29 absorbed former provinces as ``former_province`` children with
  ``valid_until``, so historical names roll up to the current unit
  (``vn-admin-2025.v1``). One alias key maps to exactly one location.
* ``rag_article_labels``: topic/location labels of one exact revision with
  provenance (``enrichment`` publication or ``manual``). Sync never touches
  manual rows; a manual ``rejected`` row suppresses the same model label.
  ``rag_current_labels`` exposes the effective labels of current revisions.
* ``rag_label_syncs``: which publication a revision's labels came from (or
  why none were applied), so "pending" and "unavailable" stay distinct.
* ``rag_work_jobs``: durable PostgreSQL jobs (``summary``, ``query_run``) with
  attempt budget, lease, claim fence and deadline. A trigger on
  ``rag_article_current`` schedules the summary job of every new current
  revision in the same transaction and marks older pending summary jobs of
  that article STALE; existing current revisions are backfilled.
* ``rag_article_summaries``: published AI summaries keyed by revision and
  model/prompt/schema identity (first fenced publication wins).
* ``rag_sessions``, ``rag_conversations``, ``rag_messages``,
  ``rag_query_runs``, ``rag_run_citations``: server-side demo sessions,
  conversations with their scope definition, durable runs (idempotency key,
  one active run per conversation) and citation identity/version/span.

``downgrade`` drops only these additive objects.
"""

import re
import unicodedata
from collections.abc import Sequence

from alembic import op

from backend.rag.infra.postgres.engine import execute

revision: str = "rag_0004_news_backend"
down_revision: str | Sequence[str] | None = "rag_0003_search_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TAXONOMY_VERSION = "topic-taxonomy.v1"
GAZETTEER_VERSION = "vn-admin-2025.v1"
REORGANISED = "2025-07-01"

# (code, Vietnamese name, English name) in display order.
TOPICS: tuple[tuple[str, str, str], ...] = (
    ("politics", "Chính trị", "Politics"),
    ("economy", "Kinh tế", "Economy"),
    ("business", "Kinh doanh", "Business"),
    ("technology", "Công nghệ", "Technology"),
    ("public_safety", "An ninh - trật tự", "Public safety"),
    ("transportation", "Giao thông", "Transportation"),
    ("health", "Sức khỏe", "Health"),
    ("environment", "Môi trường", "Environment"),
    ("energy", "Năng lượng", "Energy"),
    ("weather", "Thời tiết", "Weather"),
    ("other", "Khác", "Other"),
)

# (slug, name, extra aliases) of the six centrally-governed cities.
MUNICIPALITIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ha-noi", "Hà Nội", ("Hanoi", "Ha Noi")),
    ("hue", "Huế", ("Thừa Thiên Huế", "Thừa Thiên - Huế", "Hue")),
    ("hai-phong", "Hải Phòng", ("Haiphong",)),
    ("da-nang", "Đà Nẵng", ("Da Nang", "Danang")),
    (
        "ho-chi-minh",
        "Hồ Chí Minh",
        ("TP.HCM", "TPHCM", "HCM", "Sài Gòn", "Saigon", "Ho Chi Minh City"),
    ),
    ("can-tho", "Cần Thơ", ()),
)

PROVINCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("lai-chau", "Lai Châu", ()),
    ("dien-bien", "Điện Biên", ()),
    ("son-la", "Sơn La", ()),
    ("lang-son", "Lạng Sơn", ()),
    ("cao-bang", "Cao Bằng", ()),
    ("tuyen-quang", "Tuyên Quang", ()),
    ("lao-cai", "Lào Cai", ()),
    ("thai-nguyen", "Thái Nguyên", ()),
    ("phu-tho", "Phú Thọ", ()),
    ("bac-ninh", "Bắc Ninh", ()),
    ("hung-yen", "Hưng Yên", ()),
    ("ninh-binh", "Ninh Bình", ()),
    ("quang-ninh", "Quảng Ninh", ()),
    ("thanh-hoa", "Thanh Hóa", ("Thanh Hoá",)),
    ("nghe-an", "Nghệ An", ()),
    ("ha-tinh", "Hà Tĩnh", ()),
    ("quang-tri", "Quảng Trị", ()),
    ("quang-ngai", "Quảng Ngãi", ()),
    ("gia-lai", "Gia Lai", ()),
    ("khanh-hoa", "Khánh Hòa", ("Khánh Hoà",)),
    ("lam-dong", "Lâm Đồng", ()),
    ("dak-lak", "Đắk Lắk", ("Đắc Lắc", "Daklak")),
    ("dong-nai", "Đồng Nai", ()),
    ("tay-ninh", "Tây Ninh", ()),
    ("vinh-long", "Vĩnh Long", ()),
    ("dong-thap", "Đồng Tháp", ()),
    ("ca-mau", "Cà Mau", ()),
    ("an-giang", "An Giang", ()),
)

# (slug, name, current parent slug, extra aliases): absorbed on 2025-07-01.
FORMER_PROVINCES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("ha-giang", "Hà Giang", "tuyen-quang", ()),
    ("yen-bai", "Yên Bái", "lao-cai", ()),
    ("bac-kan", "Bắc Kạn", "thai-nguyen", ("Bắc Cạn",)),
    ("vinh-phuc", "Vĩnh Phúc", "phu-tho", ()),
    ("hoa-binh", "Hòa Bình", "phu-tho", ("Hoà Bình",)),
    ("bac-giang", "Bắc Giang", "bac-ninh", ()),
    ("thai-binh", "Thái Bình", "hung-yen", ()),
    ("hai-duong", "Hải Dương", "hai-phong", ()),
    ("ha-nam", "Hà Nam", "ninh-binh", ()),
    ("nam-dinh", "Nam Định", "ninh-binh", ()),
    ("quang-binh", "Quảng Bình", "quang-tri", ()),
    ("quang-nam", "Quảng Nam", "da-nang", ()),
    ("kon-tum", "Kon Tum", "quang-ngai", ()),
    ("binh-dinh", "Bình Định", "gia-lai", ()),
    ("ninh-thuan", "Ninh Thuận", "khanh-hoa", ()),
    ("dak-nong", "Đắk Nông", "lam-dong", ()),
    ("binh-thuan", "Bình Thuận", "lam-dong", ()),
    ("phu-yen", "Phú Yên", "dak-lak", ()),
    ("binh-duong", "Bình Dương", "ho-chi-minh", ()),
    (
        "ba-ria-vung-tau",
        "Bà Rịa - Vũng Tàu",
        "ho-chi-minh",
        ("Bà Rịa Vũng Tàu", "BR-VT", "Vũng Tàu"),
    ),
    ("binh-phuoc", "Bình Phước", "dong-nai", ()),
    ("long-an", "Long An", "tay-ninh", ()),
    ("soc-trang", "Sóc Trăng", "can-tho", ()),
    ("hau-giang", "Hậu Giang", "can-tho", ()),
    ("ben-tre", "Bến Tre", "vinh-long", ()),
    ("tra-vinh", "Trà Vinh", "vinh-long", ()),
    ("tien-giang", "Tiền Giang", "dong-thap", ()),
    ("bac-lieu", "Bạc Liêu", "ca-mau", ()),
    ("kien-giang", "Kiên Giang", "an-giang", ()),
)


def alias_key(text: str) -> str:
    """Frozen alias key: casefold, đ→d, strip marks, alphanumeric words.

    ``backend.rag.domain.taxonomy.location_alias_key`` must stay identical.
    """
    folded = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", plain).split())


def _seed_locations() -> tuple[
    list[tuple[str, str, str, str | None, str | None]], list[tuple[str, str]]
]:
    """(id, name, type, parent, valid_until) rows and (id, alias) rows."""
    units: list[tuple[str, str, str, str | None, str | None]] = [
        ("vn", "Việt Nam", "country", None, None)
    ]
    aliases: list[tuple[str, str]] = [
        ("vn", name) for name in ("Việt Nam", "Vietnam", "Viet Nam")
    ]
    for slug, name, extra in MUNICIPALITIES:
        units.append((f"vn-{slug}", f"Thành phố {name}", "municipality", "vn", None))
        prefixed = (f"Thành phố {name}", f"TP {name}", f"TP. {name}")
        aliases.extend((f"vn-{slug}", a) for a in (name, *prefixed, *extra))
    for slug, name, extra in PROVINCES:
        units.append((f"vn-{slug}", f"Tỉnh {name}", "province", "vn", None))
        aliases.extend((f"vn-{slug}", a) for a in (name, f"Tỉnh {name}", *extra))
    for slug, name, parent, extra in FORMER_PROVINCES:
        units.append(
            (
                f"vn-former-{slug}",
                f"Tỉnh {name} (cũ)",
                "former_province",
                f"vn-{parent}",
                REORGANISED,
            )
        )
        aliases.extend((f"vn-former-{slug}", a) for a in (name, f"Tỉnh {name}", *extra))
    unique: dict[str, tuple[str, str]] = {}
    for location_id, alias in aliases:
        key = alias_key(alias)
        if key in unique and unique[key][0] != location_id:
            msg = f"ambiguous location alias {alias!r}"
            raise ValueError(msg)
        _ = unique.setdefault(key, (location_id, alias))
    return units, list(unique.values())


TABLES_DDL: tuple[str, ...] = (
    """CREATE TABLE rag_topics (
    topic_code text PRIMARY KEY CHECK (topic_code ~ '^[a-z_]+$'),
    name_vi text NOT NULL,
    name_en text NOT NULL,
    sort_order integer NOT NULL UNIQUE,
    taxonomy_version text NOT NULL
)""",
    """CREATE TABLE rag_locations (
    location_id text PRIMARY KEY CHECK (location_id ~ '^[a-z0-9-]+$'),
    canonical_name text NOT NULL,
    location_type text NOT NULL CHECK (location_type IN
        ('country', 'municipality', 'province', 'former_province')),
    parent_id text REFERENCES rag_locations (location_id),
    valid_until date,
    gazetteer_version text NOT NULL,
    CHECK (parent_id IS DISTINCT FROM location_id),
    CHECK ((location_type = 'country') = (parent_id IS NULL))
)""",
    "CREATE INDEX rag_locations_parent_idx ON rag_locations (parent_id)",
    """CREATE TABLE rag_location_aliases (
    alias_key text PRIMARY KEY CHECK (alias_key ~ '^[0-9a-z]+( [0-9a-z]+)*$'),
    location_id text NOT NULL REFERENCES rag_locations (location_id),
    alias text NOT NULL
)""",
    """CREATE TABLE rag_article_labels (
    label_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('topic', 'location')),
    topic_code text REFERENCES rag_topics (topic_code),
    location_id text REFERENCES rag_locations (location_id),
    provenance text NOT NULL CHECK (provenance IN ('enrichment', 'manual')),
    match_method text NOT NULL CHECK (match_method IN
        ('enrichment_topic', 'gazetteer_alias', 'manual')),
    publication_id bigint
        REFERENCES rag_enrichment_publications (publication_id),
    model_version text,
    confidence double precision CHECK (confidence BETWEEN 0 AND 1),
    evidence_span_ids uuid[] NOT NULL DEFAULT '{}',
    review_status text NOT NULL DEFAULT 'unreviewed'
        CHECK (review_status IN ('unreviewed', 'approved', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash),
    CHECK ((kind = 'topic') = (topic_code IS NOT NULL)),
    CHECK ((kind = 'location') = (location_id IS NOT NULL)),
    CHECK ((provenance = 'enrichment') = (publication_id IS NOT NULL)),
    CHECK ((provenance = 'manual') = (match_method = 'manual'))
)""",
    """CREATE UNIQUE INDEX rag_article_labels_identity ON rag_article_labels
    (article_id, content_hash, kind, coalesce(topic_code, location_id), provenance)""",
    """CREATE INDEX rag_article_labels_topic_idx ON rag_article_labels
    (topic_code, article_id, content_hash) WHERE kind = 'topic'""",
    """CREATE INDEX rag_article_labels_location_idx ON rag_article_labels
    (location_id, article_id, content_hash) WHERE kind = 'location'""",
    """CREATE TABLE rag_label_syncs (
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN
        ('applied', 'untrusted_model', 'invalid_metadata')),
    publication_id bigint
        REFERENCES rag_enrichment_publications (publication_id),
    model_version text,
    topic_labels integer NOT NULL DEFAULT 0 CHECK (topic_labels >= 0),
    location_labels integer NOT NULL DEFAULT 0 CHECK (location_labels >= 0),
    unmatched_locations integer NOT NULL DEFAULT 0
        CHECK (unmatched_locations >= 0),
    synced_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (article_id, content_hash),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
)""",
    """CREATE VIEW rag_current_labels AS
SELECT label.article_id, label.content_hash, label.kind, label.topic_code,
       label.location_id, label.provenance
FROM rag_article_labels label
JOIN rag_article_current current
    ON current.article_id = label.article_id
    AND current.content_hash = label.content_hash
WHERE current.tombstoned_at IS NULL
  AND label.review_status <> 'rejected'
  AND NOT EXISTS (
      SELECT 1 FROM rag_article_labels manual
      WHERE manual.provenance = 'manual' AND manual.review_status = 'rejected'
        AND manual.article_id = label.article_id
        AND manual.content_hash = label.content_hash
        AND manual.kind = label.kind
        AND manual.topic_code IS NOT DISTINCT FROM label.topic_code
        AND manual.location_id IS NOT DISTINCT FROM label.location_id)""",
    """CREATE TABLE rag_work_jobs (
    job_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('summary', 'query_run')),
    subject_id uuid NOT NULL,
    revision_key text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN
        ('PENDING', 'PROCESSING', 'DONE', 'FAILED', 'CANCELLED', 'STALE')),
    priority integer NOT NULL DEFAULT 0,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts > 0),
    claim_generation bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deadline_at timestamptz,
    failure_code text,
    worker_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (kind, subject_id, revision_key),
    CHECK (attempt_count <= max_attempts),
    CHECK ((status = 'PROCESSING') = (lease_expires_at IS NOT NULL))
)""",
    """CREATE INDEX rag_work_jobs_due_idx
    ON rag_work_jobs (kind, status, priority DESC, next_attempt_at, created_at)""",
    """CREATE TABLE rag_article_summaries (
    summary_id uuid PRIMARY KEY,
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    job_id uuid NOT NULL REFERENCES rag_work_jobs (job_id),
    claim_generation bigint NOT NULL,
    summary_policy_id text NOT NULL,
    model_id text NOT NULL,
    model_version text NOT NULL,
    prompt_version text NOT NULL,
    schema_version text NOT NULL,
    summary_text text NOT NULL CHECK (char_length(summary_text) > 0),
    key_points jsonb NOT NULL CHECK (jsonb_typeof(key_points) = 'array'),
    input_chars integer NOT NULL CHECK (input_chars > 0),
    usage jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash),
    UNIQUE (article_id, content_hash, model_id, model_version, prompt_version,
            schema_version)
)""",
    """CREATE INDEX rag_article_summaries_revision_idx
    ON rag_article_summaries (article_id, content_hash, created_at DESC)""",
    """CREATE TABLE rag_sessions (
    token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    owner_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (expires_at > created_at)
)""",
    "CREATE INDEX rag_sessions_expiry_idx ON rag_sessions (expires_at)",
    """CREATE TABLE rag_conversations (
    conversation_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    title text,
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    scope_sha256 text NOT NULL CHECK (scope_sha256 ~ '^[0-9a-f]{64}$'),
    next_sequence integer NOT NULL DEFAULT 1 CHECK (next_sequence > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deleted_at timestamptz
)""",
    """CREATE INDEX rag_conversations_owner_idx
    ON rag_conversations (owner_id, created_at DESC, conversation_id)
    WHERE deleted_at IS NULL""",
    """CREATE TABLE rag_query_runs (
    run_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES rag_conversations (conversation_id),
    owner_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    question text NOT NULL CHECK (char_length(question) > 0),
    idempotency_key text,
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued', 'running', 'completed', 'failed', 'cancelled')),
    outcome text CHECK (outcome IN ('answered', 'abstained')),
    abstention_reason text,
    failure_code text,
    failure_reason text,
    retrieval_query text,
    history_message_ids jsonb,
    effective_scope jsonb,
    as_of timestamptz,
    answer jsonb,
    source_versions jsonb,
    usage jsonb,
    cancel_requested_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (conversation_id, sequence),
    CHECK ((status = 'completed') = (outcome IS NOT NULL)),
    CHECK ((status = 'failed') = (failure_code IS NOT NULL)),
    CHECK ((status IN ('completed', 'failed', 'cancelled'))
        = (finished_at IS NOT NULL))
)""",
    """CREATE UNIQUE INDEX rag_query_runs_idempotency
    ON rag_query_runs (owner_id, conversation_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL""",
    """CREATE UNIQUE INDEX rag_query_runs_one_active
    ON rag_query_runs (conversation_id) WHERE status IN ('queued', 'running')""",
    """CREATE TABLE rag_messages (
    message_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES rag_conversations (conversation_id),
    run_id uuid NOT NULL REFERENCES rag_query_runs (run_id),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    sequence integer NOT NULL CHECK (sequence > 0),
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id, sequence),
    UNIQUE (run_id, role)
)""",
    """CREATE TABLE rag_run_citations (
    citation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES rag_query_runs (run_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    claim_id uuid NOT NULL,
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    span_id uuid NOT NULL,
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL,
    excerpt text NOT NULL,
    span_fingerprint text NOT NULL,
    source_title text NOT NULL,
    source_name text NOT NULL,
    source_domain text NOT NULL,
    source_url text NOT NULL,
    source_published_at timestamptz,
    UNIQUE (run_id, ordinal),
    CHECK (end_offset > start_offset),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
)""",
    """CREATE FUNCTION rag_schedule_summary() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tombstoned_at IS NULL AND (TG_OP = 'INSERT'
            OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
            OR OLD.tombstoned_at IS NOT NULL) THEN
        UPDATE rag_work_jobs SET status = 'STALE', updated_at = clock_timestamp()
        WHERE kind = 'summary' AND subject_id = NEW.article_id
          AND revision_key <> NEW.content_hash AND status = 'PENDING';
        INSERT INTO rag_work_jobs (job_id, kind, subject_id, revision_key,
                                   max_attempts)
        VALUES (gen_random_uuid(), 'summary', NEW.article_id, NEW.content_hash, 3)
        ON CONFLICT (kind, subject_id, revision_key) DO NOTHING;
    ELSIF NEW.tombstoned_at IS NOT NULL THEN
        UPDATE rag_work_jobs SET status = 'STALE', updated_at = clock_timestamp()
        WHERE kind = 'summary' AND subject_id = NEW.article_id
          AND status = 'PENDING';
    END IF;
    RETURN NULL;
END;
$$""",
    """CREATE TRIGGER rag_schedule_summary
AFTER INSERT OR UPDATE ON rag_article_current
FOR EACH ROW EXECUTE FUNCTION rag_schedule_summary()""",
    """INSERT INTO rag_work_jobs (job_id, kind, subject_id, revision_key, max_attempts)
SELECT gen_random_uuid(), 'summary', article_id, content_hash, 3
FROM rag_article_current WHERE tombstoned_at IS NULL
ON CONFLICT (kind, subject_id, revision_key) DO NOTHING""",
)

DOWNGRADE_DDL: tuple[str, ...] = (
    "DROP TRIGGER rag_schedule_summary ON rag_article_current",
    "DROP FUNCTION rag_schedule_summary()",
    "DROP TABLE rag_run_citations",
    "DROP TABLE rag_messages",
    "DROP TABLE rag_query_runs",
    "DROP TABLE rag_conversations",
    "DROP TABLE rag_sessions",
    "DROP TABLE rag_article_summaries",
    "DROP TABLE rag_work_jobs",
    "DROP VIEW rag_current_labels",
    "DROP TABLE rag_label_syncs",
    "DROP TABLE rag_article_labels",
    "DROP TABLE rag_location_aliases",
    "DROP TABLE rag_locations",
    "DROP TABLE rag_topics",
)


def upgrade() -> None:
    """Create the additive news-backend objects and seed reference data."""
    connection = op.get_bind()
    statements = TABLES_DDL[:-3]
    for statement in statements:
        _ = execute(connection, statement)
    for order, (code, name_vi, name_en) in enumerate(TOPICS, start=1):
        _ = execute(
            connection,
            "INSERT INTO rag_topics VALUES (%s, %s, %s, %s, %s)",
            (code, name_vi, name_en, order, TAXONOMY_VERSION),
        )
    units, aliases = _seed_locations()
    for location_id, name, kind, parent, valid_until in units:
        _ = execute(
            connection,
            """INSERT INTO rag_locations (location_id, canonical_name,
            location_type, parent_id, valid_until, gazetteer_version)
            VALUES (%s, %s, %s, %s, %s::date, %s)""",
            (location_id, name, kind, parent, valid_until, GAZETTEER_VERSION),
        )
    for location_id, alias in aliases:
        _ = execute(
            connection,
            "INSERT INTO rag_location_aliases VALUES (%s, %s, %s)",
            (alias_key(alias), location_id, alias),
        )
    for statement in TABLES_DDL[-3:]:
        _ = execute(connection, statement)


def downgrade() -> None:
    """Drop only the objects this revision added."""
    connection = op.get_bind()
    for statement in DOWNGRADE_DDL:
        _ = execute(connection, statement)
