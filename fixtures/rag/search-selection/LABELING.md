# Search-selection fixture — labeling guide

`search-selection.v1` is a **synthetic** Vietnamese news corpus written for
EventLens tests. Every organization, person and figure is invented (place names
are real only as settings). No publisher text is included, so the files may live
in Git. Real articles must never be copied here: keep licensed text in a private
local directory and commit only IDs, hashes and metadata.

Source of truth: `scripts/rag/search_fixture_corpus.py` (authored text and
labels). Regenerate with:

```sh
uv run python scripts/rag/build_search_fixture.py          # write
uv run python scripts/rag/build_search_fixture.py --check  # CI: must be unchanged
```

## Files

| File | Content |
| --- | --- |
| `articles.jsonl` | 30 `article_document.v1` records (NFC text, paragraphs separated by a blank line). |
| `questions.jsonl` | 40 questions: `question_id`, `query`, `intent`, `answerability`, `required_article_ids`, `required_chunk_ids` (for `paragraph-900-1.v1`), `required_claims`, `temporal_requirement`, `allowed_scope`. |
| `qrels.jsonl` | Graded judgments. `level: "article"` (grade 0/1/2) and `level: "passage"` (exact code-point span `char_start`/`char_end` plus the quote, grade, `answerability`, `temporal_correct`, `citation_support`). |
| `manifest.json` | Version, counts, SHA-256 of each file, corpus digest, label status. The loader refuses files whose digest differs. |

## Grades

- **2** — the passage/article answers the question by itself (citable).
- **1** — related and partially useful (older figure, context, the other side
  of a false premise), but not sufficient alone.
- **0** — not relevant (implicit: anything without a judgment).

Passage spans are chunker-independent: metrics map them onto whatever chunks a
configuration returns (`exact` = one chunk contains the whole span, `overlap` =
a chunk overlaps it). `required_chunk_ids` are a convenience for
`paragraph-900-1.v1` only.

## Answerability

- `answerable` — at least one grade-2 passage exists.
- `no_answer` — nothing in the corpus answers it (q-032, q-033, q-039).
- `false_premise` — the question presumes something the corpus contradicts or
  never states (q-016 the factory move is an unconfirmed rumor; q-034 nobody
  resigned).

## Temporal requirements

- `latest` — the newest report wins (update questions: q-003, q-010, q-011,
  q-024). Older reports are grade 1 with `temporal_correct: false`.
- `two_time_points` — the answer needs both dates (q-009, q-025).
- a date (`2026-02-20`) — the answer is the state on that day.

## Question mix

