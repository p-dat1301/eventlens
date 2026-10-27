# Lựa chọn search cho EventLens RAG (BM25, semantic, hybrid, reranker)

Trạng thái: **pilot 200 bài — chưa thể chọn cấu hình cuối cùng.** Production
giữ nguyên mặc định (PostgreSQL FTS + `FakeEmbeddings` 1536 + `IdentityReranker`).
Kết quả và lý do ở mục 6–7.

## 1. Dữ liệu đã kiểm

Batch `search-test-200-v1` (`/Users/…/Downloads`, chỉ đọc; không commit toàn văn):

| Kiểm tra | Kết quả |
| --- | --- |
| SHA-256 / kích thước / số dòng so với manifest | khớp (`bdb79182…c644ab`, 1.354.082 byte, 200 dòng) |
| Schema `eventlens-rag-handoff-v1` | 200/200 hợp lệ |
| `article_id`, `article_version_id`, `canonical_url` | đều duy nhất; host URL thuộc `source_domain` |
| Thời gian | không mốc nào sau `exported_at`; khoảng `published_at`/`fetched_at` khớp manifest; 0 bài thiếu `published_at` |
| Đếm theo nguồn/ngôn ngữ/parser/quality | khớp manifest: 10 nguồn × 20; 132 `parser-v2/normalize-v4`, 68 `parser-v1/normalize-v1`; 200 `passed`, `is_current=true` |
| Unicode | 200/200 đã NFC (offset raw = offset NFC) |
| Cảnh báo (không chặn import) | 10 bài có ký tự vô hình/định dạng (U+200F, U+200B, U+00A0), 4 tiêu đề còn HTML entity (`&quot;`), 1 bài nội dung bắt đầu bằng tiêu đề |
| Gần trùng (5-gram Jaccard ≥ 0,5 hoặc containment ≥ 0,8) | 1 cặp: DNSE `tuoitre.vn` ↔ `vietstock.vn` (Jaccard 0,82) |
| Hash upstream ↔ hash RAG | 0/200 trùng — đúng thiết kế: RAG băm NFC của `content`, upstream băm theo quy ước parser. Cả hai được lưu (mục 2) |

Báo cáo đầy đủ (không chứa văn bản): `fixtures/rag/search-selection/search-test-200-v1/handoff-validation.json`.

## 2. Nhập dữ liệu và provenance

- Contract: `domain/handoff.py` (`HandoffArticle`, `HandoffManifest`).
  Validator thuần: `application/handoff_validation.py` (lỗi chặn import; cảnh
  báo được ghi lại). Report chỉ chứa ID, mã lỗi, số đếm.
- Đường nạp hợp lệ: `document_from_handoff` → `ArticleDocument` (không bịa
  `raw/duplicate/media`; `fetched_at` không bao giờ thay `published_at`) →
  `ImportHandoff` (kiểm quyền fixture) → `PostgresSourceVersions`: trong **một
  transaction** ghi revision bất biến + con trỏ lexical (`persist_revision` có
  sẵn) + dòng provenance.
- Alembic `rag_0002_source_versions` (mới, additive, không sửa baseline): bảng
  `rag_source_versions` (PK `article_version_id`, FK `(article_id, content_hash)`
  → `rag_article_documents`, lưu nguyên `upstream_content_hash`,
  `parse_version`, `normalization_version` upstream, `export_id`, `fetched_at`,
  `updated_at_source`), trigger bất biến. Downgrade chỉ khi bảng rỗng.
- Một `article_version_id` gắn với hai revision khác nhau → `ProvenanceConflictError`
  (rollback). Nhiều version upstream có thể trỏ cùng một revision RAG.
- Đã chứng minh trên PostgreSQL thật (`tests/backend/rag/db/test_search_selection_db.py`):
  import lặp không tạo revision/provenance/chunk/vector trùng (batch thật: lần 2
  = 0 bản mới); update tạo revision mới làm current, giữ provenance cũ; tombstone
  loại bài khỏi FTS và vector eval; offset chunk → `chunk_texts` → đúng đoạn
  nguồn code-point, kể cả có U+200B; batch lỗi không ghi gì.

Lệnh: `eventlens-rag handoff-validate` / `handoff-import` (chỉ nhận DB tên
`*_eval`, qua `EVENTLENS_RAG_EVAL_DATABASE_URL`).

## 3. Bộ đánh giá

- Format và hướng dẫn gán nhãn: `fixtures/rag/search-selection/LABELING.md`.
- `questions.json`: 96 câu tiếng Việt — 20 tên riêng, 16 ngày giờ, 20 diễn đạt
  lại, 16 bộ lọc, 12 nhiều nguồn, 3 gần trùng, 9 không có đáp án; chia
  dev 38 / test 58 (seed `search-selection-v1`, phân tầng theo loại).
