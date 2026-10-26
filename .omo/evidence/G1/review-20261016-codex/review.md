# Independent T3/G1 review — 2026-10-16

Reviewer: Codex, độc lập với Claude là agent thực hiện thay đổi được review.
Phạm vi: working tree T1–T3/G1 hiện tại và thay đổi selfcheck ngày 2026-10-16.
Không sửa product code, plan, migration SQL, DB/volume hiện có, hoặc báo cáo cũ.
Chỉ thêm evidence và chạy kiểm tra với container pgvector riêng do harness tạo.

**Kết luận: REQUEST CHANGES. G1 acceptance OPEN.** Đã thực hiện independent
F1–F4; không còn lý do nói rằng lượt review này chỉ là self-review. Tuy nhiên,
review không đồng nghĩa các mục đã đạt hoặc chủ dự án đã nghiệm thu.

## Findings

### R1 — P2: Makefile làm hỏng URL PostgreSQL có nhiều query parameter

Vị trí: `Makefile:47`, `Makefile:50`, `Makefile:53`.

Các recipe đưa `$(RAG_DATABASE_URL)` vào shell assignment mà không quote. Với
URL hợp lệ có `?connect_timeout=5&application_name=codex_g1_review`, shell xử lý
`&` như toán tử chạy nền. Biến `EVENTLENS_RAG_DATABASE_URL` không được truyền
đúng cho tiến trình `uv`, và CLI báo thiếu `--database-url`.

Đã tái hiện trên cùng container PostgreSQL/pgvector thật: direct CLI với URL đó
exit 0; `make rag-migrate RAG_DATABASE_URL=<cùng URL>` exit 2, báo `Missing option
'--database-url'`. Lượt Make tái hiện đã bỏ biến EVENTLENS_RAG_DATABASE_URL kế
thừa để chứng minh lỗi cấu hình thông thường. Ba target dùng cùng kiểu assignment.

Khắc phục: truyền URL qua environment an toàn hoặc shell-quote đúng tại cả ba
target. Thêm kiểm tra có nhiều query parameter; URL hợp lệ phải được giữ nguyên
khi tới CLI. Xem `qa.py`, `manual-qa.log` và `make-url-query.log` trong thư mục này.

### R2 — P3: Phần unresolved của plan vẫn mâu thuẫn trạng thái T1/T2 hiện tại

Vị trí: `.omo/plans/eventlens-ai-rag-mvp.md:605`.

Dòng này vẫn nói T1 đang chờ gap audit/acceptance evidence và T2 chưa được đánh
dấu hoàn tất, trái checklist T1/T2 tại dòng 179/188 và trạng thái hiện tại trong
chính tài liệu. Phần verdict phía trên có chú thích lịch sử, nhưng dòng unresolved
này không chỉ rõ là trạng thái lịch sử. Điều này có thể khiến lần thực thi sau
tiếp tục coi các mục đã xong là chưa xong.

Khắc phục: cập nhật trạng thái đang có hiệu lực hoặc gắn nhãn snapshot lịch sử
rõ ràng cho đoạn đó, vẫn bảo toàn nội dung/bằng chứng lịch sử.

## Independent verification

| Command/check | Exit | Kết quả mới |
| --- | ---: | --- |
| `rtk proxy make check` | 0 | 398 passed, 26 skipped do không có DSN; Ruff/typecheck và Bun test/build PASS |
| `rtk proxy make gate-g1` | 2 | Technical PASS; 303 scoped tests và 129 DB tests/0 skip; acceptance OPEN đúng thiết kế hiện tại |
| `with-test-db.sh ... qa.py` | 0 | Migrate x2; nạp 30 bài; lexical 30/vector 0 trước rebuild; FTS đúng 3 nguồn; span khớp substring; rebuild 30 rồi reuse 30/30; ledger 5; vectors/generations 30; self cosine distance 0 |
| URL có hai query parameter | 0 direct CLI / 2 Make | R1 được tái hiện trên DB thật |
| Direct CLI với DB không kết nối được | 3 mỗi lệnh | migrate/load-fixtures/rebuild đều trả `rag_storage_unavailable` |
| 27 receipt SHA-256 của worker | 0 | 27/27 OK |
| `rtk git diff --check` | 0 | Sạch |
| Kiểm tra container `eventlens-rag-test-*` sau các lượt chạy | 0 | Không có container sót |

Raw logs: `make-check.log`, `gate-g1.log`, `manual-qa.log`, `worker-digests.log`.
Gate logs chi tiết: `../run-20261016T045957Z-cBy0v8/`.
DB suite được thực thi qua gate; không tuyên bố chạy lại standalone `make rag-db-test`.
`qa.py` exit 0 nghĩa là các assertion kiểm tra và tái hiện lỗi đều đạt, không có
nghĩa lỗi R1 đã được sửa.

## F1–F4

| Review | Kết quả |
| --- | --- |
| F1 Plan compliance | OPEN: R2 và lịch sử D41 chưa được giải quyết đầy đủ |
| F2 Code quality | REQUEST CHANGES: R1; automated checks PASS |
| F3 Manual QA | PASS cho luồng mặc định; FAIL cho URL override nhiều parameter (R1) |
| F4 Scope fidelity | Ranh giới sản phẩm PASS; clearance D41 lịch sử OPEN |

Xem bốn file `final-*.md` cùng thư mục. Reviewer này không phải implementer của
thay đổi product được review; không dùng thêm reviewer giả hoặc mượn kết quả cũ
làm review hiện tại.

## Điều kiện nghiệm thu còn lại

1. Sửa R1, xác minh đường URL override; giải quyết R2.
2. Cung cấp căn cứ D41 lịch sử hoặc chủ dự án ghi rõ chấp nhận khoảng trống đó.
   Chỉ dẫn giữ apps/worker hiện tại không chứng minh approval ngày 2026-10-03.
3. Sau khi findings được xử lý và review được cập nhật, chủ dự án quyết định
   nghiệm thu G1 theo kế hoạch.

Legacy: không cần drop/archive 6 bảng để sử dụng đường DB riêng eventlens_rag.
Plan hiện đã ghi owner decision về legacy chỉ cần nếu muốn tiếp tục dùng DB đó
in-place (dòng 408). Fresh DB PASS và fail-closed legacy rollback đã được kiểm tra
trong DB suite; không có chứng nhận nâng cấp legacy thành công. Reviewer không
kết nối DB legacy thật và không xác minh lại số dòng tại đó; thông tin 6 bảng rỗng
vẫn thuộc evidence của implementer.

Không có commit/push, provider thật, deployment hay triển khai T4–T9 trong lượt review.
