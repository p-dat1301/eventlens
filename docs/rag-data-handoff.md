# Dữ liệu bài viết cần bàn giao để kiểm thử RAG EventLens

Tài liệu này dành cho nhóm dữ liệu. Nhóm dữ liệu **chỉ cung cấp bài viết đã thu thập và trích xuất**. Nhóm EventLens tự chuẩn bị câu hỏi, đáp án tham chiếu, nhãn đánh giá và chạy thử BM25, semantic search, hybrid search, reranker.

## 1. Cần gửi những gì?

Mỗi bản ghi là **một bài viết với toàn văn đã trích xuất**, không chỉ URL, tiêu đề hoặc bản tóm tắt.

| Trường | Yêu cầu | Ý nghĩa |
| --- | --- | --- |
| `article_id` | Bắt buộc; ID ổn định, giữ nguyên khi bài được cập nhật | Nhận diện bài và tránh nạp trùng. UUID là phù hợp nhất; nếu hệ thống nguồn dùng ID khác, gửi nguyên ID đó để EventLens lập bảng ánh xạ. |
| `canonical_url` | Bắt buộc | URL chính của bài. |
| `title` | Bắt buộc, không rỗng | Tiêu đề đúng như đã trích xuất. |
| `content` | Bắt buộc, không rỗng | Toàn văn dạng văn bản thuần, giữ đoạn và dấu câu; không gửi HTML trong trường này. |
| `source_name` | Bắt buộc | Tên nguồn xuất bản. |
| `source_domain` | Bắt buộc | Domain của nguồn, ví dụ `example.org`. |
| `published_at` | Gửi nếu biết; nếu không biết ghi `null` | Thời gian **nguồn xuất bản**, ISO 8601 UTC (`...Z`). Không thay bằng thời gian crawl hoặc thời gian nạp RAG. |
| `language` | Nên gửi nếu đã xác định | Mã ngôn ngữ như `vi`, `en`. Nếu thiếu, nhóm EventLens sẽ xử lý khi xây adapter nhập dữ liệu. |

Nếu đã có sẵn, gửi thêm `source_id`, `updated_at_source`, `fetched_at` và `tags`. Các trường này hỗ trợ kiểm tra nguồn, bản cập nhật và lọc dữ liệu; không cần tự tạo giá trị khi hệ thống nguồn không có.

**Bài được cập nhật:** gửi lại cùng `article_id` và nội dung mới; giữ `updated_at_source` nếu có. Nếu muốn thử tình huống xóa bài, báo riêng danh sách ID đã xóa. Định dạng bài viết dưới đây chưa biểu diễn thao tác xóa.

## 2. Định dạng bàn giao

Gửi `articles.jsonl` mã hóa UTF-8: **mỗi dòng là một JSON của một bài**. Dấu xuống dòng bên trong `content` được ghi dưới dạng `\n`. Có thể nén file thành ZIP và gửi qua nơi chia sẻ riêng của dự án. Ghi kèm ngày xuất dữ liệu, tổng số bài và những nguồn được lấy mẫu.

Ví dụ **một dòng** trong `articles.jsonl` (dữ liệu hư cấu):

```json
{"article_id":"11111111-1111-4111-8111-111111111111","canonical_url":"https://example.org/bai-viet/pha-riverside","title":"Thông báo điều chỉnh lịch phà Riverside","content":"Tuyến phà Riverside tạm dừng lúc 09:00 UTC do thời tiết xấu.\nTuyến hoạt động trở lại lúc 14:00 UTC cùng ngày.","source_name":"Nguồn minh họa","source_domain":"example.org","published_at":"2026-10-20T08:00:00Z","language":"vi"}
```

Các ID, URL và nội dung trong ví dụ chỉ để minh họa. Hãy gửi giá trị thật từ hệ thống nguồn; không điền một thời điểm hoặc ID giả để thay cho dữ liệu còn thiếu.

## 3. Số lượng cần cho từng lần thử

| Mục đích | Số bài đề nghị | Cách chọn |
| --- | ---: | --- |
| Kiểm định schema và đường nạp | 20–50 | Lấy bài thật từ vài nguồn, gồm bài tiếng Việt, bài tiếng Anh nếu có và một số bài dài. |
| Thử ban đầu BM25/semantic/reranker | 300–500 | Lấy mẫu đại diện theo nguồn, thời gian, chủ đề và độ dài; giữ cả bài gần giống, bài tiếp diễn và bài không liên quan. |
| Benchmark để chọn cấu hình | 2.000–5.000 | Mở rộng cùng cách lấy mẫu. Nhóm EventLens sẽ tự tạo và gán nhãn câu hỏi trên tập này. |

Các con số trên là **mục tiêu cho phép thử**, không phải điều kiện bắt buộc của schema. Để so sánh tốc độ ở quy mô lớn, EventLens có thể xin thêm một tập bài chưa gán nhãn sau.

## 4. EventLens sẽ tự xử lý sau khi nhận

- Kiểm schema, ID trùng, URL và thời gian; chuẩn hóa văn bản Unicode NFC.
- Tính hash nội dung, số từ, thời gian nạp/xử lý và các thông tin nội bộ của bản bài hiện hành.
- Chia bài thành các đoạn tìm kiếm, tạo embedding và lập chỉ mục lexical/vector; chạy enrichment khi được cấu hình.
- Tạo evidence span/offset cho đoạn trích dẫn từ nội dung đã chuẩn hóa.
- Tạo câu hỏi và nhãn gold ở cấp **đoạn văn** để đánh giá retrieval, trích dẫn và câu trả lời. Nhóm dữ liệu **không cần gửi câu hỏi, đáp án, nhãn liên quan, embedding hoặc vector**.

## 5. Ranh giới với lệnh ingestion hiện tại

`articles.jsonl` ở trên là **định dạng bàn giao tối giản đề xuất**, chưa phải đầu vào trực tiếp của CLI. Hiện `eventlens-rag-worker ingest` nhận **một `ArticleBundle` JSON cho mỗi bài** theo contract `article.v1`; contract đó còn bắt buộc metadata `raw`, `processing`, `duplicate` và `media`. EventLens cần viết adapter nhập `articles.jsonl` hoặc dùng `ArticleBundle` đầy đủ nếu nhóm dữ liệu đã xuất đúng contract ấy. EventLens không nên tự điền metadata crawl/duplicate giả chỉ để qua validation.

Vì vậy, nhóm dữ liệu có thể gửi tập bài theo mục 1–2 trước; việc chuyển sang contract nội bộ và nạp vào RAG do nhóm EventLens đảm trách.
