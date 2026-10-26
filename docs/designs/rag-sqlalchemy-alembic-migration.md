# Kế hoạch chuyển RAG sang SQLAlchemy và Alembic

**Phạm vi:** `src/backend/rag`, gồm rag-api, rag-worker, CLI bảo trì, các adapter PostgreSQL/pgvector và đường migration. `event_pipeline` hiện dùng `InMemoryEventWriter`, không có bảng PostgreSQL trong repo; việc tạo kho event bền vững là một hạng mục khác.

**Điểm bắt đầu:** nhánh đã chứa T1–T9. Chốt commit và bộ test DB trước khi bắt đầu; không gộp thay đổi embedding, BM25, reranker hoặc schema nghiệp vụ mới vào đợt chuyển công cụ này.

## 1. Quyết định kiến trúc

- Dùng SQLAlchemy 2.x đồng bộ với driver psycopg 3 hiện có (`postgresql+psycopg://`). Một `Engine` được tạo cho mỗi process; mỗi thao tác DB có `Connection` hoặc `Session` và transaction ngắn riêng. Không giữ transaction qua lời gọi model/embedding hoặc giữa các node LangGraph.
- Map 8 bảng nghiệp vụ RAG bằng SQLAlchemy ORM để mô tả schema và các thao tác đơn giản. Dùng SQLAlchemy Core hoặc SQL tham số hóa cho FTS, pgvector, `FOR UPDATE SKIP LOCKED`, advisory lock, upsert có điều kiện và publish có fencing. Mục tiêu là mọi đường DB production đi qua SQLAlchemy; không bắt buộc mọi câu truy vấn phải là ORM query.
- Domain, application ports, Pydantic contracts, presentation và LangGraph state không chứa ORM model, `Engine`, `Session` hoặc `Connection`. Repository trong `rag/infra` ánh xạ hàng DB sang domain/contract. `composition.py` chỉ tạo và truyền adapter.
- Alembic là **công cụ duy nhất áp migration RAG sau cutover**. SQL `001–007` và checksum đã áp dụng được giữ nguyên để kiểm chứng lịch sử. Bảng `rag_schema_migrations` được giữ như dấu vết chỉ đọc; các revision mới chỉ ghi vào `alembic_version`.
- Không dùng `Base.metadata.create_all()` để tạo hoặc sửa DB runtime. Không tin `alembic revision --autogenerate` đối với view, trigger, function, CHECK và index pgvector; viết/review migration cho các đối tượng đó.

Vị trí mã đề xuất (tên cuối cùng có thể điều chỉnh khi triển khai):

```text
src/backend/rag/infra/postgres/
  engine.py                 # Engine, pool và transaction factory
  models.py                 # ORM mapping; không xuất ra application
  types.py                  # pgvector/kiểu PostgreSQL nếu cần
  repositories/             # hiện thực các application ports
  migrations/               # SQL 001–007 lưu nguyên byte để audit
alembic.ini
alembic/
  env.py                    # dùng cùng metadata, lấy DSN từ cấu hình triển khai
  versions/                 # baseline và mọi revision mới
```

Các adapter chuyên biệt có thể giữ ở `rag/infra/retrieval` và `rag/infra/diagnostics`; chúng lấy `Engine` từ composition thay vì tự mở psycopg connection. Không chuyển ORM model lên `domain` hay state của workflow.

```text
rag/application/ports  <-  rag/infra/repositories, retrieval, postgres
                                    |
                         SQLAlchemy Engine/Session/Core
                                    |
                            psycopg 3 -> PostgreSQL

Alembic revisions ------------------> schema RAG
```

## 2. Kiểm kê schema hiện tại

| Nhóm | Đối tượng |
| --- | --- |
| Bài viết | `rag_article_documents` (revision bất biến, JSON, FTS), `rag_article_current` (con trỏ hiện hành, tombstone, generation) |
| Enrichment | `rag_enrichment_jobs` (trạng thái, retry, lease, fence), `rag_enrichment_versions` (metadata đang gắn), `rag_enrichment_publications` (lịch sử append-only) |
| Index | `rag_index_generations` (danh tính model/chunker/revision), `rag_chunks` (đoạn, offset, `vector(1536)`) |
| Citation | `rag_source_references` (hash token, caller/scope, span, hạn dùng) |
| Migration cũ | `rag_schema_migrations` (tên file, checksum, thời gian áp dụng) |
| Đối tượng PostgreSQL khác | extension `vector`; hai view `rag_current_lexical`, `rag_current_vectors`; function/trigger bất biến; GIN/HNSW và các index/constraint khác |

