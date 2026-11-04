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
uv run eventlens-rag search-review-sheet \
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