- **Toàn bộ là `candidate` do mô hình soạn**, chưa có người duyệt. Chúng thiên
  về trùng từ với bài (đã thấy ở kết quả: lexical rất cao) và gold thiếu bài
  liên quan ở nguồn khác. Không được coi là gold độc lập.
- Review: `search-review-sheet` (HTML riêng tư, có pool top-5 của mọi hệ thống)
  → `review-decisions.json` → `search-apply-review` (tính lại offset từ quote).

## 4. Kiến trúc benchmark

```text
handoff batch ──validate──▶ ImportHandoff ──▶ PostgreSQL *_eval (rag_0002)
                                   │
              CorpusReader.current_documents() ─▶ chunk_document (T3, 1200/200)
                                   │  cùng một tập chunk cho mọi engine
        ┌──────────────┬───────────┴───────────┬──────────────────────┐
  A RetrievalStore   B ChunkLexicalIndex     C TextEmbedder +        (fake cho CI)
   (FTS production)   (Elasticsearch BM25)    ChunkVectorIndex
                                              (pgvector exact, rag_eval.vectors_<model>_<dim>)
        └──────── D production `fuse` (RRF k=60) ─────────┘
                                   │
                    E production rerank window (40) + RerankerPort
                      (CrossEncoderReranker bge-reranker-v2-m3)
```

- Ports mới: `application/ports/search_index.py` (`ChunkLexicalIndex`,
  `TextEmbedder`, `ChunkVectorIndex`, `CorpusReader`),
  `application/ports/source_versions.py`. Adapter ở `infra/search`,
  `infra/embeddings`, `infra/rerankers`, `infra/postgres`. Systems/runner ở
  `evaluation/search_bench` chỉ phụ thuộc ports + domain (`rank_lexical`,
  `fuse`, `validate_rerank_scores`, `order_after_rerank`); không import SDK.
- Không có SDK Elasticsearch (HTTP stdlib). `sentence-transformers`/`torch` nằm
  trong nhóm `bench` (không cài trong CI/runtime), import động.
- Vector eval ở schema `rag_eval`, bảng `vector(<dim>)` đúng số chiều model;
  **không** đệm/cắt để vừa `vector(1536)`. Qwen3 512 là đầu ra Matryoshka được
  huấn luyện (lấy 512 thành phần đầu rồi chuẩn hoá lại), không phải cắt để vừa cột.
- Công bằng: cùng snapshot (digest kiểm khi chạy), cùng chunk, cùng
  `RetrievalScope` lọc trong engine trước khi cắt độ sâu, cùng kiểm quyền
  fixture, cùng độ sâu 50 chunk/tối đa 3 chunk mỗi bài và tie-break production.
  A giữ nguyên hành vi production (freshness 0,05 với đồng hồ cố định
  `2026-10-26T14:26:14Z`); B/C không có freshness.
- Chọn cấu hình chỉ trên dev (nDCG@10, rồi Recall@10); test chỉ để báo cáo.

## 5. Chạy lại

```sh
make search-eval-up                       # PG pgvector + ES 9.2.0 trên loopback
SEARCH_ARTICLES=…/articles.jsonl SEARCH_MANIFEST=…/manifest.json \
SEARCH_QUESTIONS=fixtures/rag/search-selection/search-test-200-v1/questions.json \
SEARCH_DEVICE=mps SEARCH_REPEATS=3 make search-bench
make search-eval-down
```

Model phải có sẵn trong cache Hugging Face (không tải trong lệnh). CI chỉ chạy
fake + `make search-index-test` (job `search-index`, ES service, cấm skip).

## 6. Kết quả (lượt cuối, commit `3a39430`, worktree sạch)

Môi trường: MacBook M1 Pro 8 nhân/16 GB, MPS; PostgreSQL 17.6 + pgvector 0.8.0;
Elasticsearch 9.2.0 (Lucene 10.3.1, heap 1 GB); torch 2.14.1,
sentence-transformers 5.7.0, transformers 4.57.6. Model (snapshot commit):
`BAAI/bge-m3@5617a9f6`, `Qwen/Qwen3-Embedding-0.6B@97b0c614`,
`BAAI/bge-reranker-v2-m3@953dc6f6`. Corpus digest `sha256:30e474d5…`, 200 bài,
967 chunk. 3 lượt lặp, 0 lỗi, 0 câu bị loại, xếp hạng giống hệt nhau giữa các
lượt. Thời gian chạy 39 phút, RSS đỉnh 4,0 GiB. Report đầy đủ (từng câu, không
chứa văn bản bài): `docs/benchmarks/search-selection/2026-10-27-pilot-200/`.