Migration gốc nằm trong `src/backend/rag/infra/postgres/migrations/001…007_*.sql`. Mã dùng DB hiện ở `infra/postgres`, `infra/repositories`, `infra/retrieval`, `infra/diagnostics` và một phần CLI bảo trì. Test DB hiện có phải tiếp tục chạy trên PostgreSQL + pgvector thật.

## 3. Alembic tiếp quản DB cũ và tạo DB mới

**DB đã áp dụng `001–007`:**

1. Sao lưu DB; dừng các process ghi trong lúc chuyển migration authority.
2. Preflight kiểm đủ bảy hàng ledger và checksum đúng byte SQL gốc; kiểm bảng/cột, khóa, CHECK, view, trigger, function, extension và index. Kiểm không có `document_json` thiếu, con trỏ current/generation mồ côi hoặc dữ liệu trái ràng buộc.
3. Tạo một Alembic *baseline revision* mô tả **trạng thái schema sau `007`**. Với DB cũ, chỉ `stamp` baseline sau preflight: thao tác này ghi phiên bản Alembic, không chạy DDL. Giữ ledger cũ để audit.
4. Chạy `alembic upgrade head` cho các revision **sau** baseline. Không chạy lại `001–007` và không tự sửa checksum cũ.

**DB mới:** `alembic upgrade head` phải tạo từ rỗng ra schema chức năng tương đương trạng thái sau `007`, rồi áp revision mới. Baseline cần viết DDL đầy đủ và có test so sánh với DB được tạo bằng runner cũ; chỉ có revision rỗng sẽ không tạo được DB mới. Baseline cũng tạo ledger lịch sử với đúng bảy checksum gốc để DB mới và DB cũ có cùng bảng audit. Đây là ghi nhận rằng baseline đã tạo trạng thái tương đương, không phải chạy lại runner cũ. Bảng này không nhận thêm hàng sau baseline.

Giữ cùng một lệnh vận hành `eventlens-rag migrate`, nhưng sau cutover lệnh này gọi Alembic. Tách lệnh preflight/adopt cho DB cũ; không tự `stamp` khi khởi động API/worker. Nếu preflight sai, dừng và yêu cầu xử lý DB, không đoán phiên bản. Trong giai đoạn 1–4, Alembic baseline chỉ được thử trên DB riêng; runner cũ vẫn là migration authority của runtime đang dùng. Đến giai đoạn 5 mới đổi authority một lần, trong cửa sổ dừng ghi. Không chạy đồng thời hai runner trên cùng DB.

## 4. Các giai đoạn thực hiện

| Giai đoạn | Thay đổi | Gate trước khi sang giai đoạn sau |
| --- | --- | --- |
| 0. Baseline | Ghi commit, checksum `001–007`, sơ đồ DB, số hàng, query plan FTS/HNSW và kết quả `make check`, `make rag-db-test`, T9 eval. Chuẩn bị một DB cũ có revision, job, chunk, publication và citation ref. | Baseline tái lập được; không mất dữ liệu thử. |
| 1. Schema + migration | Thêm dependency đã khóa; ORM mappings, `Engine`/session factory trong `rag/infra/postgres`; Alembic baseline và preflight. Runtime vẫn dùng adapter psycopg cũ ở bước này. | DB mới và DB cũ sau adopt có cùng schema/hành vi; `001–007` không đổi byte; Alembic không áp lại DDL cũ. |
| 2. Đường đọc | Chuyển source references và retrieval sang SQLAlchemy. Giữ nguyên truy vấn FTS/vector, bộ lọc, thứ tự tie-break, current/tombstone check và typed error. | Kết quả HTTP, citation viewer, Recall@k fixture, query plan và test DB không đổi về ngữ nghĩa. |
| 3. Đường ghi cơ bản | Chuyển persist revision/current pointer, tombstone, enqueue job và ingestion transaction. | Duplicate delivery cùng job ID; sửa bài làm stale metadata/vector đồng thời; tombstone ẩn ngay; fault injection rollback nguyên transaction. |
| 4. Vòng đời enrichment | Chuyển claim/retry/cancel, staging chunk/index và fenced publication. Mỗi method giữ transaction ngắn; không giữ session qua model call. | Test cạnh tranh nhiều worker, lease hết hạn, stale runner, cancel, retry, provider/DB outage và recovery trên DB thật đều đạt. |
| 5. Cutover hoàn toàn | Chuyển CLI, diagnostics, error mapping và test harness cần thiết; bỏ đường gọi psycopg trực tiếp khỏi production `backend.rag`; `eventlens-rag migrate` gọi Alembic. Cập nhật docs/Docker/CI. | `make check`, `make rag-db-test`, eval và browser gate đạt; build wheel/image; quét import bảo đảm SQLAlchemy chỉ ở `infra` và composition. |