Names of people/organizations, places, numbers, dates, paraphrases (q-026 "cầu
bắc qua sông Hậu", q-037 "tàu điện ngầm", unaccented q-035), updates, multi-source
discrepancies (q-004 42 vs ~40 ships, q-030 30 vs 34 kiosks), no-answer, false
premise and two-time-point comparisons.

## Status and limits

- Labels are **author labels from a single annotator**; they are not
  independently reviewed. Use `eventlens-rag search-review-sheet` to produce a
  pooled review sheet (`.private/search-review/`) and record a second
  annotator's grades before using numbers for a production decision.
- 30 articles is a diagnostic set: most configurations hit the ceiling. It
  checks mechanics (offsets, filters, fusion, scope, repeatability), not
  production ranking quality. A 2k–5k article private corpus is needed for that.


## Pilot handoff labels (search-selection benchmark)

# Hướng dẫn gán nhãn `search-questions.v1`

Bộ câu hỏi dùng để chọn cấu hình tìm kiếm (FTS, BM25, semantic, hybrid,
reranker). File nhãn được commit; **toàn văn bài không bao giờ nằm trong Git**.

## 1. Định dạng

`questions.json` (`schema_version = "search-questions.v1"`):

| Trường | Ý nghĩa |
| --- | --- |
| `corpus_export_id`, `corpus_digest` | Snapshot corpus mà offsets trỏ vào; SHA-256 trên `article_id:content_hash` đã sắp xếp. Bench từ chối chạy nếu corpus trong DB khác digest. |
| `chunker_version` | Chunker dùng chung cho mọi engine (`codepoint-1200-200.v1`). |
| `split_seed`, `dev_fraction` | Chia dev/test xác định, phân tầng theo `category`. |
| `questions[].qid` | ID ổn định. |
| `questions[].category` | `proper_noun`, `datetime`, `paraphrase`, `filter`, `multi_source`, `near_duplicate`, `no_answer`. |
| `questions[].split` | `dev` (được dùng để chọn analyzer/model/fusion) hoặc `test` (chỉ báo cáo, **không** dùng để chỉnh). |
| `questions[].filters` | Cùng ngữ nghĩa với `RetrievalScope`: `published_after` bao gồm, `published_before` không bao gồm, bài không có `published_at` bị loại khi có bất kỳ cận nào. |
| `questions[].answerable` | `false` cho câu không có đáp án trong corpus; khi đó `gold = []`. |
| `questions[].gold[]` | Mỗi bài liên quan: `article_id`, `content_hash` (revision RAG), `grade` (2 = có đoạn trả lời trực tiếp, 1 = liên quan một phần), `spans[]`. |
| `gold[].spans[]` | `start`, `end` là offset **Unicode code point** `[start, end)` trên `normalized_content` (NFC) của revision; `excerpt_sha256` là SHA-256 UTF-8 của đoạn trích để kiểm lại mà không lưu văn bản. |
| `label_status` | `candidate` (máy soạn, chưa duyệt), `reviewed_accepted`, `reviewed_edited`, `reviewed_rejected`. |
| `origin`, `reviewer`, `reviewed_at`, `notes` | Nguồn gốc và người duyệt. Nhãn `reviewed_*` bắt buộc có `reviewer`. |

**Nhãn `candidate` không phải gold độc lập.** Chúng do mô hình soạn từ chính bài
viết, nên thiên về trùng từ ngữ với bài. Chỉ tập `reviewed_test` được dùng để
kết luận cấu hình thắng.

## 2. Quy tắc gán nhãn

1. Câu hỏi phải tự nhiên, như người đọc tin hoặc chuyên viên phân tích sẽ hỏi;
   không chép nguyên tiêu đề.
2. `grade 2`: đoạn văn trong bài trả lời trực tiếp câu hỏi. Dán **quote chính
   xác** (copy từ bài, giữ dấu). Quote phải xuất hiện đúng một lần trong bài;
   nếu lặp, mở rộng quote.
3. `grade 1`: bài cùng sự kiện/chủ đề nhưng chỉ trả lời một phần hoặc cho bối
   cảnh. Không bắt buộc quote.
4. Bài khác nguồn đưa cùng thông tin (đăng lại, gần trùng) đều là `grade 2` nếu
   có đoạn trả lời.
5. `paraphrase`: diễn đạt khác từ ngữ của đoạn trả lời (đồng nghĩa, mô tả thay
   cho tên riêng).
6. `filter`: câu hỏi kèm bộ lọc nguồn hoặc thời gian; bài ngoài bộ lọc không
   được tính là liên quan, kể cả khi chứa đáp án.
7. `no_answer`: chủ đề gần corpus nhưng chi tiết được hỏi không có trong bất kỳ
   bài nào. Người duyệt kiểm tra bằng tìm kiếm trước khi chấp nhận.
8. Không sửa nhãn sau khi xem kết quả `test` của một cấu hình cụ thể. Sửa lỗi
   nhãn phải ghi `notes` và áp dụng cho mọi cấu hình (chạy lại toàn bộ).

## 3. Quy trình duyệt

```sh
# 1) Sinh review sheet riêng tư (chứa toàn văn; chỉ mở cục bộ, không commit/chia sẻ)
uv run eventlens-rag search-bench-review-sheet \
    --articles <articles.jsonl> --manifest <manifest.json> \
    --questions fixtures/rag/search-selection/search-test-200-v1/questions.json \
    --report .private/rag-search-bench/reports/<run>/report.json \
    --output .private/rag-search-bench/review/review.html

# 2) Mở review.html, chấm từng câu: accept / edit / reject, chấm grade cho
#    các bài trong pool (top-5 của mọi hệ thống), dán quote cho grade 2,
#    rồi bấm "Xuất review-decisions.json".

# 3) Hợp nhất quyết định (offsets được tính lại từ quote, sai quote là lỗi)
uv run eventlens-rag search-apply-review \
    --articles <articles.jsonl> --manifest <manifest.json> \
    --questions fixtures/rag/search-selection/search-test-200-v1/questions.json \
    --decisions <review-decisions.json> --reviewer "<tên người duyệt>" \
    --output fixtures/rag/search-selection/search-test-200-v1/questions.json
```

Pool lấy từ top-5 bài của mọi cấu hình đã chạy, nên người duyệt thấy cả những
bài liên quan mà người soạn bỏ sót (pooling kiểu TREC). Bài không nằm trong pool
và không được chấm được coi là không liên quan; đây là giới hạn đã biết.