**Mọi số dưới đây dùng nhãn `candidate` chưa được duyệt.** Test có 58 câu: 53
câu có đáp án và 5 câu không có đáp án.

| Cấu hình (test) | R@5 | R@10 | MRR | nDCG@10 | Span@10 | NoAns AUROC (5 câu âm) | p50 / p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| A FTS hiện tại | 0,939 | 0,943 | 0,934 | 0,924 | 50/53 | 0,68 | 252 / 286 |
| B BM25 `vi-standard.v1` | 0,986 | 0,991 | 0,972 | 0,973 | 50/53 | 0,62 | 24 / 55 |
| B BM25 `vi-standard-fold.v1` | 0,961 | 0,965 | 0,973 | 0,966 | 49/53 | 0,54 | 13 / 17 |
| B BM25 `vi-syllable-bigram.v1` | 0,986 | 0,991 | 0,981 | 0,980 | 49/53 | 0,60 | 12 / 17 |
| C BGE-M3 1024 | 0,991 | 0,991 | 0,981 | 0,984 | 50/53 | 0,75 | 16 / 36 (+33 embed) |
| C Qwen3-0.6B 1024 | 0,991 | 0,991 | 0,991 | 0,988 | 51/53 | 0,80 | 16 / 43 (+62 embed) |
| C Qwen3-0.6B 512 | 0,991 | 0,991 | 0,981 | 0,984 | 51/53 | 0,82 | 15 / 36 (+61 embed) |
| D FTS + BGE-M3 (RRF) | 0,939 | 0,991 | 0,956 | 0,956 | 50/53 | 0,65 | 270 / 329 |
| D BM25 std + BGE-M3 (RRF), **chọn trên dev** | 0,991 | 0,991 | 0,981 | 0,983 | 49/53 | 0,59 | 30 / 47 (+33 embed) |
| D BM25 bigram + Qwen3 512 (tốt nhất trên test, không dùng để chọn) | 0,991 | 0,991 | 0,991 | 0,990 | 49/53 | 0,45 | 26 / 43 |
| E = D chọn trên dev + bge-reranker-v2-m3 (40 chunk) | 0,991 | 0,991 | 0,981 | 0,983 | 51/53 | 0,95 | 4.666 / 5.110 |

9 tổ hợp D còn lại nằm trong khoảng nDCG@10 từ 0,956 đến 0,990 (xem `summary.md`).
Latency của C/D/E chưa gồm thời gian embed câu hỏi; cột "+embed" là p50 embed
một câu trên MPS. Embed câu hỏi được làm trước và đo riêng, vì nạp cả 4 model
cùng lúc làm máy 16 GB bị swap.

So sánh cặp trên test (bootstrap 10.000 lần, seed 20261027):

| Cặp | ΔnDCG@10 [CI 95%] | ΔR@10 [CI 95%] | Thắng/thua/hoà |
|---|---|---|---|
| D chọn trên dev − A FTS | +0,059 [+0,011; +0,116] | +0,047 [+0,006; +0,101] | 9/1/43 |
| BM25 tốt nhất trên dev (fold) − A FTS | +0,041 [+0,008; +0,083] | +0,022 [−0,006; +0,060] | 6/2/45 |
| E − D | 0,000 [0; 0] | 0,000 [0; 0] | 0/0/53 |

Chi phí index (967 chunk, M1/MPS): ES 0,9–1,1 s, 1,4–2,8 MiB; BGE-M3 110 s,
5,4 MiB; Qwen3 219–227 s, 5,4 MiB (1024 chiều) và 2,9 MiB (512 chiều). Ngoại suy
tuyến tính cho 5.000 bài (khoảng 24.000 chunk): BGE-M3 mất khoảng 45 phút trên máy này,
Qwen3 khoảng 90 phút; cần đo lại trên phần cứng đích. Không có chi phí API: mọi
model đều chạy cục bộ.

Những case đáng chú ý (test):
- A thua rõ ở `multi_source` (nDCG 0,75 so với 0,95–0,98 của các cấu hình
  khác) và `paraphrase` (0,89 so với 1,00 của semantic/hybrid). Lý do: A lấy
  pool 50 bài theo `ts_rank_cd` với toán tử OR trên lexeme `simple`, không có
  stopword tiếng Việt, rồi chấm chunk theo tỉ lệ lexeme trùng. A cũng chậm hơn
  BM25 khoảng 10 lần, do tính cửa sổ chunk bằng SQL.