Mỗi giai đoạn nên là một PR review riêng. Không đổi schema nghiệp vụ trong lúc đổi thư viện truy cập DB. Khi cần thay đổi schema về sau, dùng revision Alembic mới với phép chuyển dữ liệu được test riêng.

Test migration cũ đang gọi `migrate(..., directory=...)`, kiểm tên SQL và ledger. Giữ chúng làm bộ kiểm lịch sử `001–007`; thêm test Alembic độc lập cho DB mới, adopt DB cũ và revision tiếp theo. Khi CLI chuyển authority, sửa test CLI theo hành vi mới thay vì xóa các assertion về checksum hoặc legacy schema. Test DB trước/sau nên dùng hai DB clone cùng fixture để so sánh output; tránh cho hai implementation cùng ghi lên một DB trong một lượt so sánh.

## 5. Các bất biến phải chứng minh lại

1. **Persist-first:** bài hiện hành và job PENDING ghi trong một transaction; lexical tìm được bài ngay sau commit.
2. **Không lộ bản cũ:** sửa/xóa bài cập nhật con trỏ và vô hiệu metadata/vector hiện hành đồng bộ; retrieval và viewer kiểm phiên bản hiện tại.
3. **Một chủ sở hữu job:** `FOR UPDATE SKIP LOCKED`, lease theo đồng hồ DB và `claim_generation` giữ nguyên ngữ nghĩa; worker cũ không publish sau khi mất quyền.
4. **Publish nguyên tử:** đủ số chunk, metadata hợp lệ, current hash và fence đều được kiểm trong cùng transaction trước khi đổi generation hoạt động; lịch sử publication vẫn append-only.
5. **Nguồn trích dẫn:** DB chỉ giữ SHA-256 token; ref hết hạn, đổi nội dung, tombstone hoặc thu hồi quyền vẫn cho đúng 404/410/403 như trước.
6. **Tìm kiếm:** GIN FTS, HNSW cosine, phạm vi truy cập, giới hạn ứng viên, RRF/rerank và thứ tự tie-break cho cùng dữ liệu vẫn tương đương. Không suy diễn chất lượng model thật từ test fake.
7. **Lỗi có kiểu:** lỗi kết nối/transaction được chuyển về error domain hiện có; SQLAlchemy exception hoặc DSN không lọt ra API/log công khai.

## 6. Rollout và rollback

- Triển khai trước trên DB clone từ bản cũ, sau đó chạy fresh-DB và adopted-DB suites trong CI. Dùng backup/snapshot trước khi adopt DB vận hành.
- Baseline `stamp` không đổi bảng nghiệp vụ; ở giai đoạn chỉ đổi adapter, có thể quay về binary cũ nếu chưa áp revision schema mới. Giữ đường schema tương thích khi rollout nhiều process.
- Sau revision schema mới, rollback ứng dụng chỉ an toàn nếu revision đó tương thích với binary cũ. Với thay đổi dữ liệu không đảo ngược, phục hồi từ backup hoặc làm revision sửa tiếp; không tự động downgrade phá dữ liệu.
- Chỉ tắt runner migration cũ sau khi đã kiểm đường tạo DB mới và adopt DB cũ. Không để hai công cụ cùng áp thay đổi lên một bảng RAG.

## 7. Định nghĩa hoàn thành

- RAG production dùng SQLAlchemy cho toàn bộ thao tác PostgreSQL; psycopg còn là driver bên dưới SQLAlchemy, không là API được adapter RAG gọi trực tiếp.
- Alembic tạo được DB mới và tiếp quản được DB `001–007` có dữ liệu mà không sửa checksum hoặc mất hàng; chỉ một migration authority cho revision mới.
- Tất cả bất biến ở mục 5 được xác nhận trên PostgreSQL/pgvector thật; báo cáo schema, số hàng, chỉ mục, EXPLAIN và kết quả test trước/sau được lưu làm evidence.
- API, worker, CLI và LangGraph giữ nguyên contract công khai và hành vi lỗi; G3/F1–F4 không tự được đóng bởi refactor thư viện DB.

## Tài liệu công cụ

- [SQLAlchemy PostgreSQL/psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html)
- [Alembic `stamp`](https://alembic.sqlalchemy.org/en/latest/api/commands.html)
- [Giới hạn của Alembic autogenerate](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [pgvector Python/SQLAlchemy integration](https://github.com/pgvector/pgvector-python)

## 8. Triển khai thực tế (nhánh `feature/rag-sqlalchemy-alembic`, từ T9 `8e35c23`)

### 8.1 Quyết định khác hoặc chi tiết hơn thiết kế

| Chủ đề | Quyết định | Lý do / bằng chứng |
| --- | --- | --- |
| Vị trí Alembic | `src/backend/rag/infra/postgres/revisions/` (`env.py`, `script.py.mako`, `versions/`), nạp qua `backend.rag.infra.postgres:revisions`; không có `alembic.ini` ở gốc repo | Revision phải nằm trong wheel/image; CLI cấu hình Alembic bằng code. Test wheel chạy `migrate` từ bản giải nén ngoài checkout. |
| Thực thi SQL | Câu SQL nóng (FTS, pgvector exact, eligibility, `SKIP LOCKED`, fenced publish, upsert có điều kiện, staging) chạy qua `Connection.exec_driver_sql` với **nguyên byte SQL cũ**; tombstone và source references dùng Core trên ORM model | Giữ query plan; parity EXPLAIN cũ/mới trùng nhau. |
| Session | Không dùng ORM `Session`; `transaction()` (engine.py) là transaction factory: một connection checkout, một commit/rollback cho mỗi thao tác | Không có unit-of-work; transaction ngắn như adapter cũ. |
| DSN | Chuỗi libpq (URL hoặc `key=value`) giao nguyên cho psycopg qua hook `do_connect`; Engine URL là `postgresql+psycopg://` không credential | Giữ ngữ nghĩa `options=-c search_path=…` mà test và vận hành dùng. |
| Pool | QueuePool 5+5, `pool_pre_ping`, `pool_recycle=1800`; lệnh migrate dùng NullPool | Test: pool phục hồi sau `pg_terminate_backend`; cắt TCP giữa transaction rồi phục hồi. |
| Prepared statement | `prepare_threshold=None` | Adapter cũ mở connection mới mỗi thao tác nên không bao giờ prepare; connection sống lâu có thể khiến PostgreSQL chuyển sang generic plan. |
| Lỗi | Mất kết nối (connection invalidated, SQLSTATE rỗng, `08*`, `57P*`) → `StorageUnavailableError`; lỗi câu lệnh khác → `StorageOperationError` mới (`rag_storage_failed`, cause là lỗi driver, context SQLAlchemy chứa SQL bị ẩn) | Trước đây lỗi câu lệnh ở đường ghi lọt ra dạng psycopg exception; test fault-injection giữ assertion và kiểm `__cause__` là `RaiseException`. |
| Phạm vi schema | Kiểm tồn tại bảng chỉ trong `current_schema()`; `alembic_version` ghim vào `current_schema()` | `to_regclass` thấy bảng ở `public` qua `search_path` → phân loại sai DB (đã gặp khi thử). |
| Runner cũ | `migrations.migrate` giữ lại (port sang SQLAlchemy) để audit và dựng DB legacy cho test; từ chối áp script khi DB có `alembic_version` (`AlembicManagedError`); dùng chung advisory lock 734528109 với Alembic | Không để hai công cụ cùng quản lý một DB. |
| Preflight | Kiểm: đủ 7 hàng ledger, checksum khớp hằng số baseline và byte file đóng gói; catalog (`pg_get_*def`) bằng `expected_schema.json`; 6 bất biến dữ liệu (document_json, danh tính JSON, tombstone không còn generation, con trỏ generation đúng revision/ACTIVE, chunk đúng revision, publication đúng revision) | Mọi lỗi liệt kê đủ, không stamp, không đổi dữ liệu. |
| Worker khởi động | Vẫn `upgrade head` như trước; DB legacy chưa adopt → `rag_migration_failed` exit 1, không stamp | Giữ hành vi chuẩn bị storage, thêm fail-closed. |
| Downgrade baseline | Từ chối | Downgrade chỉ có thể xóa dữ liệu. |

### 8.2 Bằng chứng (`.omo/evidence/sqlalchemy-alembic/`, không commit)

| Gate | Trước (baseline 8e35c23) | Sau |
| --- | --- | --- |
| `make check` | exit 0; 1083 pass, 139 skip (test DB, không DB) | exit 0; 1093 pass, 166 skip (test DB, không DB) |
| `make rag-db-test` | exit 0; 1064 pass, 0 skip | exit 0; fresh 1101 pass + adopted 1101 pass, 0 skip |
| `make enrichment-eval` | digest `a806275d…2dd0` | digest giống hệt, JSON trùng byte |
| `make rag-eval` | digest `be3348b5…786d` | digest giống hệt, JSON trùng byte |
| Browser (`web-browser-test`) | 9 pass | 9 pass |
| `make gate-g3` | — | technical PASS (exit 2: G3 acceptance vẫn OPEN, cần F1–F4 và chấp nhận của chủ dự án) |
| `make rag-adopt-smoke` | — | 13/13 bước đúng exit code: runner cũ → `migrate`/worker từ chối → preflight → adopt (2 lần) → migrate → rebuild → worker ingest/enrich → rag-api briefing hybrid + citation 200 |
| Wheel | — | `uv build --wheel` chứa `revisions/` + `expected_schema.json` + 7 SQL; CLI từ wheel giải nén chạy `migrate` hai lần ngoài checkout |
| Image `rag-api`, `rag-worker` | — | build exit 0; smoke 20/20 bước (DB fresh: migrate×2, load, rebuild, worker ingest/enrich, API health + briefing hybrid + citation 200; DB adopted: runner cũ, `migrate` và worker từ chối, preflight, adopt, migrate, rebuild, worker, API) |

- Schema: catalog DB Alembic-fresh trùng catalog DB runner cũ (11 relation, 102 cột, 33 constraint, 18 index gồm GIN/HNSW, 2 view, 2 trigger, 1 function, extension `vector`). ORM metadata không drift (Alembic `compare_metadata` rỗng; test chủ động phá index/cột chứng minh có phát hiện).
- Checksum SHA-256 nguyên byte 001–007 không đổi (`9e9b2cfe…`, `e458d823…`, `08fa6ff5…`, `4f373bd2…`, `c05a1a27…`, `ce3631be…`, `1ad7fb7a…`); ledger của DB adopted không đổi kể cả `applied_at`.
- Adopt DB có dữ liệu (32 revision, job READY/FAILED/STALE/PENDING, generation, chunk, publication, tombstone, 2 source ref): SHA-256 từng bảng trước/sau adopt + upgrade giống hệt.
- Parity hai DB clone cùng fixture (code cũ psycopg, code mới trên DB fresh, code mới trên DB adopted): kết quả retrieval lexical và hybrid cho mọi câu hỏi fixture, sau sửa bài/tombstone, worker (duplicate delivery cùng job), source ref, nội dung bảng và EXPLAIN (lexical, vector exact, current, eligibility) trùng nhau. Khác biệt duy nhất trước khi che: timestamp đồng hồ ứng dụng trong JSON (`ingested_at`, `processed_at`, `enriched_at`).
- SQL: 76/80 câu SQL trong adapter, CLI và diagnostics trùng byte với baseline (so bằng AST); 4 câu đơn giản (tombstone, purge/insert/select source refs) cố ý chuyển sang Core trên ORM model.
- `vector-route-report` (pgvector 0.8.0, 3000 bài, 20 truy vấn, 12 kịch bản): route **exact** (production) recall 1.0 ở mọi kịch bản cả cũ lẫn mới; mọi plan (exact, ANN tự nhiên, ANN ép index) cùng hình dạng sau khi bỏ số đo; cờ dùng HNSW giống nhau. Recall của route ANN chẩn đoán dao động: `all_100pct` 0.48 (cũ) / 0.52 (cũ chạy lại) / 0.46 (mới); `source_10pct` ép index 0.81 / 0.815 / 0.82. Cùng code cũ hai lần cho kết quả khác, nên nguyên nhân là việc dựng đồ thị HNSW không tất định (chọn level ngẫu nhiên khi chèn), không phải refactor. Độ trễ trung vị của route exact giảm (vd. 63.0 → 47.6 ms) nhờ pool connection; đây là số đo cục bộ, không phải gate.

### 8.3 Giới hạn còn lại

- Chưa adopt DB vận hành nào; chỉ thử trên DB thử nghiệm. Adopt thật cần backup và cửa sổ dừng ghi.
- Fixture/fake: không suy ra chất lượng model thật; G3/F1–F4 vẫn OPEN.
- `StorageOperationError` là lỗi có kiểu mới cho thất bại câu lệnh ở đường ghi; caller trước đây nhận psycopg exception thô (không có caller production nào bắt loại đó).
- Chưa có revision Alembic nào sau baseline; quy trình review tay được ghi trong `docs/rag.md` và template revision.