- Bài `parser-v1` khó hơn với mọi cấu hình (A 0,89; BM25 0,96; semantic
  0,97–0,99), nhưng chỉ có 17 câu.
- Reranker không đổi thứ hạng bài ở `R@k`/nDCG (đã sát trần), chỉ nâng Span@10
  từ 49 lên 51 và tách câu không có đáp án tốt hơn hẳn (AUROC 0,95, nhưng chỉ
  dựa trên 5 câu âm). Latency p50 4,7 s vượt xa lát 1,5 s của
  `retrieval-policy.v3`.
- Analyzer gấp dấu (`fold`) kém hơn analyzer giữ dấu ở `paraphrase` và
  `tuoitre.vn`, vì mất dấu làm nhập nhằng âm tiết tiếng Việt.

## 7. Quyết định: **chưa thể chọn cấu hình cuối cùng**

Lý do:
1. Không có nhãn nào được người duyệt (`reviewed_test` = 0 câu). Nhãn
   `candidate` do mô hình soạn, nên trùng từ ngữ với bài và thiếu gold ở các
   nguồn khác.
2. Hiệu ứng trần: 14/19 cấu hình phase 1 đạt nDCG@10 ≥ 0,97, và 43/53 câu hoà
   giữa D và A. Các model semantic (BGE-M3 so với Qwen3 1024/512) và các tổ hợp
   hybrid chênh nhau dưới mức nhiễu; dev và test chọn khác nhau (dev chọn BM25
   std + BGE-M3, test thì BM25 bigram + Qwen3 512 cao nhất). Chưa đủ bằng chứng
   để chọn model hay số chiều.
3. Chỉ có 9 câu không có đáp án (5 câu trong test). AUROC trên 5 mẫu âm không
   đủ tin cậy.
4. 200 bài là pilot: số chunk ít nên cả FTS lẫn vector exact đều nhanh; chưa
   đo ANN/HNSW, chưa đo tải đồng thời.

Tín hiệu có thể dùng để định hướng (chưa phải quyết định):
- Cả BM25 và hybrid đều hơn FTS hiện tại, với CI 95% không chứa 0 (trên nhãn
  candidate). FTS đang yếu ở câu nhiều nguồn/diễn đạt lại và chậm hơn khoảng 10
  lần. Đây là lý do nên đưa BM25 vào vòng đo tiếp theo. Có dùng Elasticsearch
  trong vận hành hay không còn phải cân thêm chi phí chạy một dịch vụ mới
  (heap ≥ 1 GB, đồng bộ index khi update/tombstone).
- Reranker chưa đáng bật với latency này (p50 4,7 s trên M1). Nên đo lại trên
  GPU/CPU máy chủ, hoặc dùng nó làm tín hiệu từ chối trả lời (abstention), thay
  vì để xếp lại thứ tự.

Production giữ nguyên: không thêm cờ cấu hình, không migration vector, không
reindex. Vì vậy chưa có tài liệu rollback cho đường mới; revision duy nhất là
`rag_0002_source_versions` (additive, downgrade được khi bảng rỗng).

## 8. Dữ liệu cần bổ sung và bước còn mở

1. **Người duyệt nhãn:** duyệt 96 câu bằng review sheet, đặc biệt chấm thêm
   các bài trong pool, rồi chạy lại `make search-bench` và đọc `reviewed_test`.
2. **Câu hỏi thật** từ người dùng/analyst. Mục tiêu tiếp theo: ≥ 200 câu, trong
   đó ≥ 30 câu không có đáp án và ≥ 40 câu diễn đạt lại/nhiều nguồn; đa số câu
   hỏi không do mô hình soạn từ chính bài viết.
3. **Corpus 2.000–5.000 bài**, cùng cách lấy mẫu, có nhiều bài cùng sự kiện để
   bài toán khó hơn; đo thêm ANN (HNSW) so với exact và latency khi tải đồng thời.
4. **QA đầu-cuối:** chưa chạy, vì chưa chọn được retrieval và chưa có provider
   LLM live được duyệt (production chỉ có fake/fixture). Khi có cả hai, chạy
   synthesis và kiểm citation/abstention trên cấu hình thắng.
5. Word segmentation tiếng Việt (ví dụ CocCoc tokenizer) chưa được thử; cả 3
   analyzer hiện chỉ tách theo âm tiết.
6. Cảnh báo dữ liệu cần nhóm parser trả lời: HTML entity trong tiêu đề, ký tự
   vô hình, và quy ước hash upstream.
